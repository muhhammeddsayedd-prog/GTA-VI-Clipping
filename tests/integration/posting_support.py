"""Helpers for the posting integration tests: a clean set of posting tables, the seeded accounts and small
builders for creators, clips, spikes and old posts."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from sixstars import config, db, settings

REPO = Path(__file__).resolve().parents[2]
SEEDS = REPO / "supabase" / "seed"
MIGRATION_0005 = REPO / "supabase" / "migrations" / "0005_posting_limits.sql"

# A Friday noon after the launch (2026-11-18 11:00 UTC): the hour 12 is one of TikTok's hourly hours.
T0 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
NOW = T0 + timedelta(minutes=3)
IG, YT, TT, TT_LIVE = "instagram_main", "youtube_main", "tiktok_main", "tiktok_live"


def fixed(now: datetime = NOW) -> Callable[[], datetime]:
    return lambda: now


def use_env(monkeypatch: pytest.MonkeyPatch, app_env: str) -> None:
    monkeypatch.setenv("APP_ENV", app_env)
    config.env.cache_clear()


def clean_posting_tables() -> None:
    db.execute("truncate posts, clips, spikes, creators, platform_accounts restart identity cascade")


def seed(name: str) -> None:
    """Run `supabase/seed/<name>.sql` (accounts, and for staging its settings)."""
    db.execute((SEEDS / f"{name}.sql").read_bytes())  # type: ignore[arg-type]
    settings.invalidate()


def go_live() -> None:
    """Production defaults with the dry-run switch off: at `NOW` posting is live."""
    settings.set("launch.dry_run", False)


def add_creator(handle: str = "SomeStreamer", platform: str = "youtube") -> str:
    row = db.fetch_one(
        "insert into creators (platform, channel_id, handle) values (%s, %s, %s) returning id::text as id",
        (platform, f"chan-{handle}", handle),
    )
    assert row is not None
    return str(row["id"])


def add_spike(creator_id: str, spike_wall: datetime) -> str:
    row = db.fetch_one(
        "insert into spikes (mode, creator_id, spike_wall, tier) values ('live', %s, %s, 'S') "
        "returning id::text as id",
        (creator_id, spike_wall),
    )
    assert row is not None
    return str(row["id"])


def add_clip(
    creator_id: str,
    video: Path | None,
    *,
    lane: str = "hourly",
    rank: float = 5.0,
    approved_at: datetime = NOW - timedelta(hours=1),
    status: str = "approved",
    spike_id: str | None = None,
    title: str = "He jumped the WHOLE bridge",
) -> str:
    row = db.fetch_one(
        """
        insert into clips
          (creator_id, spike_id, source_platform, source_id, source_url, source_type, lane, start_s, end_s,
           title, caption, hashtags, rank_score, status, approved_at, local_path)
        values (%s, %s, 'youtube', 'vid1', 'https://example.test/v', %s, %s, 0, 30,
                %s, 'Nobody expected this jump', '{#stunt}', %s, %s, %s, %s)
        returning id::text as id
        """,
        (
            creator_id,
            spike_id,
            "live" if lane == "spike" else "vod",
            lane,
            title,
            rank,
            status,
            approved_at,
            None if video is None else str(video),
        ),
    )
    assert row is not None
    return str(row["id"])


def add_spike_clip(creator_id: str, video: Path, *, spike_wall: datetime, rank: float = 9.0) -> str:
    """An approved spike-lane clip whose spike happened at `spike_wall`."""
    spike_id = add_spike(creator_id, spike_wall)
    return add_clip(creator_id, video, lane="spike", rank=rank, approved_at=NOW, spike_id=spike_id)


def add_post(
    clip_id: str,
    account_id: str,
    at: datetime,
    *,
    lane: str = "hourly",
    status: str = "published",
    dry_run: bool = False,
    slot_at: datetime | None = None,
    key: str | None = None,
) -> str:
    """An existing `posts` row (history)."""
    row = db.fetch_one(
        """
        insert into posts (clip_id, account_id, platform, lane, slot_at, status, dry_run, idempotency_key,
                           created_at, published_at)
        values (%s, %s, split_part(%s, '_', 1), %s, %s, %s, %s, %s, %s, %s)
        returning id::text as id
        """,
        (
            clip_id,
            account_id,
            account_id,
            lane,
            slot_at,
            status,
            dry_run,
            key or f"old:{clip_id}:{account_id}:{at.isoformat()}",
            at,
            at if status == "published" else None,
        ),
    )
    assert row is not None
    return str(row["id"])


def posts(where: str = "true", params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return db.fetch_all(
        f"select *, id::text as post_id, clip_id::text as clip from posts where {where} "
        "order by account_id, created_at",
        params,
    )


def clip_row(clip_id: str) -> dict[str, Any]:
    row = db.fetch_one("select * from clips where id = %s", (clip_id,))
    assert row is not None
    return row


def account_row(account_id: str) -> dict[str, Any]:
    row = db.fetch_one("select * from platform_accounts where id = %s", (account_id,))
    assert row is not None
    return row


def event_types() -> list[str]:
    return [row["type"] for row in db.fetch_all("select type from events order by id")]


def count(table: str) -> int:
    row = db.fetch_one(f"select count(*) as n from {table}")
    assert row is not None
    return int(row["n"])


@pytest.fixture
def video(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"not a real video")
    return path


@pytest.fixture
def production(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clean posting tables, the production seed and `APP_ENV=production`. Still in dry run (the default).
    The tables are emptied again afterwards, so other integration tests find them as they expect."""
    use_env(monkeypatch, "production")
    clean_posting_tables()
    seed("production")
    # The lane tests describe posting the moment a spike clip is ready; the R12 holding window
    # (`posting.spike_hold_s`, migration 0023) has its own test, which switches it on.
    hold = settings.get("posting.spike_hold_s", None)
    settings.set("posting.spike_hold_s", 0)
    yield
    clean_posting_tables()
    if hold is not None:
        settings.set("posting.spike_hold_s", hold)


@pytest.fixture
def staging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clean posting tables, the staging seed (accounts and settings) and `APP_ENV=staging`."""
    use_env(monkeypatch, "staging")
    clean_posting_tables()
    seed("staging")
    yield
    clean_posting_tables()
