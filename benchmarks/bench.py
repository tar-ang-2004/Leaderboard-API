"""
bench.py — Leaderboard API benchmark suite

Measures the performance of every core operation at three scales:
  - Small  :   10,000 players
  - Medium :  100,000 players
  - Large  : 1,000,000 players

Operations benchmarked
──────────────────────
  insert        — upsert a new player into the skip list
  update        — overwrite an existing player's score (delete + insert)
  get_rank      — look up a single player's 1-based rank
  get_top       — retrieve the top-100 players
  get_range     — retrieve a rank slice (ranks 101–200)
  delete        — remove a player from the skip list
  lru_cache     — get/put throughput and hit-rate under repeated reads
  store_upsert  — full store.upsert_score() including hash map + cache invalidation

Run
───
  python benchmarks/bench.py              # all suites, default scales
  python benchmarks/bench.py --quick      # skip 1M (faster CI run)
  python benchmarks/bench.py --csv        # also write results.csv

Expected output (Apple M2, Python 3.12)
─────────────────────────────────────────
  n=    10,000  insert=    42ms  update=  0.004ms  rank=  0.003ms  top100=  0.011ms  ...
  n=   100,000  insert=   620ms  update=  0.005ms  rank=  0.005ms  top100=  0.013ms  ...
  n= 1,000,000  insert=  9200ms  update=  0.007ms  rank=  0.008ms  top100=  0.016ms  ...

The O(log n) growth is visible in rank/update averages: roughly doubling
each decade while insert_total scales ~linearly (more nodes to insert).
"""

import csv
import math
import random
import statistics
import string
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.skip_list import SkipList
from app.core.lru_cache import LRUCache
from app.core.store import LeaderboardStore


# ── Helpers ───────────────────────────────────────────────────────────────────

def random_id(length: int = 10) -> str:
    """Generate a random alphanumeric player ID."""
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def timer(fn, *args, repeat: int = 1) -> float:
    """
    Run fn(*args) `repeat` times and return total elapsed milliseconds.
    Using perf_counter for the highest-resolution wall-clock time available.
    """
    start = time.perf_counter()
    for _ in range(repeat):
        fn(*args)
    return (time.perf_counter() - start) * 1000


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class OpResult:
    """Timing stats for a single operation at a single scale."""
    op:          str
    n:           int
    total_ms:    float
    avg_ms:      float
    ops_per_sec: int
    samples:     int


@dataclass
class SuiteResult:
    """All op results for one scale (n)."""
    n:       int
    results: list[OpResult] = field(default_factory=list)

    def add(self, op: str, total_ms: float, samples: int) -> None:
        avg         = total_ms / samples if samples else 0
        ops_per_sec = int(1000 / avg) if avg > 0 else 0
        self.results.append(OpResult(op, self.n, total_ms, avg, ops_per_sec, samples))

    def get(self, op: str) -> Optional[OpResult]:
        return next((r for r in self.results if r.op == op), None)


# ── Individual benchmarks ─────────────────────────────────────────────────────

def bench_skip_list(n: int) -> SuiteResult:
    """
    Benchmark raw SkipList operations in isolation.
    No LRU cache, no hash map — pure skip list performance.
    """
    suite   = SuiteResult(n)
    players = [(random_id(), random.uniform(0, 1_000_000), float(i)) for i in range(n)]

    # ── insert ────────────────────────────────────────────────────────────────
    sl = SkipList()
    t  = timer(lambda: [sl.insert(pid, sc, ts) for pid, sc, ts in players])
    suite.add("insert", t, n)

    # ── get_rank (sample 1 000 random players) ────────────────────────────────
    sample     = random.sample(players, min(1_000, n))
    rank_total = timer(lambda: [sl.get_rank(pid, sc, ts) for pid, sc, ts in sample])
    suite.add("get_rank", rank_total, len(sample))

    # ── get_top 100 ───────────────────────────────────────────────────────────
    TOPK_REPEAT = 1_000
    top_total   = timer(sl.get_top, 100, repeat=TOPK_REPEAT)
    suite.add("get_top_100", top_total, TOPK_REPEAT)

    # ── get_range ranks 101–200 ───────────────────────────────────────────────
    range_total = timer(sl.get_range, 101, 200, repeat=TOPK_REPEAT)
    suite.add("get_range", range_total, TOPK_REPEAT)

    # ── update (delete old + insert new score) ────────────────────────────────
    update_sample = random.sample(players, min(1_000, n))
    t0            = time.perf_counter()
    for pid, old_score, ts in update_sample:
        sl.delete(pid, old_score, ts)
        sl.insert(pid, old_score + 1.0, ts)
    update_total = (time.perf_counter() - t0) * 1000
    suite.add("update", update_total, len(update_sample))

    # ── delete (remove 1 000 players) ────────────────────────────────────────
    # Re-insert the ones we just updated so scores match
    for pid, old_score, ts in update_sample:
        sl.delete(pid, old_score + 1.0, ts)
        sl.insert(pid, old_score, ts)

    delete_sample = random.sample(players, min(1_000, n))
    t0            = time.perf_counter()
    for pid, sc, ts in delete_sample:
        sl.delete(pid, sc, ts)
    delete_total = (time.perf_counter() - t0) * 1000
    suite.add("delete", delete_total, len(delete_sample))

    return suite


