"""
Leaderboard management endpoints.

Rate limits (per IP):
  POST   /leaderboards         20/minute  — prevent board spam
  GET    /leaderboards         200/minute — reads are cheap
  GET    /leaderboards/{id}    200/minute
  PATCH  /leaderboards/{id}    20/minute
  DELETE /leaderboards/{id}    20/minute
  POST   /leaderboards/{id}/reset  10/minute — destructive op
"""

from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)

from app.models.schemas import (
    CreateLeaderboardRequest,
    UpdateLeaderboardRequest,
    LeaderboardResponse,
    LeaderboardStats,
    ResetResponse,
    ErrorResponse,
)
from app.core.store import store

router = APIRouter(tags=["Leaderboards"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_or_404(lb_id: str):
    lb = store.get(lb_id)
    if not lb:
        raise HTTPException(
            status_code=404,
            detail={"error": "leaderboard_not_found",
                    "message": f"No leaderboard with id '{lb_id}'",
                    "status": 404},
        )
    return lb


def _to_response(lb) -> LeaderboardResponse:
    stats_raw = store.get_stats(lb)
    last_upd  = stats_raw["last_updated"]
    return LeaderboardResponse(
        id           = lb.id,
        name         = lb.name,
        order        = lb.order,
        max_entries  = lb.max_entries,
        reset_policy = lb.reset_policy,
        created_at   = datetime.fromtimestamp(lb.created_at, tz=timezone.utc),
        stats=LeaderboardStats(
            total_players = stats_raw["total_players"],
            highest_score = stats_raw["highest_score"],
            lowest_score  = stats_raw["lowest_score"],
            average_score = stats_raw["average_score"],
            last_updated  = datetime.fromisoformat(last_upd) if last_upd else None,
        ),
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post(
    "/leaderboards",
    response_model=LeaderboardResponse,
    status_code=201,
    summary="Create a leaderboard",
    responses={422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
@limiter.limit("20/minute")
def create_leaderboard(request: Request, body: CreateLeaderboardRequest):
    """
    Create a new named leaderboard.

    - **name**: URL-safe slug (lowercase letters, digits, hyphens, underscores)
    - **order**: `desc` (high score = rank 1) or `asc` (low score = rank 1)
    - **max_entries**: cap on stored players
    - **reset_policy**: `never | daily | weekly | monthly`

    Rate limit: **20/minute per IP**
    """
    lb = store.create(
        name         = body.name,
        order        = body.order,
        max_entries  = body.max_entries,
        reset_policy = body.reset_policy,
    )
    return _to_response(lb)


@router.get(
    "/leaderboards",
    response_model=list[LeaderboardResponse],
    summary="List all leaderboards",
    responses={429: {"model": ErrorResponse}},
)
@limiter.limit("200/minute")
def list_leaderboards(request: Request):
    """Return every leaderboard with live stats. Rate limit: **200/minute per IP**"""
    return [_to_response(lb) for lb in store.list_all()]


@router.get(
    "/leaderboards/{lb_id}",
    response_model=LeaderboardResponse,
    summary="Get leaderboard details",
    responses={404: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
@limiter.limit("200/minute")
def get_leaderboard(request: Request, lb_id: str):
    """Retrieve a leaderboard by ID with live stats. Rate limit: **200/minute per IP**"""
    lb = _get_or_404(lb_id)
    return _to_response(lb)


@router.patch(
    "/leaderboards/{lb_id}",
    response_model=LeaderboardResponse,
    summary="Update leaderboard settings",
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
@limiter.limit("20/minute")
def update_leaderboard(request: Request, lb_id: str, body: UpdateLeaderboardRequest):
    """
    Partially update a leaderboard's configuration.
    Only `max_entries` and `reset_policy` are mutable after creation.
    Rate limit: **20/minute per IP**
    """
    lb = _get_or_404(lb_id)
    lb = store.update(lb, body.max_entries, body.reset_policy)
    return _to_response(lb)


@router.delete(
    "/leaderboards/{lb_id}",
    status_code=204,
    summary="Delete a leaderboard",
    responses={404: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
@limiter.limit("20/minute")
def delete_leaderboard(request: Request, lb_id: str):
    """Permanently delete a leaderboard and all its score data. Rate limit: **20/minute per IP**"""
    if not store.delete(lb_id):
        raise HTTPException(
            status_code=404,
            detail={"error": "leaderboard_not_found",
                    "message": f"No leaderboard with id '{lb_id}'",
                    "status": 404},
        )


@router.post(
    "/leaderboards/{lb_id}/reset",
    response_model=ResetResponse,
    summary="Reset all scores",
    responses={404: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
@limiter.limit("10/minute")
def reset_leaderboard(request: Request, lb_id: str):
    """
    Wipe every score while keeping the leaderboard itself.
    Rate limit: **10/minute per IP** — destructive operation.
    """
    lb = _get_or_404(lb_id)
    cleared = store.reset(lb)
    return ResetResponse(
        leaderboard_id  = lb_id,
        players_cleared = cleared,
        reset_at        = datetime.now(tz=timezone.utc),
    )