"""
schemas.py — Pydantic request/response models

Every model in this file maps 1-to-1 with a shape that the API either
accepts (Request) or returns (Response / Result).

Validation philosophy
─────────────────────
  - Strings are stripped of whitespace before validation.
  - Scores must be finite (no NaN, no ±Inf).
  - player_id is alphanumeric + limited punctuation — no raw spaces.
  - Bulk endpoints cap at 500 entries to prevent runaway payloads.
  - All optional fields default to None / [] / {} so callers don't
    have to include them.

FastAPI integration
───────────────────
  FastAPI reads Field(...) descriptions and generates them verbatim
  in the Swagger UI at /docs — keep them clear and recruiter-friendly.
"""

import math
from datetime import datetime
from typing import Annotated, Any, Optional

from pydantic import (
    BaseModel,
    Field,
    field_validator,
    model_validator,
    field_serializer,
)


# ── Reusable annotated types ──────────────────────────────────────────────────

PlayerIdField = Annotated[
    str,
    Field(
        ...,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_\-\.@]+$",
        description="Caller-defined player identifier. Alphanumeric plus _ - . @ allowed.",
        examples=["user_42", "alice@example.com", "player-007"],
    ),
]

ScoreField = Annotated[
    float,
    Field(
        ...,
        description="Numeric score. Supports decimals. Must be a finite number (no NaN / ±Inf).",
        examples=[1500.0, 99.9, -200.0],
    ),
]

MetadataField = Annotated[
    dict[str, Any],
    Field(
        default_factory=dict,
        description=(
            "Arbitrary JSON object stored alongside the score. "
            "Put display names, avatars, country codes, or anything else here. "
            "Not indexed — use player_id for lookups."
        ),
        examples=[{"display_name": "Alice", "country": "IN", "avatar_url": "https://..."}],
    ),
]


# ── Shared validator helpers ──────────────────────────────────────────────────

def _assert_finite(v: float, field_name: str = "value") -> float:
    """Raise ValueError if v is NaN or infinite."""
    if math.isnan(v) or math.isinf(v):
        raise ValueError(f"{field_name} must be a finite number, got {v}")
    return v


# ═══════════════════════════════════════════════════════════════════════════════
# Leaderboard schemas
# ═══════════════════════════════════════════════════════════════════════════════

class CreateLeaderboardRequest(BaseModel):
    """Body for POST /v1/leaderboards"""

    name: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9\-_]+$",
        description="URL-safe slug. Lowercase letters, digits, hyphens, underscores only.",
        examples=["chess-tournament", "weekly_speedrun", "global-2026"],
    )
    order: str = Field(
        "desc",
        pattern="^(asc|desc)$",
        description=(
            "Sort direction. "
            "`desc` = highest score is rank 1 (most games). "
            "`asc`  = lowest score is rank 1 (e.g. golf, speedrun times)."
        ),
        examples=["desc", "asc"],
    )
    max_entries: int = Field(
        10_000,
        ge=1,
        le=1_000_000,
        description=(
            "Maximum number of players stored. "
            "Once full, the lowest-ranked player is evicted on each new insert."
        ),
        examples=[1000, 10_000, 1_000_000],
    )
    reset_policy: str = Field(
        "never",
        pattern="^(never|daily|weekly|monthly)$",
        description="Automatic score-wipe schedule. `never` disables auto-reset.",
        examples=["never", "daily", "weekly", "monthly"],
    )

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        return v.strip()


class UpdateLeaderboardRequest(BaseModel):
    """Body for PATCH /v1/leaderboards/{lb_id} — all fields optional."""

    max_entries: Optional[int] = Field(
        None,
        ge=1,
        le=1_000_000,
        description="New cap on stored players.",
    )
    reset_policy: Optional[str] = Field(
        None,
        pattern="^(never|daily|weekly|monthly)$",
        description="New auto-reset schedule.",
    )

    @model_validator(mode="after")
    def at_least_one_field(self):
        if self.max_entries is None and self.reset_policy is None:
            raise ValueError(
                "Provide at least one of max_entries or reset_policy to update."
            )
        return self