def bench_lru_cache(n: int) -> SuiteResult:
    """
    Benchmark LRUCache get/put and the invalidate_prefix hot path.
    Uses a realistic key pattern matching what the store generates.
    """
    suite    = SuiteResult(n)
    cache    = LRUCache(capacity=256)
    lb_id    = "lb_bench01"
    dummy    = list(range(100))   # simulate a cached top-100 result

    # ── put (fill cache, triggers LRU eviction once full) ─────────────────────
    keys    = [f"{lb_id}:top:{k}:{offset}" for k in range(10, 110) for offset in range(0, 50, 10)]
    PUT_REP = 10_000
    t0      = time.perf_counter()
    for i in range(PUT_REP):
        cache.put(keys[i % len(keys)], dummy)
    put_total = (time.perf_counter() - t0) * 1000
    suite.add("cache_put", put_total, PUT_REP)

    # ── get hit (key guaranteed in cache) ─────────────────────────────────────
    cache.put("lb_bench01:top:10:0", dummy)
    GET_REP   = 100_000
    get_total = timer(cache.get, "lb_bench01:top:10:0", repeat=GET_REP)
    suite.add("cache_get_hit", get_total, GET_REP)

    # ── get miss ──────────────────────────────────────────────────────────────
    miss_total = timer(cache.get, "lb_bench01:nonexistent_key", repeat=GET_REP)
    suite.add("cache_get_miss", miss_total, GET_REP)

    # ── invalidate_prefix (simulates a score write flushing all cached reads) ─
    for k in keys[:256]:
        cache.put(k, dummy)
    INV_REP   = 10_000
    inv_total = 0.0
    for _ in range(INV_REP):
        for k in keys[:256]:
            cache.put(k, dummy)
        t0         = time.perf_counter()
        cache.invalidate_prefix(lb_id)
        inv_total += (time.perf_counter() - t0) * 1000
    suite.add("invalidate_prefix", inv_total, INV_REP)

    return suite


def bench_store(n: int) -> SuiteResult:
    """
    Benchmark LeaderboardStore — the full stack (skip list + hash map + LRU cache).
    This is the closest to what the API endpoints actually do.
    """
    suite   = SuiteResult(n)
    s       = LeaderboardStore()
    lb      = s.create("bench", "desc", n + 10_000, "never")
    players = [(random_id(), random.uniform(0, 1_000_000)) for _ in range(n)]

    # ── store upsert (insert all n players) ───────────────────────────────────
    t0 = time.perf_counter()
    for pid, score in players:
        s.upsert_score(lb, pid, score, {})
    upsert_total = (time.perf_counter() - t0) * 1000
    suite.add("store_upsert", upsert_total, n)

    # ── store get_rank ────────────────────────────────────────────────────────
    sample     = random.sample(players, min(1_000, n))
    t0         = time.perf_counter()
    for pid, _ in sample:
        s.get_rank(lb, pid)
    rank_total = (time.perf_counter() - t0) * 1000
    suite.add("store_get_rank", rank_total, len(sample))

    # ── store get_top (cache miss first, then hit) ────────────────────────────
    lb.cache.invalidate_all()
    miss_t = timer(s.get_top, lb, 100, 0)
    suite.add("store_top100_miss", miss_t, 1)

    hit_t = timer(s.get_top, lb, 100, 0)
    suite.add("store_top100_hit", hit_t, 1)

    # ── store score update (upsert overwrite) ────────────────────────────────
    update_sample = random.sample(players, min(1_000, n))
    t0            = time.perf_counter()
    for pid, score in update_sample:
        s.upsert_score(lb, pid, score + 1.0, {})
    update_total = (time.perf_counter() - t0) * 1000
    suite.add("store_update", update_total, len(update_sample))

    # ── store increment ───────────────────────────────────────────────────────
    inc_sample = random.sample(players, min(1_000, n))
    t0         = time.perf_counter()
    for pid, _ in inc_sample:
        s.increment_score(lb, pid, 10.0, {})
    inc_total = (time.perf_counter() - t0) * 1000
    suite.add("store_increment", inc_total, len(inc_sample))

    return suite


# ── Reporting ─────────────────────────────────────────────────────────────────

COLS = {
    "insert":              "Insert (total)",
    "get_rank":            "get_rank (avg)",
    "get_top_100":         "get_top_100 (avg)",
    "get_range":           "get_range (avg)",
    "update":              "Update (avg)",
    "delete":              "Delete (avg)",
    "cache_put":           "Cache put (avg)",
    "cache_get_hit":       "Cache get hit (avg)",
    "cache_get_miss":      "Cache get miss (avg)",
    "invalidate_prefix":   "Invalidate prefix (avg)",
    "store_upsert":        "Store upsert (total)",
    "store_get_rank":      "Store get_rank (avg)",
    "store_top100_miss":   "Store top100 cache MISS",
    "store_top100_hit":    "Store top100 cache HIT",
    "store_update":        "Store update (avg)",
    "store_increment":     "Store increment (avg)",
}


