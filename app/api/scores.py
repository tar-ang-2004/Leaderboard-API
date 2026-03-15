"""
Score and rank endpoints.

Rate limits (per IP):
  Writes  (POST/DELETE scores)     : 120/minute  ~2/sec
  Reads   (GET top/rankings/range) : 200/minute  ~3/sec
  Search                           : 60/minute
"""

from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Query, Request
from typing import Optional
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)

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
        player_id     = player_id,
        score         = entry.score,
        rank          = rank,
        percentile    = pct,
        total_players = total,
        metadata      = entry.metadata,
    )


def _to_rank_entry(raw: dict) -> RankEntry:
    return RankEntry(
        rank       = raw["rank"],
        player_id  = raw["player_id"],
        score      = raw["score"],
        percentile = raw.get("percentile", 0.0),
        metadata   = raw.get("metadata", {}),
        updated_at = datetime.fromisoformat(raw["updated_at"]) if raw.get("updated_at") else None,
    )


# ── Score submission ──────────────────────────────────────────────────────────

@router.post(
    "/leaderboards/{lb_id}/scores",
    response_model=ScoreResponse,
    summary="Submit or update a score",
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
@limiter.limit("120/minute")
def submit_score(request: Request, lb_id: str, body: SubmitScoreRequest):
    """
    Insert or overwrite a player's score in O(log n).
    Returns the player's new rank and percentile immediately.
    Rate limit: **120/minute per IP**
    """
    lb = _get_or_404(lb_id)
    store.upsert_score(lb, body.player_id, body.score, body.metadata)
    return _build_score_response(lb, body.player_id)


@router.post(
    "/leaderboards/{lb_id}/scores/bulk",
    response_model=BulkSubmitResult,
    summary="Bulk submit up to 500 scores",
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
@limiter.limit("30/minute")
def bulk_submit_scores(request: Request, lb_id: str, body: BulkSubmitRequest):
    """
    Submit up to 500 scores in one request.
    Each entry processed independently — one failure won't abort the rest.
    Rate limit: **30/minute per IP** (each call can contain 500 entries)
    """
    lb = _get_or_404(lb_id)
    submitted, failed, errors = store.bulk_upsert(lb, body.entries)
    return BulkSubmitResult(submitted=submitted, failed=failed, errors=errors)


@router.post(
    "/leaderboards/{lb_id}/scores/increment",
    response_model=ScoreResponse,
    summary="Increment or decrement a player's score",
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
@limiter.limit("120/minute")
def increment_score(request: Request, lb_id: str, body: IncrementScoreRequest):
    """
    Add `delta` to the player's current score.
    Player is created with score=delta if they don't exist yet.
    Rate limit: **120/minute per IP**
    """
    lb = _get_or_404(lb_id)
    store.increment_score(lb, body.player_id, body.delta, body.metadata)
    return _build_score_response(lb, body.player_id)


# ── Player removal ────────────────────────────────────────────────────────────

@router.delete(
    "/leaderboards/{lb_id}/scores/{player_id}",
    summary="Remove a player",
    responses={404: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
@limiter.limit("120/minute")
def remove_player(request: Request, lb_id: str, player_id: str):
    """Remove a single player from the leaderboard. Rate limit: **120/minute per IP**"""
    lb = _get_or_404(lb_id)
    _player_or_404(lb, player_id)
    store.remove_player(lb, player_id)
    return {"player_id": player_id, "removed": True}


@router.delete(
    "/leaderboards/{lb_id}/scores",
    response_model=BulkDeleteResult,
    summary="Bulk remove players",
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
@limiter.limit("30/minute")
def bulk_remove_players(request: Request, lb_id: str, body: BulkDeleteRequest):
    """
    Remove up to 500 players in one request.
    Rate limit: **30/minute per IP**
    """
    lb = _get_or_404(lb_id)
    deleted, not_found = store.bulk_remove(lb, body.player_ids)
    return BulkDeleteResult(deleted=deleted, not_found=not_found)


# ── Rank queries ──────────────────────────────────────────────────────────────

@router.get(
    "/leaderboards/{lb_id}/top",
    response_model=list[RankEntry],
    summary="Get top K players",
    responses={404: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
@limiter.limit("200/minute")
def get_top(
    request: Request,
    lb_id: str,
    k: int = Query(10, ge=1, le=1000, description="Number of players to return"),
    offset: int = Query(0, ge=0, description="Number of top entries to skip"),
):
    """
    Return top `k` players. Results served from LRU cache on repeated calls.
    Rate limit: **200/minute per IP**
    """
    lb = _get_or_404(lb_id)
    return [_to_rank_entry(r) for r in store.get_top(lb, k, offset)]


@router.get(
    "/leaderboards/{lb_id}/rankings",
    response_model=PaginatedRankResponse,
    summary="Paginated full rankings",
    responses={404: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
@limiter.limit("200/minute")
def get_rankings(
    request: Request,
    lb_id: str,
    page: int = Query(1, ge=1, description="1-indexed page number"),
    page_size: int = Query(20, ge=1, le=200, description="Entries per page"),
):
    """
    Page-based pagination over the full leaderboard.
    Rate limit: **200/minute per IP**
    """
    lb = _get_or_404(lb_id)
    raw = store.get_page(lb, page, page_size)
    return PaginatedRankResponse(
        items     = [_to_rank_entry(r) for r in raw["items"]],
        total     = raw["total"],
        page      = raw["page"],
        page_size = raw["page_size"],
        has_next  = raw["has_next"],
        has_prev  = raw["has_prev"],
    )


@router.get(
    "/leaderboards/{lb_id}/range",
    response_model=list[RankEntry],
    summary="Get players between two ranks",
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
@limiter.limit("200/minute")
def get_range(
    request: Request,
    lb_id: str,
    from_rank: int = Query(1, ge=1, description="Starting rank (inclusive)"),
    to_rank: int   = Query(10, ge=1, description="Ending rank (inclusive)"),
):
    """
    Return players with rank in [from_rank, to_rank].
    Rate limit: **200/minute per IP**
    """
    lb = _get_or_404(lb_id)
    if from_rank > to_rank:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_range",
                    "message": f"from_rank ({from_rank}) must be ≤ to_rank ({to_rank})",
                    "status": 422},
        )
    total      = lb.skip_list.size
    if from_rank > total:
        return []
    clamped_to = min(to_rank, total)
    return [_to_rank_entry(r) for r in store.get_range(lb, from_rank, clamped_to)]


@router.get(
    "/leaderboards/{lb_id}/players/{player_id}/rank",
    response_model=PlayerRankResponse,
    summary="Get a player's rank",
    responses={404: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
@limiter.limit("200/minute")
def get_player_rank(
    request: Request,
    lb_id: str,
    player_id: str,
    window: int = Query(0, ge=0, le=50, description="Players above/below to include"),
):
    """
    Look up a player's rank, score, and percentile.
    Set `window > 0` for a 'nearby players' list.
    Rate limit: **200/minute per IP**
    """
    lb = _get_or_404(lb_id)
    _player_or_404(lb, player_id)
    raw    = store.get_player(lb, player_id, window)
    nearby = None
    if raw.get("nearby"):
        nearby = [_to_rank_entry(r) for r in raw["nearby"]]
    return PlayerRankResponse(
        player_id     = raw["player_id"],
        rank          = raw["rank"],
        score         = raw["score"],
        percentile    = raw["percentile"],
        total_players = raw["total_players"],
        metadata      = raw["metadata"],
        nearby        = nearby,
        updated_at    = datetime.fromisoformat(raw["updated_at"]) if raw.get("updated_at") else None,
    )


# ── Search ────────────────────────────────────────────────────────────────────

@router.get(
    "/leaderboards/{lb_id}/search",
    summary="Search players by ID prefix or score range",
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
@limiter.limit("60/minute")
def search(
    request: Request,
    lb_id: str,
    player_prefix: Optional[str] = Query(None, min_length=1, max_length=64),
    min_score:     Optional[float] = Query(None),
    max_score:     Optional[float] = Query(None),
    limit:         int = Query(50, ge=1, le=500),
):
    """
    Search by player ID prefix OR score range (not both).
    Rate limit: **60/minute per IP**
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
                        "message": "Both min_score and max_score are required.",
                        "status": 422},
            )
        if min_score > max_score:
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_score_range",
                        "message": f"min_score ({min_score}) must be ≤ max_score ({max_score})",
                        "status": 422},
            )
        return [_to_rank_entry(r) for r in store.search_by_score_range(lb, min_score, max_score, limit)]

    return store.search_by_player_prefix(lb, player_prefix, limit)