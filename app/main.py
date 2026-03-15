"""
main.py — FastAPI application entry point

Responsibilities
────────────────
  1. Construct the FastAPI app with full OpenAPI metadata
  2. Register middleware (CORS, request logging, process-time header)
  3. Register global exception handlers (validation errors, 404s, 500s)
  4. Mount routers under /v1
  5. Expose /health and /debug/cache endpoints

Running locally
───────────────
  uvicorn app.main:app --reload
  → API explorer: http://localhost:8000/docs
  → ReDoc:        http://localhost:8000/redoc

Deploying
─────────
  Docker + Railway:  see Dockerfile and railway.toml
  The app is fully stateless per-process — horizontal scaling requires
  replacing LeaderboardStore with a shared Redis backend.
"""

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import leaderboards, scores
from app.core.store import store

logger = logging.getLogger("leaderboard_api")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)


# ── Lifespan (startup / shutdown) ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Code before `yield` runs on startup.
    Code after `yield` runs on shutdown.
    """
    logger.info("Leaderboard API starting up")
    yield
    logger.info(
        "Leaderboard API shutting down — %d leaderboard(s) in memory",
        len(store.list_all()),
    )


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Leaderboard API",
    description="""
A **real-time leaderboard service** powered by a skip list built from scratch —
no Redis, no external sorted-set library.

## Data structures under the hood

| Layer | Structure | Purpose |
|-------|-----------|---------|
| Ranking | **Skip List** | O(log n) insert · delete · rank |
| Lookup  | **Hash Map**  | O(1) score lookup by player ID |
| Hot reads | **LRU Cache** | Caches topK / range results, invalidated on every write |

## Quick start

```bash
# Create a leaderboard
curl -X POST /v1/leaderboards -d '{"name": "my-game"}'

# Submit a score
curl -X POST /v1/leaderboards/{id}/scores \\
     -d '{"player_id": "alice", "score": 4200}'

# Get the top 10
curl /v1/leaderboards/{id}/top?k=10
```

## Scaling to production

Swap `LeaderboardStore` for a Redis adapter — the API layer is unchanged:
- `skip_list` → `ZADD` / `ZRANK` / `ZRANGE`
- `players` dict → `HSET`
- LRU cache → Redis TTL keys or a local process cache
""",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_tags=[
        {
            "name": "Leaderboards",
            "description": "Create, read, update, delete leaderboards and reset scores.",
        },
        {
            "name": "Scores & Rankings",
            "description": "Submit scores, query ranks, search players.",
        },
        {
            "name": "Health",
            "description": "Liveness and debug endpoints.",
        },
    ],
    contact={
        "name": "GitHub",
        "url": "https://github.com/your-username/leaderboard-api",
    },
    license_info={
        "name": "MIT",
    },
    lifespan=lifespan,
)


# ── Middleware ────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # tighten to specific origins in production
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    """
    Attach X-Process-Time (ms) to every response.
    Useful for latency monitoring and verifying that O(log n) ops are fast.
    """
    start    = time.perf_counter()
    response = await call_next(request)
    elapsed  = (time.perf_counter() - start) * 1000
    response.headers["X-Process-Time-Ms"] = f"{elapsed:.3f}"
    return response


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log method, path, and response status for every request."""
    response = await call_next(request)
    logger.info(
        "%s %s → %d",
        request.method,
        request.url.path,
        response.status_code,
    )
    return response


# ── Exception handlers ────────────────────────────────────────────────────────

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """
    Override FastAPI's default 422 response to match our ErrorResponse envelope.
    Surfaces each field-level error clearly so callers know exactly what to fix.
    """
    errors = []
    for e in exc.errors():
        errors.append({
            "field":   " → ".join(str(loc) for loc in e["loc"]),
            "message": e["msg"],
            "type":    e["type"],
        })
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error":   "validation_error",
            "message": "One or more fields failed validation.",
            "status":  422,
            "detail":  errors,
        },
    )


@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    return JSONResponse(
        status_code=404,
        content={
            "error":   "not_found",
            "message": f"No route matches {request.method} {request.url.path}",
            "status":  404,
            "detail":  None,
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """
    Catch-all for any unhandled exception.
    Logs the full traceback server-side; returns a safe message to the caller.
    """
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "error":   "internal_error",
            "message": "An unexpected error occurred. Check server logs.",
            "status":  500,
            "detail":  None,
        },
    )


# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(leaderboards.router, prefix="/v1")
app.include_router(scores.router,       prefix="/v1")


# ── Health & debug endpoints ──────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def root():
    """Redirect hint — not shown in docs."""
    return {"status": "ok", "docs": "/docs", "health": "/health"}


@app.get(
    "/health",
    tags=["Health"],
    summary="Liveness check",
    response_description="Returns 200 OK when the service is running.",
)
def health():
    """
    Simple liveness endpoint.
    Use this for Railway / Docker health checks and uptime monitors.
    """
    return {
        "status": "ok",
        "version": app.version,
    }


@app.get(
    "/debug/store",
    tags=["Health"],
    summary="In-memory store snapshot",
    response_description="Counts of leaderboards and players currently in memory.",
)
def debug_store():
    """
    Returns a lightweight snapshot of the in-memory state.

    Useful during development to confirm data is persisting across requests
    without having to query individual leaderboards.
    **Do not expose this endpoint in production.**
    """
    boards = store.list_all()
    return {
        "leaderboard_count": len(boards),
        "leaderboards": [
            {
                "id":            lb.id,
                "name":          lb.name,
                "player_count":  lb.skip_list.size,
                "cache_stats":   store.get_cache_stats(lb),
            }
            for lb in boards
        ],
    }