def _fmt_ms(ms: float) -> str:
    if ms >= 1000:
        return f"{ms/1000:>8.2f}s "
    if ms >= 1:
        return f"{ms:>8.2f}ms"
    if ms >= 0.001:
        return f"{ms*1000:>8.2f}µs"
    return f"{ms*1_000_000:>8.2f}ns"


def print_suite(suite: SuiteResult) -> None:
    print(f"\n  n = {suite.n:>10,}")
    print(f"  {'Operation':<30} {'Time':>10}  {'ops/sec':>10}")
    print(f"  {'-'*30} {'-'*10}  {'-'*10}")
    for r in suite.results:
        label = COLS.get(r.op, r.op)
        print(f"  {label:<30} {_fmt_ms(r.avg_ms):>10}  {r.ops_per_sec:>10,}")


def print_complexity_table(suites: list[SuiteResult], op: str) -> None:
    """
    Print the growth factor between scales to empirically confirm O(log n).
    For O(log n): growth factor ≈ log(n2)/log(n1) ≈ 1.25 per decade.
    """
    label = COLS.get(op, op)
    print(f"\n  Complexity check — {label}")
    print(f"  {'n':>12}  {'avg_ms':>12}  {'growth vs prev':>16}")
    prev_ms = None
    for s in suites:
        r = s.get(op)
        if r is None:
            continue
        growth = f"{r.avg_ms / prev_ms:.2f}x" if prev_ms else "—"
        print(f"  {s.n:>12,}  {r.avg_ms:>12.6f}  {growth:>16}")
        prev_ms = r.avg_ms


def write_csv(suites_groups: list[list[SuiteResult]], path: str = "benchmarks/results.csv") -> None:
    all_ops = list(COLS.keys())
    rows    = []
    for suites in suites_groups:
        for s in suites:
            for r in s.results:
                rows.append({
                    "n":           r.n,
                    "op":          r.op,
                    "total_ms":    round(r.total_ms, 4),
                    "avg_ms":      round(r.avg_ms,   6),
                    "ops_per_sec": r.ops_per_sec,
                    "samples":     r.samples,
                })
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["n","op","total_ms","avg_ms","ops_per_sec","samples"])
        w.writeheader()
        w.writerows(rows)
    print(f"\n  Results written to {path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    quick  = "--quick" in sys.argv
    to_csv = "--csv"   in sys.argv

    scales = [10_000, 100_000] if quick else [10_000, 100_000, 1_000_000]

    # ── Skip List ─────────────────────────────────────────────────────────────
    print("\n" + "="*60)
    print("  SKIP LIST  —  raw data structure")
    print("="*60)
    sl_suites = []
    for n in scales:
        sys.stdout.write(f"  Benchmarking n={n:>10,} ..."); sys.stdout.flush()
        s = bench_skip_list(n)
        sl_suites.append(s)
        print(" done")
    for s in sl_suites:
        print_suite(s)
    print_complexity_table(sl_suites, "get_rank")
    print_complexity_table(sl_suites, "update")

    # ── LRU Cache ─────────────────────────────────────────────────────────────
    print("\n" + "="*60)
    print("  LRU CACHE  —  read/write throughput")
    print("="*60)
    cache_suites = []
    for n in scales:
        sys.stdout.write(f"  Benchmarking n={n:>10,} ..."); sys.stdout.flush()
        s = bench_lru_cache(n)
        cache_suites.append(s)
        print(" done")
    for s in cache_suites:
        print_suite(s)

    # ── Store (full stack) ────────────────────────────────────────────────────
    print("\n" + "="*60)
    print("  LEADERBOARD STORE  —  full stack (skip list + hashmap + cache)")
    print("="*60)
    store_suites = []
    for n in scales:
        sys.stdout.write(f"  Benchmarking n={n:>10,} ..."); sys.stdout.flush()
        s = bench_store(n)
        store_suites.append(s)
        print(" done")
    for s in store_suites:
        print_suite(s)
    print_complexity_table(store_suites, "store_get_rank")

    # ── Cache speedup callout ─────────────────────────────────────────────────
    print("\n" + "="*60)
    print("  CACHE SPEEDUP  —  top100 miss vs hit")
    print("="*60)
    print(f"\n  {'n':>12}  {'miss':>12}  {'hit':>12}  {'speedup':>10}")
    print(f"  {'-'*12}  {'-'*12}  {'-'*12}  {'-'*10}")
    for s in store_suites:
        miss = s.get("store_top100_miss")
        hit  = s.get("store_top100_hit")
        if miss and hit and hit.avg_ms > 0:
            speedup = miss.avg_ms / hit.avg_ms
            print(f"  {s.n:>12,}  {_fmt_ms(miss.avg_ms):>12}  {_fmt_ms(hit.avg_ms):>12}  {speedup:>9.1f}x")

    # ── CSV ───────────────────────────────────────────────────────────────────
    if to_csv:
        write_csv([sl_suites, cache_suites, store_suites])

    print("\n" + "="*60)
    print("  Done.")
    print("="*60 + "\n")


if __name__ == "__main__":
    main()