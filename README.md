# Leaderboard API

A production-grade **real-time leaderboard REST API** built with FastAPI —
powered by a **skip list data structure implemented from scratch**, with no
Redis or external sorted-set library.

Built as a portfolio project to demonstrate: skip list internals, LRU caching,
system design thinking, and the ability to ship a deployable service.

---

## Why a skip list?

A skip list is a probabilistic layered linked list that achieves O(log n)
insert, delete, and rank queries — the same complexity as a balanced BST —
but is dramatically simpler to implement and extend.

```
level 3:  HEAD ──────────────────────────────────────→ [bob:900]  → None
level 2:  HEAD ─────────────→ [carol:650] ───────────→ [bob:900]  → None
level 1:  HEAD → [alice:500] → [carol:650] ──────────→ [bob:900]  → None
level 0:  HEAD → [alice:500] → [carol:650] ──────────→ [bob:900]  → None
                  rank 3         rank 2                  rank 1
```

| Operation   | Skip List   | Sorted Array | Balanced BST |
|-------------|-------------|--------------|--------------|
| Insert      | O(log n)    | O(n)         | O(log n)     |
| Delete      | O(log n)    | O(n)         | O(log n)     |
| Rank query  | O(log n)    | O(log n)     | O(log n)     |
| Top-K       | O(k)        | O(k)         | O(k)         |
| Implement   | Simple      | Trivial      | Hard         |

Skip list wins on **implementability** and **concurrency extension** — adding
per-node locks for concurrent access is straightforward; doing the same for a
red-black tree is notoriously difficult.

---

## Architecture

```
┌─ FastAPI (/v1) ────────────────────────────────────────────────┐
│  POST /leaderboards          GET /leaderboards/{id}/top        │
│  GET  /leaderboards/{id}     GET /leaderboards/{id}/rankings   │
│  POST /leaderboards/{id}/scores          ... and 11 more       │
└────────────────────────┬───────────────────────────────────────┘
                         │
┌─ LeaderboardStore ──────▼──────────────────────────────────────┐
│                                                                 │
│  players: dict[player_id → PlayerEntry]   ← O(1) score lookup  │
│  skip_list: SkipList                      ← O(log n) ranking   │
│  cache: LRUCache(256)                     ← hot-path reads      │
│                                                                 │
│  cache is invalidated on every write; all reads hit cache      │
│  after the first miss → 100x+ speedup on repeated top-K calls  │
└─────────────────────────────────────────────────────────────────┘
```

**Measured cache speedup** (from `bench.py`):

| n players | top-100 cache MISS | top-100 cache HIT | speedup |
|-----------|-------------------|-------------------|---------|
| 10,000    | ~315 µs           | ~2.8 µs           | **114x** |
| 100,000   | ~340 µs           | ~2.8 µs           | **123x** |

---

## Project structure

```
leaderboard-api/
├── app/
│   ├── main.py               # FastAPI app, middleware, exception handlers
│   ├── api/
│   │   ├── leaderboards.py   # 6 leaderboard CRUD + reset endpoints
│   │   └── scores.py         # 9 score + rank + search endpoints
│   ├── core/
│   │   ├── skip_list.py      # Skip list — insert/delete/rank in O(log n)
│   │   ├── lru_cache.py      # LRU cache — get/put in O(1)
│   │   └── store.py          # Business logic layer wiring both together
│   ├── models/
│   │   └── schemas.py        # 17 Pydantic request/response models
│   └── tests/
│       ├── test_skip_list.py # Unit tests for the skip list
│       └── test_api.py       # Integration tests via FastAPI TestClient
├── benchmarks/
│   └── bench.py              # Skip list + LRU cache + store benchmarks
├── Dockerfile                # Multi-stage build, non-root user, healthcheck
├── railway.toml              # One-click Railway deploy config
├── requirements.txt
└── README.md
```

---

## Endpoints

### Leaderboard management

| Method   | Path                              | Description                        |
|----------|-----------------------------------|------------------------------------|
| `POST`   | `/v1/leaderboards`                | Create a leaderboard               |
| `GET`    | `/v1/leaderboards`                | List all leaderboards + stats      |
| `GET`    | `/v1/leaderboards/{id}`           | Get one leaderboard + live stats   |
| `PATCH`  | `/v1/leaderboards/{id}`           | Update max_entries / reset_policy  |
| `DELETE` | `/v1/leaderboards/{id}`           | Delete leaderboard and all scores  |
| `POST`   | `/v1/leaderboards/{id}/reset`     | Wipe scores, keep the board        |

### Scores & rankings

