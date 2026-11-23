"""The `covers` table (migration 0039): one row per cover attempt for a clip render on a platform account.

All SQL of the cover engine goes through here. Rows are written as `draft` and move through the statuses of
docs/29a section 8; every row carries the scoring config version it was made with.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from psycopg.types.json import Jsonb

from sixstars import db

CoverStatus = Literal[
    "draft",
    "passed",
    "failed",
    "applied",
    "apply_failed",
    "skipped_deadline",
    "fallback_plain",
    "fallback_default",
]


@dataclass(frozen=True, slots=True)
class NewCover:
    """The columns of a new `covers` row."""

    clip_id: str
    lane: str
    platform: str
    arm: str
    config_version: int
    render_id: str | None = None
    account_id: str | None = None
    mode: Literal["full", "frame_only"] = "full"
    layout: str | None = None
    line_text: str | None = None
    line_style: str | None = None
    key_word: str | None = None
    frame_ts_ms: int | None = None
    rendered_ts_ms: int | None = None
    scores: Mapping[str, Any] = field(default_factory=dict)
    phash: str | None = None
    storage_path: str | None = None
    public_url: str | None = None
    status: CoverStatus = "draft"
    attempt: int = 1
    error: str | None = None


_COLUMNS = (
    "clip_id",
    "render_id",
    "account_id",
    "lane",
    "mode",
    "platform",
    "arm",
    "layout",
    "line_text",
    "line_style",
    "key_word",
    "frame_ts_ms",
    "rendered_ts_ms",
    "scores",
    "phash",
    "storage_path",
    "public_url",
    "status",
    "attempt",
    "error",
    "config_version",
)


def insert(cover: NewCover) -> str:
    """Insert a row; returns its id."""
    values = [getattr(cover, name) for name in _COLUMNS]
    values[_COLUMNS.index("scores")] = Jsonb(dict(cover.scores))
    placeholders = ", ".join(["%s"] * len(_COLUMNS))
    row = db.fetch_one(
        f"insert into covers ({', '.join(_COLUMNS)}) values ({placeholders}) returning id::text as id",  # noqa: S608
        tuple(values),
    )
    assert row is not None
    return str(row["id"])


def set_status(
    cover_id: str,
    status: CoverStatus,
    *,
    error: str | None = None,
    post_id: str | None = None,
    scores: Mapping[str, Any] | None = None,
) -> None:
    """Move a cover to `status`; `applied` stamps `applied_at`. `scores` are merged into the stored ones."""
    db.execute(
        """
        update covers set
          status = %s,
          error = coalesce(%s, error),
          post_id = coalesce(%s::uuid, post_id),
          scores = scores || %s,
          applied_at = case when %s = 'applied' then now() else applied_at end
        where id = %s
        """,
        (status, error, post_id, Jsonb(dict(scores or {})), status, cover_id),
    )


@dataclass(frozen=True, slots=True)
class RecentCover:
    """What the diversity rule and the hook prompt need from an account's latest covers."""

    id: str
    layout: str | None
    line_style: str | None
    line_text: str | None
    phash: str | None
    status: str


_RECENT_SQL = """
select id::text as id, layout, line_style, line_text, phash, status
from covers
where account_id = %s and status in ('passed', 'applied', 'fallback_plain')
order by created_at desc
limit %s
"""


def recent_for_account(account_id: str, limit: int) -> list[RecentCover]:
    """The account's latest usable covers, newest first."""
    return [
        RecentCover(
            id=str(row["id"]),
            layout=row["layout"],
            line_style=row["line_style"],
            line_text=row["line_text"],
            phash=row["phash"],
            status=str(row["status"]),
        )
        for row in db.fetch_all(_RECENT_SQL, (account_id, limit))
    ]


def recent_lines(account_id: str, limit: int = 20) -> list[str]:
    """The account's latest cover lines (for the hook prompt's "different from these")."""
    return [cover.line_text for cover in recent_for_account(account_id, limit) if cover.line_text]
