"""
store.py — In-Memory Leaderboard Store

This module owns all business logic that sits between the API layer and
the raw data structures (SkipList, LRUCache).

Architecture per leaderboard
─────────────────────────────
  ┌─────────────────────────────────────────────────────┐
  │  Leaderboard                                         │
  │                                                      │
  │  players: dict[player_id → PlayerEntry]             │
  │    └─ O(1) score lookup, metadata, timestamp        │
  │                                                      │
  │  skip_list: SkipList                                 │
  │    └─ O(log n) insert / delete / rank               │
  │    └─ O(k)     top-K / range walk                   │
  │                                                      │
  │  cache: LRUCache                                     │
  │    └─ caches hot read results (topK, range, page)   │
  │    └─ invalidated on every write                    │
  └─────────────────────────────────────────────────────┘

Production notes
────────────────
  Swap LeaderboardStore for a Redis adapter:
    - players dict  → HSET lb:{id}:players
    - skip_list     → ZADD / ZRANK / ZRANGE  (Redis sorted set)
    - cache         → Redis TTL keys or local process cache
  The public method signatures stay the same — only the implementation changes.
"""

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.core.skip_list import SkipList
from app.core.lru_cache import LRUCache


# ── Utilities ─────────────────────────────────────────────────────────────────

def _now() -> float:
    """Current UTC time as a Unix timestamp."""
    return time.time()


def _dt(ts: float) -> datetime:
    """Convert a Unix timestamp to a timezone-aware UTC datetime."""
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _iso(ts: float) -> str:
    """Unix timestamp → ISO-8601 string (used in serialised dicts)."""
    return _dt(ts).isoformat()


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class PlayerEntry:
    """
    Everything the store knows about one player on one leaderboard.
    The score and timestamp are duplicated in the skip list so we can
    call skip_list.delete() without a second lookup.
    """
    player_id: str
    score:     float
    timestamp: float            # Unix time of last update
    metadata:  dict = field(default_factory=dict)


@dataclass
class Leaderboard:
    """
    One leaderboard — owns its own skip list, player map, and LRU cache.

    Fields
    ------
    id            : unique identifier, e.g. "lb_a1b2c3d4"
    name          : human-readable slug, e.g. "chess-world-cup"
    order         : "desc" (high score = rank 1) or "asc" (low = rank 1)
    max_entries   : evict the worst-ranked player once this is exceeded
    reset_policy  : "never" | "daily" | "weekly" | "monthly"
    created_at    : Unix timestamp
    last_updated  : Unix timestamp of the most recent score change, or None
    """
    id:           str
    name:         str
    order:        str
    max_entries:  int
    reset_policy: str
    created_at:   float
    last_updated: Optional[float]      = None
    skip_list:    SkipList             = field(default_factory=SkipList)
    players:      dict[str, PlayerEntry] = field(default_factory=dict)
    cache:        LRUCache             = field(default_factory=lambda: LRUCache(256))


# ── Store ─────────────────────────────────────────────────────────────────────

