"""
lru_cache.py — LRU Cache

Least-Recently-Used eviction cache backed by Python's OrderedDict.

Why OrderedDict?
  dict preserves insertion order in Python 3.7+, but OrderedDict additionally
  exposes move_to_end() which lets us mark a key as "just used" in O(1).
  Combined with popitem(last=False) for O(1) eviction of the oldest entry,
  this gives a clean O(1) get and O(1) put with no extra linked list needed.

Internal layout:
  Least recently used  ←─────────────────────────────→  Most recently used
  _cache: [oldest_key, ..., recent_key, newest_key]

Every get() promotes the key to the right (most-recent) end.
Every put() appends to the right, then evicts from the left if over capacity.

Usage:
    cache = LRUCache(capacity=256)
    cache.put("top:10:0", result)
    data  = cache.get("top:10:0")   # → result  (promotes to MRU end)
    cache.invalidate_prefix("lb_abc123:")  # wipe all keys for one leaderboard
"""

from collections import OrderedDict
from typing import Any, Optional


class LRUCache:
    """
    Fixed-capacity LRU cache with O(1) get, put, and delete.

    Keys are strings. Values can be anything (lists, dicts, etc.).
    Thread-safety is NOT guaranteed — add a threading.RLock if you need it.

    Parameters
    ----------
    capacity : int
        Maximum number of entries before the oldest is evicted. Default 256.
    """

    def __init__(self, capacity: int = 256):
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        self.capacity = capacity
        self._cache: OrderedDict[str, Any] = OrderedDict()
        self.hits   = 0   # cache hit counter (useful for benchmarking)
        self.misses = 0   # cache miss counter

    # ── Core ops ──────────────────────────────────────────────────────────────

    def get(self, key: str) -> Optional[Any]:
        """
        Return the value for `key`, or None if not present.
        Moves the key to the MRU (most-recently-used) end on a hit.
        O(1).
        """
        if key not in self._cache:
            self.misses += 1
            return None
        self._cache.move_to_end(key)   # promote to MRU end
        self.hits += 1
        return self._cache[key]

    def put(self, key: str, value: Any) -> None:
        """
        Store a key-value pair.
        If key already exists its value is updated and it is promoted to MRU.
        If the cache is full the LRU (oldest) entry is evicted first.
        O(1).
        """
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = value
        if len(self._cache) > self.capacity:
            self._cache.popitem(last=False)   # evict LRU end

    def delete(self, key: str) -> bool:
        """
        Remove a single key. Returns True if it existed, False otherwise.
        O(1).
        """
        if key in self._cache:
            del self._cache[key]
            return True
        return False

    # ── Bulk invalidation ─────────────────────────────────────────────────────

    def invalidate_prefix(self, prefix: str) -> int:
        """
        Delete all keys that start with `prefix`.
        Used to flush all cached results for a single leaderboard when
        any score in that leaderboard changes.

        e.g. invalidate_prefix("lb_abc123:") removes:
              "lb_abc123:top:10:0"
              "lb_abc123:range:1:100"
              ...

        Returns the number of keys deleted. O(n) where n = cache size.
        """
        stale = [k for k in self._cache if k.startswith(prefix)]
        for k in stale:
            del self._cache[k]
        return len(stale)

    def invalidate_all(self) -> int:
        """Wipe the entire cache. Returns number of entries cleared."""
        count = len(self._cache)
        self._cache.clear()
        return count

    # ── Introspection ─────────────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self._cache)

    def __contains__(self, key: str) -> bool:
        return key in self._cache

    @property
    def hit_rate(self) -> float:
        """Cache hit rate as a float in [0.0, 1.0]. Returns 0.0 if never queried."""
        total = self.hits + self.misses
        return round(self.hits / total, 4) if total > 0 else 0.0

    def stats(self) -> dict:
        """Return a snapshot of cache health metrics."""
        return {
            "size":     len(self._cache),
            "capacity": self.capacity,
            "hits":     self.hits,
            "misses":   self.misses,
            "hit_rate": self.hit_rate,
        }

    def __repr__(self) -> str:
        return (
            f"LRUCache(size={len(self._cache)}/{self.capacity}, "
            f"hit_rate={self.hit_rate:.1%})"
        )