class LeaderboardStats(BaseModel):
    """Live aggregate statistics embedded in LeaderboardResponse."""

    total_players: int = Field(..., description="Number of players currently on the board.")
    highest_score: Optional[float] = Field(None, description="Score of rank-1 player.")
    lowest_score:  Optional[float] = Field(None, description="Score of last-place player.")
    average_score: Optional[float] = Field(None, description="Mean score across all players.")
    last_updated:  Optional[datetime] = Field(None, description="When the last score change occurred.")


class LeaderboardResponse(BaseModel):
    """Returned by all leaderboard read/write endpoints."""

    id:           str             = Field(..., description="Unique leaderboard ID, e.g. 'lb_a1b2c3d4'.")
    name:         str             = Field(..., description="URL-safe slug.")
    order:        str             = Field(..., description="'asc' or 'desc'.")
    max_entries:  int             = Field(..., description="Player cap.")
    reset_policy: str             = Field(..., description="Auto-reset schedule.")
    created_at:   datetime        = Field(..., description="Creation timestamp (UTC).")
    stats:        LeaderboardStats


class ResetResponse(BaseModel):
    """Returned by POST /v1/leaderboards/{lb_id}/reset"""

    leaderboard_id:  str      = Field(..., description="ID of the leaderboard that was reset.")
    players_cleared: int      = Field(..., description="Number of score entries that were wiped.")
    reset_at:        datetime = Field(..., description="Timestamp of the reset (UTC).")


# ═══════════════════════════════════════════════════════════════════════════════
# Score submission schemas
# ═══════════════════════════════════════════════════════════════════════════════

class SubmitScoreRequest(BaseModel):
    """Body for POST /v1/leaderboards/{lb_id}/scores"""

    player_id: PlayerIdField
    score:     ScoreField
    metadata:  MetadataField = {}

    @field_validator("score")
    @classmethod
    def validate_score(cls, v: float) -> float:
        return _assert_finite(v, "score")

    @field_validator("player_id")
    @classmethod
    def strip_player_id(cls, v: str) -> str:
        return v.strip()


class BulkSubmitEntry(BaseModel):
    """One entry inside a BulkSubmitRequest. Same shape as SubmitScoreRequest."""

    player_id: PlayerIdField
    score:     ScoreField
    metadata:  MetadataField = {}

    @field_validator("score")
    @classmethod
    def validate_score(cls, v: float) -> float:
        return _assert_finite(v, "score")

    @field_validator("player_id")
    @classmethod
    def strip_player_id(cls, v: str) -> str:
        return v.strip()


class BulkSubmitRequest(BaseModel):
    """Body for POST /v1/leaderboards/{lb_id}/scores/bulk"""

    entries: list[BulkSubmitEntry] = Field(
        ...,
        min_length=1,
        max_length=500,
        description="List of score entries to submit. Max 500 per request.",
    )


class BulkSubmitResult(BaseModel):
    """Returned by POST /v1/leaderboards/{lb_id}/scores/bulk"""

    submitted: int = Field(..., description="Number of entries successfully stored.")
    failed:    int = Field(..., description="Number of entries that failed.")
    errors: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Per-entry error details for any failed entries.",
        examples=[[{"player_id": "bad_one", "error": "score must be finite"}]],
    )


class IncrementScoreRequest(BaseModel):
    """Body for POST /v1/leaderboards/{lb_id}/scores/increment"""

    player_id: PlayerIdField
    delta: float = Field(
        ...,
        description=(
            "Amount to add to the player's current score. "
            "Positive = score goes up. Negative = score goes down. "
            "If the player doesn't exist yet, they start at 0 + delta."
        ),
        examples=[10.0, -5.0, 0.5],
    )
    metadata: MetadataField = {}

    @field_validator("delta")
    @classmethod
    def validate_delta(cls, v: float) -> float:
        return _assert_finite(v, "delta")

    @field_validator("player_id")
    @classmethod
    def strip_player_id(cls, v: str) -> str:
        return v.strip()


# ═══════════════════════════════════════════════════════════════════════════════
# Rank / query response schemas
# ═══════════════════════════════════════════════════════════════════════════════

