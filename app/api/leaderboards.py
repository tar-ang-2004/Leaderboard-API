"""
Leaderboard management endpoints.

POST   /v1/leaderboards                  Create a leaderboard
GET    /v1/leaderboards                  List all leaderboards
GET    /v1/leaderboards/{lb_id}          Get leaderboard detail + stats
PATCH  /v1/leaderboards/{lb_id}          Update leaderboard settings
DELETE /v1/leaderboards/{lb_id}          Delete leaderboard and all scores
POST   /v1/leaderboards/{lb_id}/reset    Wipe all scores (keep the board)
"""

from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException

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
    last_upd = stats_raw["last_updated"]
    return LeaderboardResponse(
        id=lb.id,
        name=lb.name,
        order=lb.order,
        max_entries=lb.max_entries,
        reset_policy=lb.reset_policy,
        created_at=datetime.fromtimestamp(lb.created_at, tz=timezone.utc),
        stats=LeaderboardStats(
            total_players=stats_raw["total_players"],
            highest_score=stats_raw["highest_score"],
            lowest_score=stats_raw["lowest_score"],
            average_score=stats_raw["average_score"],
            last_updated=datetime.fromisoformat(last_upd) if last_upd else None,
        ),
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post(
    "/leaderboards",
    response_model=LeaderboardResponse,
    status_code=201,
    summary="Create a leaderboard",
    responses={422: {"model": ErrorResponse}},
)
def create_leaderboard(body: CreateLeaderboardRequest):
    """
    Create a new named leaderboard.

    - **name**: URL-safe slug (lowercase letters, digits, hyphens, underscores)
    - **order**: `desc` (high score = rank 1) or `asc` (low score = rank 1)
    - **max_entries**: cap on stored players; once full the lowest-ranked entry is evicted
    - **reset_policy**: automatic wipe schedule — `never | daily | weekly | monthly`
    """
    lb = store.create(
        name=body.name,
        order=body.order,
        max_entries=body.max_entries,
        reset_policy=body.reset_policy,
    )
    return _to_response(lb)


@router.get(
    "/leaderboards",
    response_model=list[LeaderboardResponse],
    summary="List all leaderboards",
)
def list_leaderboards():
    """Return every leaderboard with its current stats."""
    return [_to_response(lb) for lb in store.list_all()]


@router.get(
    "/leaderboards/{lb_id}",
    response_model=LeaderboardResponse,
    summary="Get leaderboard details",
    responses={404: {"model": ErrorResponse}},
)
def get_leaderboard(lb_id: str):
    """Retrieve a leaderboard by ID along with live stats (player count, score distribution)."""
    lb = _get_or_404(lb_id)
    return _to_response(lb)


@router.patch(
    "/leaderboards/{lb_id}",
    response_model=LeaderboardResponse,
    summary="Update leaderboard settings",
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def update_leaderboard(lb_id: str, body: UpdateLeaderboardRequest):
    """
    Partially update a leaderboard's configuration.
    Only `max_entries` and `reset_policy` are mutable after creation.
    """
    lb = _get_or_404(lb_id)
    lb = store.update(lb, body.max_entries, body.reset_policy)
    return _to_response(lb)


@router.delete(
    "/leaderboards/{lb_id}",
    status_code=204,
    summary="Delete a leaderboard",
    responses={404: {"model": ErrorResponse}},
)
def delete_leaderboard(lb_id: str):
    """Permanently delete a leaderboard and all of its score data."""
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
    responses={404: {"model": ErrorResponse}},
)
def reset_leaderboard(lb_id: str):
    """
    Wipe every score from a leaderboard while keeping the leaderboard itself.
    Useful for starting a new season or round without losing the configuration.
    """
    lb = _get_or_404(lb_id)
    cleared = store.reset(lb)
    return ResetResponse(
        leaderboard_id=lb_id,
        players_cleared=cleared,
        reset_at=datetime.now(tz=timezone.utc),
    )