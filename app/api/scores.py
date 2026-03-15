"""
Score and rank endpoints.

POST   /v1/leaderboards/{lb_id}/scores                       Submit or update a score
POST   /v1/leaderboards/{lb_id}/scores/bulk                  Bulk submit up to 500 scores
POST   /v1/leaderboards/{lb_id}/scores/increment             Increment/decrement a player's score
DELETE /v1/leaderboards/{lb_id}/scores/{player_id}           Remove a player
DELETE /v1/leaderboards/{lb_id}/scores                       Bulk delete players

GET    /v1/leaderboards/{lb_id}/top                          Top K players (simple)
GET    /v1/leaderboards/{lb_id}/rankings                     Paginated full rankings
GET    /v1/leaderboards/{lb_id}/range                        Players between rank X and rank Y
GET    /v1/leaderboards/{lb_id}/players/{player_id}/rank     A player's rank + optional neighbours
GET    /v1/leaderboards/{lb_id}/search                       Search by player_id prefix or score range
"""

from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Query
from typing import Optional

from app.models.schemas import (
    SubmitScoreRequest,
    BulkSubmitRequest,
    BulkSubmitResult,
    IncrementScoreRequest,
    ScoreResponse,
    PlayerRankResponse,
    RankEntry,
    PaginatedRankResponse,
    BulkDeleteRequest,
    BulkDeleteResult,
    ErrorResponse,
)
from app.core.store import store, _dt

router = APIRouter(tags=["Scores & Rankings"])


# ── Shared helpers ────────────────────────────────────────────────────────────

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


def _player_or_404(lb, player_id: str):
    if player_id not in lb.players:
        raise HTTPException(
            status_code=404,
            detail={"error": "player_not_found",
                    "message": f"Player '{player_id}' is not on leaderboard '{lb.id}'",
                    "status": 404},
        )


def _build_score_response(lb, player_id: str) -> ScoreResponse:
    entry = lb.players[player_id]
    rank  = store.get_rank(lb, player_id)
    total = lb.skip_list.size
    pct   = store._percentile(rank, total)
    return ScoreResponse(
        player_id=player_id,
        score=entry.score,
        rank=rank,
        percentile=pct,
        total_players=total,
        metadata=entry.metadata,
    )


def _to_rank_entry(raw: dict) -> RankEntry:
    return RankEntry(
        rank=raw["rank"],
        player_id=raw["player_id"],
        score=raw["score"],
        percentile=raw.get("percentile", 0.0),
        metadata=raw.get("metadata", {}),
        updated_at=datetime.fromisoformat(raw["updated_at"]) if raw.get("updated_at") else None,
    )


# ── Score submission ──────────────────────────────────────────────────────────