class RankEntry(BaseModel):
    """
    A single row in a ranking result.
    Used in top-K, range, paginated, and nearby responses.
    """

    rank:       int     = Field(..., ge=1, description="1-based rank position.")
    player_id:  str     = Field(..., description="Player identifier.")
    score:      float   = Field(..., description="Player's current score.")
    percentile: float   = Field(
        0.0,
        ge=0.0,
        le=100.0,
        description="Percentage of players this player beats. 100.0 = top of the board.",
    )
    metadata:   dict[str, Any] = Field(default_factory=dict)
    updated_at: Optional[datetime] = Field(None, description="When this score was last set.")


class ScoreResponse(BaseModel):
    """
    Returned immediately after a score submit or increment.
    Gives the caller their new rank without a separate GET request.
    """

    player_id:     str   = Field(..., description="Player identifier.")
    score:         float = Field(..., description="Score that was stored.")
    rank:          int   = Field(..., ge=1, description="Player's new rank.")
    percentile:    float = Field(..., ge=0.0, le=100.0)
    total_players: int   = Field(..., description="Total players on the board after this insert.")
    metadata:      dict[str, Any] = Field(default_factory=dict)


class PlayerRankResponse(BaseModel):
    """
    Returned by GET /v1/leaderboards/{lb_id}/players/{player_id}/rank.
    Optionally includes nearby players when ?window > 0.
    """

    player_id:     str     = Field(..., description="Player identifier.")
    rank:          int     = Field(..., ge=1)
    score:         float
    percentile:    float   = Field(..., ge=0.0, le=100.0)
    total_players: int     = Field(..., description="Total players currently on the board.")
    metadata:      dict[str, Any] = Field(default_factory=dict)
    nearby: Optional[list[RankEntry]] = Field(
        None,
        description=(
            "Players immediately above and below this player. "
            "Only present when ?window > 0 is passed."
        ),
    )
    updated_at: Optional[datetime] = Field(None, description="When this player's score was last set.")


class PaginatedRankResponse(BaseModel):
    """
    Returned by GET /v1/leaderboards/{lb_id}/rankings.
    Wraps a page of RankEntry items with cursor metadata.
    """

    items:     list[RankEntry] = Field(..., description="Entries for this page.")
    total:     int             = Field(..., description="Total players on the board.")
    page:      int             = Field(..., ge=1, description="Current page number (1-indexed).")
    page_size: int             = Field(..., ge=1, description="Entries per page.")
    has_next:  bool            = Field(..., description="Whether a next page exists.")
    has_prev:  bool            = Field(..., description="Whether a previous page exists.")


# ═══════════════════════════════════════════════════════════════════════════════
# Bulk delete schemas
# ═══════════════════════════════════════════════════════════════════════════════

class BulkDeleteRequest(BaseModel):
    """Body for DELETE /v1/leaderboards/{lb_id}/scores"""

    player_ids: list[str] = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Player IDs to remove. Max 500 per request.",
        examples=[["alice", "bob", "carol"]],
    )

    @field_validator("player_ids")
    @classmethod
    def no_empty_ids(cls, v: list[str]) -> list[str]:
        for pid in v:
            if not pid or not pid.strip():
                raise ValueError("player_ids must not contain empty strings")
        return [pid.strip() for pid in v]


class BulkDeleteResult(BaseModel):
    """Returned by DELETE /v1/leaderboards/{lb_id}/scores"""

    deleted:   int       = Field(..., description="Number of players successfully removed.")
    not_found: list[str] = Field(
        default_factory=list,
        description="Player IDs that were not found on the leaderboard.",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Error schema
# ═══════════════════════════════════════════════════════════════════════════════

class ErrorResponse(BaseModel):
    """
    Standard error envelope returned on 4xx / 5xx responses.

    Example:
        {
            "error":   "leaderboard_not_found",
            "message": "No leaderboard with id 'lb_abc123'",
            "status":  404,
            "detail":  null
        }
    """

    error:   str          = Field(..., description="Machine-readable error code.")
    message: str          = Field(..., description="Human-readable explanation.")
    status:  int          = Field(..., ge=100, le=599, description="HTTP status code.")
    detail:  Optional[Any] = Field(None, description="Additional context (validation errors, etc.).")