class LeaderboardStore:
    """
    In-memory registry of all leaderboards.

    All mutating methods invalidate the leaderboard's LRU cache so that
    subsequent reads always reflect the latest state.
    """

    def __init__(self):
        self._boards: dict[str, Leaderboard] = {}

    # ── Leaderboard CRUD ──────────────────────────────────────────────────────

    def create(
        self,
        name:         str,
        order:        str = "desc",
        max_entries:  int = 10_000,
        reset_policy: str = "never",
    ) -> Leaderboard:
        """
        Create and register a new leaderboard.
        The generated ID is a short hex string prefixed with "lb_".
        """
        lb = Leaderboard(
            id           = f"lb_{uuid.uuid4().hex[:8]}",
            name         = name,
            order        = order,
            max_entries  = max_entries,
            reset_policy = reset_policy,
            created_at   = _now(),
        )
        self._boards[lb.id] = lb
        return lb

    def get(self, lb_id: str) -> Optional[Leaderboard]:
        """Return a leaderboard by ID, or None if it doesn't exist."""
        return self._boards.get(lb_id)

    def list_all(self) -> list[Leaderboard]:
        """Return all leaderboards in insertion order."""
        return list(self._boards.values())

    def update(
        self,
        lb:           Leaderboard,
        max_entries:  Optional[int] = None,
        reset_policy: Optional[str] = None,
    ) -> Leaderboard:
        """
        Mutate leaderboard configuration.
        Only max_entries and reset_policy are allowed to change after creation.
        """
        if max_entries is not None:
            lb.max_entries = max_entries
        if reset_policy is not None:
            lb.reset_policy = reset_policy
        lb.cache.invalidate_prefix(lb.id)
        return lb

    def delete(self, lb_id: str) -> bool:
        """
        Permanently remove a leaderboard and all its data.
        Returns True if deleted, False if not found.
        """
        if lb_id in self._boards:
            del self._boards[lb_id]
            return True
        return False

    # ── Score mutations ───────────────────────────────────────────────────────

    def upsert_score(
        self,
        lb:        Leaderboard,
        player_id: str,
        score:     float,
        metadata:  dict,
    ) -> PlayerEntry:
        """
        Insert a new player or replace an existing player's score.

        Steps:
          1. If the player exists, remove their old node from the skip list.
          2. Insert a new skip list node with the new score and current timestamp.
          3. Update the players dict.
          4. If max_entries is exceeded, evict the last-place player.
          5. Invalidate the LRU cache.

        O(log n) overall.
        """
        ts = _now()

        # Remove old skip list node if the player already has a score.
        if player_id in lb.players:
            old = lb.players[player_id]
            lb.skip_list.delete(old.player_id, old.score, old.timestamp)

        # Insert the new node.
        lb.skip_list.insert(player_id, score, ts)
        lb.players[player_id] = PlayerEntry(
            player_id = player_id,
            score     = score,
            timestamp = ts,
            metadata  = metadata,
        )
        lb.last_updated = ts

        # Enforce max_entries: evict the lowest-ranked player if over the cap.
        if lb.skip_list.size > lb.max_entries:
            self._evict_last(lb)

        lb.cache.invalidate_prefix(lb.id)
        return lb.players[player_id]

    def _evict_last(self, lb: Leaderboard) -> None:
        """
        Remove the last node in the skip list (worst-ranked player).
        Called automatically when max_entries is exceeded.
        """
        # Walk to the last node at level 0.
        current = lb.skip_list.head
        while current.forward[0] is not None:
            prev    = current
            current = current.forward[0]
        # current is now the last node.
        last_node = current
        lb.skip_list.delete(last_node.player_id, last_node.score, last_node.timestamp)
        lb.players.pop(last_node.player_id, None)

    def increment_score(
        self,
        lb:        Leaderboard,
        player_id: str,
        delta:     float,
        metadata:  dict,
    ) -> PlayerEntry:
        """
        Add `delta` to the player's existing score.
        If the player doesn't exist yet, they start from 0 + delta.
        Metadata is merged (incoming keys overwrite existing ones).
        """
        existing_score = lb.players[player_id].score if player_id in lb.players else 0.0
        merged_meta    = {
            **(lb.players[player_id].metadata if player_id in lb.players else {}),
            **metadata,
        }
        return self.upsert_score(lb, player_id, existing_score + delta, merged_meta)

    def bulk_upsert(
        self,
        lb:      Leaderboard,
        entries: list,
    ) -> tuple[int, int, list[dict]]:
        """
        Upsert up to 500 scores in one call.
        Each entry is processed independently — a failure on one doesn't
        abort the rest.

        Returns (submitted, failed, errors).
        """
        submitted = 0
        failed    = 0
        errors    = []

        for e in entries:
            try:
                self.upsert_score(lb, e.player_id, e.score, e.metadata)
                submitted += 1
            except Exception as exc:
                failed += 1
                errors.append({"player_id": e.player_id, "error": str(exc)})

        return submitted, failed, errors

    def remove_player(self, lb: Leaderboard, player_id: str) -> bool:
        """
        Remove a single player from the leaderboard.
        Returns True if the player existed and was removed, False otherwise.
        O(log n).
        """
        if player_id not in lb.players:
            return False
        entry = lb.players.pop(player_id)
        lb.skip_list.delete(entry.player_id, entry.score, entry.timestamp)
        lb.cache.invalidate_prefix(lb.id)
        return True

    def bulk_remove(
        self,
        lb:         Leaderboard,
        player_ids: list[str],
    ) -> tuple[int, list[str]]:
        """
        Remove multiple players in one call.
        Returns (deleted_count, not_found_ids).
        """
        deleted   = 0
        not_found = []
        for pid in player_ids:
            if self.remove_player(lb, pid):
                deleted += 1
            else:
                not_found.append(pid)
        return deleted, not_found

    def reset(self, lb: Leaderboard) -> int:
        """
        Wipe every score from a leaderboard while keeping the board itself.
        Returns the number of players that were cleared.
        """
        count         = lb.skip_list.size
        lb.skip_list  = SkipList()
        lb.players    = {}
        lb.cache.invalidate_all()
        lb.last_updated = _now()
        return count

    # ── Query helpers ─────────────────────────────────────────────────────────

    def _percentile(self, rank: int, total: int) -> float:
        """
        What percentage of players does this player beat?
        rank=1, total=100 → 100.0 (beats everyone)
        rank=100, total=100 → 1.0 (beats only themselves)
        """
        if total == 0:
            return 100.0
        return round((1 - (rank - 1) / total) * 100, 2)

    def _node_to_dict(self, lb: Leaderboard, node, rank: int, total: int) -> dict:
        """Serialise a SkipNode + its PlayerEntry into a plain dict."""
        entry = lb.players[node.player_id]
        return {
            "rank":       rank,
            "player_id":  node.player_id,
            "score":      node.score,
            "percentile": self._percentile(rank, total),
            "metadata":   entry.metadata,
            "updated_at": _iso(entry.timestamp),
        }

    # ── Rank queries ──────────────────────────────────────────────────────────

    def get_rank(self, lb: Leaderboard, player_id: str) -> Optional[int]:
        """
        Return the 1-based rank of a player, or None if not on the board.
        O(log n).
        """
        if player_id not in lb.players:
            return None
        e = lb.players[player_id]
        return lb.skip_list.get_rank(player_id, e.score, e.timestamp)

    def get_top(self, lb: Leaderboard, k: int, offset: int = 0) -> list[dict]:
        """
        Return the top `k` players starting at rank `offset+1`.
        Results are LRU-cached; cache is invalidated on any write.
        O(offset + k) after cache miss.
        """
        cache_key = f"{lb.id}:top:{k}:{offset}"
        cached    = lb.cache.get(cache_key)
        if cached is not None:
            return cached

        total  = lb.skip_list.size
        nodes  = lb.skip_list.get_top(k, offset)
        result = [
            self._node_to_dict(lb, node, offset + i + 1, total)
            for i, node in enumerate(nodes)
        ]
        lb.cache.put(cache_key, result)
        return result

    def get_page(self, lb: Leaderboard, page: int, page_size: int) -> dict:
        """
        Page-based pagination over the full ranking.
        Pages are 1-indexed. Uses get_top() internally (and its cache).
        """
        offset = (page - 1) * page_size
        total  = lb.skip_list.size
        items  = self.get_top(lb, page_size, offset)
        return {
            "items":     items,
            "total":     total,
            "page":      page,
            "page_size": page_size,
            "has_next":  offset + page_size < total,
            "has_prev":  page > 1,
        }

    def get_range(self, lb: Leaderboard, from_rank: int, to_rank: int) -> list[dict]:
        """
        Return all players with rank in [from_rank, to_rank] (1-based, inclusive).
        Results are LRU-cached.
        O(to_rank − from_rank) after cache miss.
        """
        cache_key = f"{lb.id}:range:{from_rank}:{to_rank}"
        cached    = lb.cache.get(cache_key)
        if cached is not None:
            return cached

        total  = lb.skip_list.size
        nodes  = lb.skip_list.get_range(from_rank, to_rank)
        result = [
            self._node_to_dict(lb, node, from_rank + i, total)
            for i, node in enumerate(nodes)
        ]
        lb.cache.put(cache_key, result)
        return result

    def get_player(
        self,
        lb:        Leaderboard,
        player_id: str,
        window:    int = 0,
    ) -> Optional[dict]:
        """
        Look up a player's full rank details.
        If window > 0, also include the `window` players above and below them.
        Returns None if the player is not on the leaderboard.
        """
        if player_id not in lb.players:
            return None

        entry = lb.players[player_id]
        rank  = self.get_rank(lb, player_id)
        total = lb.skip_list.size

        result = {
            "player_id":    player_id,
            "rank":         rank,
            "score":        entry.score,
            "percentile":   self._percentile(rank, total),
            "total_players": total,
            "metadata":     entry.metadata,
            "updated_at":   _iso(entry.timestamp),
        }

        if window > 0:
            from_r          = max(1, rank - window)
            to_r            = min(total, rank + window)
            result["nearby"] = self.get_range(lb, from_r, to_r)

        return result

    # ── Search ────────────────────────────────────────────────────────────────

    def search_by_score_range(
        self,
        lb:        Leaderboard,
        min_score: float,
        max_score: float,
        limit:     int,
    ) -> list[dict]:
        """
        Return up to `limit` players whose score falls in [min_score, max_score].

        Implementation: single linear scan of the level-0 linked list.
        Because the list is sorted descending by score we can stop early
        once we pass below min_score — O(rank_of_min_score) in practice.
        """
        total   = lb.skip_list.size
        results = []
        current = lb.skip_list.head.forward[0]

        while current is not None and len(results) < limit:
            # Since list is descending, once score < min_score we're done.
            if current.score < min_score:
                break
            if current.score <= max_score:
                rank = lb.skip_list.get_rank(
                    current.player_id, current.score, current.timestamp
                )
                results.append(self._node_to_dict(lb, current, rank, total))
            current = current.forward[0]

        return results

    def search_by_player_prefix(
        self,
        lb:     Leaderboard,
        prefix: str,
        limit:  int,
    ) -> list[dict]:
        """
        Return up to `limit` players whose player_id starts with `prefix`.
        Results are sorted by rank ascending.

        Implementation: linear scan of the players dict — O(n).
        In production, maintain a trie over player IDs for O(|prefix|) lookup.
        """
        total   = lb.skip_list.size
        results = []

        for pid, entry in lb.players.items():
            if not pid.startswith(prefix):
                continue
            rank = self.get_rank(lb, pid)
            results.append({
                "player_id":  pid,
                "score":      entry.score,
                "rank":       rank,
                "percentile": self._percentile(rank, total),
                "metadata":   entry.metadata,
                "updated_at": _iso(entry.timestamp),
            })
            if len(results) >= limit:
                break

        results.sort(key=lambda x: x["rank"])
        return results

    # ── Aggregate stats ───────────────────────────────────────────────────────

    def get_stats(self, lb: Leaderboard) -> dict:
        """
        Compute live aggregate statistics for a leaderboard.
        O(n) — iterates all player scores.
        For very large boards this could be maintained incrementally instead.
        """
        total = lb.skip_list.size
        if total == 0:
            return {
                "total_players": 0,
                "highest_score": None,
                "lowest_score":  None,
                "average_score": None,
                "last_updated":  None,
            }

        scores = [e.score for e in lb.players.values()]
        return {
            "total_players": total,
            "highest_score": max(scores),
            "lowest_score":  min(scores),
            "average_score": round(sum(scores) / len(scores), 4),
            "last_updated":  _iso(lb.last_updated) if lb.last_updated else None,
        }

    def get_cache_stats(self, lb: Leaderboard) -> dict:
        """Return LRU cache health metrics for a leaderboard (useful for /debug endpoints)."""
        return lb.cache.stats()


# ── Module-level singleton ────────────────────────────────────────────────────
# Imported by the API routers. In production, replace with a factory that
# returns a Redis-backed store injected via FastAPI's dependency system.

store = LeaderboardStore()