@router.post(
    "/leaderboards/{lb_id}/scores",
    response_model=ScoreResponse,
    summary="Submit or update a score",
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def submit_score(lb_id: str, body: SubmitScoreRequest):
    """
    Insert a new player or overwrite an existing player's score.

    - If the player already exists their score is atomically replaced
      (old entry removed from skip list, new one inserted) in **O(log n)**.
    - Returns the player's new rank and percentile immediately.
    - `metadata` is freeform JSON stored alongside the score — put display
      names, avatars, country codes, or anything else here.
    """
    lb = _get_or_404(lb_id)
    store.upsert_score(lb, body.player_id, body.score, body.metadata)
    return _build_score_response(lb, body.player_id)


@router.post(
    "/leaderboards/{lb_id}/scores/bulk",
    response_model=BulkSubmitResult,
    summary="Bulk submit up to 500 scores",
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def bulk_submit_scores(lb_id: str, body: BulkSubmitRequest):
    """
    Submit up to **500** scores in a single request.

    Each entry is processed independently — one bad entry does not abort the rest.
    Inspect the `errors` field in the response for any per-entry failures.
    Useful for batch imports or syncing scores from an external system.
    """
    lb = _get_or_404(lb_id)
    submitted, failed, errors = store.bulk_upsert(lb, body.entries)
    return BulkSubmitResult(submitted=submitted, failed=failed, errors=errors)


@router.post(
    "/leaderboards/{lb_id}/scores/increment",
    response_model=ScoreResponse,
    summary="Increment or decrement a player's score",
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def increment_score(lb_id: str, body: IncrementScoreRequest):
    """
    Add `delta` to the player's current score.
    If the player does not yet exist they are created with score = delta.

    - Positive delta → score goes up (player climbs the board)
    - Negative delta → score goes down (player falls)

    Useful for incremental event systems (e.g. "+10 for each match won")
    rather than absolute score systems.
    """
    lb = _get_or_404(lb_id)
    store.increment_score(lb, body.player_id, body.delta, body.metadata)
    return _build_score_response(lb, body.player_id)


# ── Player removal ────────────────────────────────────────────────────────────

@router.delete(
    "/leaderboards/{lb_id}/scores/{player_id}",
    summary="Remove a player",
    responses={404: {"model": ErrorResponse}},
)
def remove_player(lb_id: str, player_id: str):
    """Remove a single player and their score from the leaderboard."""
    lb = _get_or_404(lb_id)
    _player_or_404(lb, player_id)
    store.remove_player(lb, player_id)
    return {"player_id": player_id, "removed": True}


@router.delete(
    "/leaderboards/{lb_id}/scores",
    response_model=BulkDeleteResult,
    summary="Bulk remove players",
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def bulk_remove_players(lb_id: str, body: BulkDeleteRequest):
    """
    Remove up to **500** players in one request.
    Returns how many were deleted and which IDs were not found.
    """
    lb = _get_or_404(lb_id)
    deleted, not_found = store.bulk_remove(lb, body.player_ids)
    return BulkDeleteResult(deleted=deleted, not_found=not_found)


# ── Rank queries ──────────────────────────────────────────────────────────────

@router.get(
    "/leaderboards/{lb_id}/top",
    response_model=list[RankEntry],
    summary="Get top K players",
    responses={404: {"model": ErrorResponse}},
)
def get_top(
    lb_id: str,
    k: int = Query(10, ge=1, le=1000, description="Number of players to return"),
    offset: int = Query(0, ge=0, description="Number of top entries to skip (for manual pagination)"),
):
    """
    Return the top `k` players starting from `offset`.

    Results are served from the LRU cache when the same (k, offset) pair is
    requested repeatedly — the cache is invalidated automatically on any score
    change, so you always get fresh data.
    """
    lb = _get_or_404(lb_id)
    return [_to_rank_entry(r) for r in store.get_top(lb, k, offset)]


@router.get(
    "/leaderboards/{lb_id}/rankings",
    response_model=PaginatedRankResponse,
    summary="Paginated full rankings",
    responses={404: {"model": ErrorResponse}},
)
def get_rankings(
    lb_id: str,
    page: int = Query(1, ge=1, description="1-indexed page number"),
    page_size: int = Query(20, ge=1, le=200, description="Entries per page"),
):
    """
    Cursor-free page-based pagination over the full leaderboard.

    Use this endpoint to power a browse-all-rankings UI.
    The response includes `has_next` and `has_prev` flags for easy navigation.
    """
    lb = _get_or_404(lb_id)
    raw = store.get_page(lb, page, page_size)
    return PaginatedRankResponse(
        items=[_to_rank_entry(r) for r in raw["items"]],
        total=raw["total"],
        page=raw["page"],
        page_size=raw["page_size"],
        has_next=raw["has_next"],
        has_prev=raw["has_prev"],
    )


@router.get(
    "/leaderboards/{lb_id}/range",
    response_model=list[RankEntry],
    summary="Get players between two ranks",
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def get_range(
    lb_id: str,
    from_rank: int = Query(1, ge=1, description="Starting rank (inclusive)"),
    to_rank: int = Query(10, ge=1, description="Ending rank (inclusive)"),
):
    """
    Return every player whose rank falls between `from_rank` and `to_rank` (both inclusive).

    Useful for "show ranks 51–100" or "who finished on the podium (1–3)?".
    Backed directly by the skip list's level-0 linked list — **O(to_rank − from_rank)**.
    """
    lb = _get_or_404(lb_id)
    if from_rank > to_rank:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_range",
                    "message": f"from_rank ({from_rank}) must be ≤ to_rank ({to_rank})",
                    "status": 422},
        )
    total = lb.skip_list.size
    if from_rank > total:
        return []
    clamped_to = min(to_rank, total)
    return [_to_rank_entry(r) for r in store.get_range(lb, from_rank, clamped_to)]


@router.get(
    "/leaderboards/{lb_id}/players/{player_id}/rank",
    response_model=PlayerRankResponse,
    summary="Get a player's rank",
    responses={404: {"model": ErrorResponse}},
)
def get_player_rank(
    lb_id: str,
    player_id: str,
    window: int = Query(
        0, ge=0, le=50,
        description="Include `window` players above and below in a 'nearby' list"
    ),
):
    """
    Look up a single player's current rank, score, and percentile.

    Set `window > 0` to also receive the players immediately around them —
    ideal for a "you are rank 42, here's who's near you" UI widget.
    The nearby list is served from the skip list in **O(window)**.
    """
    lb = _get_or_404(lb_id)
    _player_or_404(lb, player_id)
    raw = store.get_player(lb, player_id, window)
    nearby = None
    if raw.get("nearby"):
        nearby = [_to_rank_entry(r) for r in raw["nearby"]]
    return PlayerRankResponse(
        player_id=raw["player_id"],
        rank=raw["rank"],
        score=raw["score"],
        percentile=raw["percentile"],
        total_players=raw["total_players"],
        metadata=raw["metadata"],
        nearby=nearby,
        updated_at=datetime.fromisoformat(raw["updated_at"]) if raw.get("updated_at") else None,
    )


# ── Search ────────────────────────────────────────────────────────────────────

@router.get(
    "/leaderboards/{lb_id}/search",
    summary="Search players by ID prefix or score range",
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def search(
    lb_id: str,
    player_prefix: Optional[str] = Query(
        None, min_length=1, max_length=64,
        description="Return players whose IDs start with this string"
    ),
    min_score: Optional[float] = Query(None, description="Lower bound of score range (inclusive)"),
    max_score: Optional[float] = Query(None, description="Upper bound of score range (inclusive)"),
    limit: int = Query(50, ge=1, le=500, description="Maximum results to return"),
):
    """
    Two search modes — supply exactly one:

    **Player ID prefix search** (`player_prefix`):
    Finds all players whose `player_id` starts with the given string.
    Useful for autocomplete or finding a user when you only have a partial ID.

    **Score range search** (`min_score` + `max_score`):
    Returns every player whose score falls within [min_score, max_score].
    Useful for "show me everyone in the Bronze tier (1000–2999 points)".

    Results are sorted by rank ascending in both modes.
    """
    lb = _get_or_404(lb_id)

    using_prefix = player_prefix is not None
    using_range  = min_score is not None or max_score is not None

    if using_prefix and using_range:
        raise HTTPException(
            status_code=422,
            detail={"error": "ambiguous_search",
                    "message": "Provide either player_prefix or (min_score + max_score), not both.",
                    "status": 422},
        )

    if not using_prefix and not using_range:
        raise HTTPException(
            status_code=422,
            detail={"error": "missing_search_param",
                    "message": "Provide player_prefix or both min_score and max_score.",
                    "status": 422},
        )

    if using_range:
        if min_score is None or max_score is None:
            raise HTTPException(
                status_code=422,
                detail={"error": "incomplete_score_range",
                        "message": "Both min_score and max_score are required for a score range search.",
                        "status": 422},
            )
        if min_score > max_score:
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_score_range",
                        "message": f"min_score ({min_score}) must be ≤ max_score ({max_score})",
                        "status": 422},
            )
        results = store.search_by_score_range(lb, min_score, max_score, limit)
        return [_to_rank_entry(r) for r in results]

    # prefix search
    results = store.search_by_player_prefix(lb, player_prefix, limit)
    return results