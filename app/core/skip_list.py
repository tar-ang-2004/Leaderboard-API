"""
skip_list.py — Probabilistic Skip List

A skip list is a layered linked list where each node is promoted to higher
levels with probability P. This gives O(log n) expected time for insert,
delete, and search — same as a balanced BST but far simpler to implement.

Layout (descending by score, ties broken by earlier timestamp):

level 3:  head ────────────────────────────────────> [bob:900] ──> None
level 2:  head ──────────────> [carol:700] ─────────> [bob:900] ──> None
level 1:  head ──> [alice:500] -> [carol:700] ──────> [bob:900] ──> None
level 0:  head ──> [alice:500] -> [carol:700] ──────> [bob:900] ──> None
                   rank=3         rank=2                rank=1

Complexity:
  insert   O(log n) average
  delete   O(log n) average
  get_rank O(log n) average
  get_top  O(k)     — walk level-0 linked list
  space    O(n log n) expected
"""

import random
from dataclasses import dataclass, field
from typing import Optional

MAX_LEVEL = 16    # supports up to ~65k nodes at P=0.5 before degradation
P         = 0.5   # promotion probability per level


# ── Node ──────────────────────────────────────────────────────────────────────

@dataclass
class SkipNode:
    """
    A single node in the skip list.

    `forward` is a list of length `level` where forward[i] points to the
    next node at level i. Level 0 is the full linked list; higher levels
    are express lanes that skip over groups of nodes.
    """
    player_id: str
    score:     float
    timestamp: float                          # used for tie-breaking
    forward:   list = field(default_factory=list)  # list[Optional[SkipNode]]


# ── Skip List ─────────────────────────────────────────────────────────────────

class SkipList:
    """
    Sorted descending by score.
    Equal scores are ordered by timestamp ascending (earlier timestamp = better rank).

    Usage:
        sl = SkipList()
        sl.insert("alice", 500.0, time.time())
        sl.insert("bob",   800.0, time.time())
        rank = sl.get_rank("alice", 500.0, ts)   # → 2
        top3 = sl.get_top(3)
        sl.delete("alice", 500.0, ts)
    """

    def __init__(self):
        # Sentinel head node — score=+inf so every real node sorts after it
        self.head  = SkipNode("__head__", float("inf"), 0.0, [None] * MAX_LEVEL)
        self.level = 1    # current highest active level (1-indexed)
        self.size  = 0    # number of real nodes

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _random_level(self) -> int:
        """
        Flip a coin up to MAX_LEVEL times.
        Returns a geometrically distributed level in [1, MAX_LEVEL].
        """
        lvl = 1
        while lvl < MAX_LEVEL and random.random() < P:
            lvl += 1
        return lvl

    def _comes_before(self, node: SkipNode, score: float, ts: float) -> bool:
        """
        Return True if `node` should appear BEFORE a node with (score, ts)
        in our sorted order (descending score, ascending timestamp for ties).
        """
        if node.score != score:
            return node.score > score        # higher score ranks first
        return node.timestamp < ts           # earlier timestamp ranks first

    def _find_update(self, score: float, ts: float) -> list:
        """
        Walk from the highest level down to level 0.
        At each level, advance as long as the next node comes before (score, ts).
        Returns update[i] = the last node at level i that is still before
        the insertion/deletion position.
        This is the standard skip list predecessor array.
        """
        update  = [None] * MAX_LEVEL
        current = self.head

        for i in range(self.level - 1, -1, -1):
            while (
                current.forward[i] is not None
                and self._comes_before(current.forward[i], score, ts)
            ):
                current = current.forward[i]
            update[i] = current

        return update

    # ── Public API ────────────────────────────────────────────────────────────

    def insert(self, player_id: str, score: float, timestamp: float) -> None:
        """
        Insert a new node in O(log n) expected time.
        Caller must ensure the (player_id, score, timestamp) triple is unique.
        To update a score, call delete() then insert().
        """
        update    = self._find_update(score, timestamp)
        new_level = self._random_level()

        # If the new node introduces levels we haven't seen before,
        # point those fresh levels at head so splice-in works uniformly.
        if new_level > self.level:
            for i in range(self.level, new_level):
                update[i] = self.head
            self.level = new_level

        node = SkipNode(player_id, score, timestamp, [None] * new_level)

        # Splice the new node into every level it participates in.
        for i in range(new_level):
            node.forward[i]    = update[i].forward[i]
            update[i].forward[i] = node

        self.size += 1

    def delete(self, player_id: str, score: float, timestamp: float) -> bool:
        """
        Remove the node matching (player_id, score, timestamp) in O(log n).
        Returns True if found and deleted, False if not present.
        """
        update  = self._find_update(score, timestamp)
        target  = update[0].forward[0]

        # Verify the candidate node really is the one we want.
        if target is None or target.player_id != player_id:
            return False

        # Unlink from every level where this node appears.
        for i in range(self.level):
            if update[i].forward[i] is not target:
                break
            update[i].forward[i] = target.forward[i]

        # Shrink self.level if top levels are now empty.
        while self.level > 1 and self.head.forward[self.level - 1] is None:
            self.level -= 1

        self.size -= 1
        return True

    def get_rank(self, player_id: str, score: float, timestamp: float) -> Optional[int]:
        """
        Return the 1-based rank of a node.
        Rank 1 = highest score (or earliest timestamp on a tie).
        Returns None if the node is not in the list.
        
        We walk the full level-0 linked list to accurately count all nodes.
        This ensures nodes that appear only at level 0 are counted correctly.
        """
        rank    = 0
        current = self.head.forward[0]

        # Walk level 0 (the complete sorted linked list)
        while current is not None:
            if self._comes_before(current, score, timestamp):
                # This node comes before our target, count it
                rank += 1
                current = current.forward[0]
            elif current.player_id == player_id:
                # Found our target node
                return rank + 1   # convert 0-based count to 1-based rank
            else:
                # We've passed the insertion point without finding our target
                return None

        return None

    def get_top(self, k: int, offset: int = 0) -> list[SkipNode]:
        """
        Return up to `k` nodes starting at position `offset` (0-based).
        Walks the level-0 linked list — O(offset + k).
        """
        results = []
        current = self.head.forward[0]

        # Skip `offset` nodes
        skipped = 0
        while current is not None and skipped < offset:
            current = current.forward[0]
            skipped += 1

        # Collect `k` nodes
        while current is not None and len(results) < k:
            results.append(current)
            current = current.forward[0]

        return results

    def get_range(self, from_rank: int, to_rank: int) -> list[SkipNode]:
        """
        Return nodes whose rank falls in [from_rank, to_rank] (1-based, inclusive).
        O(from_rank + (to_rank - from_rank)) = O(to_rank).
        """
        count  = to_rank - from_rank + 1
        offset = from_rank - 1
        return self.get_top(count, offset)

    def iter_all(self):
        """Generator — yield every node in rank order (level-0 walk). O(n)."""
        current = self.head.forward[0]
        while current is not None:
            yield current
            current = current.forward[0]

    def __len__(self) -> int:
        return self.size

    def __repr__(self) -> str:
        top5 = [(n.player_id, n.score) for n in self.get_top(5)]
        return f"SkipList(size={self.size}, levels={self.level}, top5={top5})"