| Method   | Path                                        | Description                        |
|----------|---------------------------------------------|------------------------------------|
| `POST`   | `/v1/leaderboards/{id}/scores`              | Submit or update a score           |
| `POST`   | `/v1/leaderboards/{id}/scores/bulk`         | Bulk submit up to 500 scores       |
| `POST`   | `/v1/leaderboards/{id}/scores/increment`    | Add/subtract delta from score      |
| `DELETE` | `/v1/leaderboards/{id}/scores/{player_id}`  | Remove a player                    |
| `DELETE` | `/v1/leaderboards/{id}/scores`              | Bulk remove up to 500 players      |
| `GET`    | `/v1/leaderboards/{id}/top`                 | Top K players                      |
| `GET`    | `/v1/leaderboards/{id}/rankings`            | Paginated full rankings            |
| `GET`    | `/v1/leaderboards/{id}/range`               | Players between rank X and rank Y  |
| `GET`    | `/v1/leaderboards/{id}/players/{id}/rank`   | Player rank + optional nearby      |
| `GET`    | `/v1/leaderboards/{id}/search`              | Search by player prefix or score range |

---

## Quick start

```bash
# 1. Clone and install
git clone https://github.com/your-username/leaderboard-api
cd leaderboard-api
pip install -r requirements.txt

# 2. Run locally
uvicorn app.main:app --reload

# 3. Open the interactive API explorer
open http://localhost:8000/docs
```

### Example workflow

```bash
# Create a leaderboard
curl -X POST http://localhost:8000/v1/leaderboards \
  -H "Content-Type: application/json" \
  -d '{"name": "chess-world-cup", "order": "desc"}'
# → {"id": "lb_a1b2c3d4", ...}

# Submit scores
curl -X POST http://localhost:8000/v1/leaderboards/lb_a1b2c3d4/scores \
  -H "Content-Type: application/json" \
  -d '{"player_id": "alice", "score": 2850, "metadata": {"country": "IN"}}'
# → {"player_id": "alice", "score": 2850, "rank": 1, "percentile": 100.0, ...}

# Get the top 5
curl http://localhost:8000/v1/leaderboards/lb_a1b2c3d4/top?k=5

# Get alice's rank with 3 players above and below her
curl http://localhost:8000/v1/leaderboards/lb_a1b2c3d4/players/alice/rank?window=3

# Search everyone in the 2000–2999 rating band
curl "http://localhost:8000/v1/leaderboards/lb_a1b2c3d4/search?min_score=2000&max_score=2999"
```

---

## Run tests

```bash
pytest app/tests/ -v
```

---

## Run benchmarks

```bash
# Full benchmark (10k / 100k / 1M players)
python benchmarks/bench.py

# Quick run — skip 1M (faster CI)
python benchmarks/bench.py --quick

# Save results to CSV for plotting
python benchmarks/bench.py --csv
```

Sample output:

```
SKIP LIST  —  raw data structure
  n =     10,000
  Insert (total)          3.5 µs    286,000 ops/sec
  get_rank (avg)          2.8 µs    352,000 ops/sec
  get_top_100 (avg)       4.8 µs    209,000 ops/sec
  Update (avg)            6.6 µs    151,000 ops/sec

CACHE SPEEDUP  —  top100 miss vs hit
  n=  10,000   miss=315µs   hit=2.8µs   speedup=114x
  n= 100,000   miss=340µs   hit=2.8µs   speedup=123x
```

---

## Deploy to Railway

```bash
# 1. Push to GitHub
git add . && git commit -m "initial commit" && git push

# 2. Go to railway.app → New Project → Deploy from GitHub repo
# 3. Select this repo — Railway auto-detects the Dockerfile
# 4. Done. Your API is live at https://your-app.railway.app/docs
```

Railway injects `$PORT` automatically. The `railway.toml` handles everything else.

---

## Scaling to production (interview talking points)

This service is intentionally **single-process and in-memory** for simplicity.
Here is how you would scale it:

**Data layer:**
- Swap `LeaderboardStore` for a Redis adapter — `ZADD`/`ZRANK`/`ZRANGE` are
  drop-in equivalents of the skip list operations. Only `store.py` changes;
  the API layer is untouched.
- Add Postgres for persistent score history and metadata.

**Compute layer:**
- Run multiple uvicorn workers behind a load balancer (Nginx / Railway replicas).
- Since state moves to Redis, workers are stateless and can scale horizontally.

**Real-time:**
- Add a WebSocket endpoint that pushes rank changes to connected clients.
- Use Redis pub/sub to fan out updates across workers.

**Observability:**
- The `X-Process-Time-Ms` response header is already wired in `main.py`.
- Plug in OpenTelemetry or Prometheus for structured metrics.

---

## Design decisions

**Why not use Redis sorted sets directly?**
Building the skip list from scratch demonstrates understanding of the underlying
data structure. In a real system you would use Redis — but a recruiter can see
*why* Redis sorted sets are fast, not just that they exist.

**Why in-memory instead of a database?**
Keeps the project self-contained and deployable in one click. The store layer
is deliberately isolated behind a clean interface so swapping backends is a
one-file change.

**Why LRU cache over a simple dict?**
A plain dict grows unbounded. The LRU cache caps memory usage and evicts stale
entries automatically. The `invalidate_prefix` method ensures correctness —
every write flushes all cached reads for that leaderboard.

---

## License

MIT