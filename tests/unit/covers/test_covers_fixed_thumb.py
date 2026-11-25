"""The fixed YouTube thumbnail (migration 0057): config parsing, the publish hook, the set-thumbnail job.
No network, no database."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from sixstars.clients.fakes import FakeUploadPostClient
from sixstars.clients.upload_post import AccountAuthError, PostingBlocked, RateLimited
from sixstars.covers import deliver, fixed_thumb
from sixstars.covers.config import CoverConfig

KEY = "covers-public/brand/youtube-thumbnail-abc.jpg"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (KEY, KEY),
        (f"/{KEY}/ ", KEY),
        (None, None),
        ("", None),
        ("covers-public", None),
        (42, None),
    ],
)
def test_config_parses_the_key(value: Any, expected: str | None) -> None:
    config = CoverConfig.from_values({"cover.youtube_fixed_thumbnail": value})
    assert config.youtube_fixed_thumbnail == expected
    assert config.enabled is False  # independent of the cover engine switch


def test_image_key_follows_the_content() -> None:
    assert fixed_thumb.image_key(b"a") == fixed_thumb.image_key(b"a")
    assert fixed_thumb.image_key(b"a") != fixed_thumb.image_key(b"b")
    assert fixed_thumb.image_key(b"a").startswith("covers-public/brand/youtube-thumbnail-")


def test_delay_backs_off_and_caps() -> None:
    assert [fixed_thumb.delay_for(n) for n in range(7)] == [2, 5, 15, 30, 60, 60, 60]


@dataclass
class Recorder:
    queued: list[dict[str, Any]] = field(default_factory=list)
    events: list[tuple[str, str]] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    r = Recorder()
    monkeypatch.setattr(
        CoverConfig,
        "load",
        classmethod(lambda cls: CoverConfig.from_values({"cover.youtube_fixed_thumbnail": KEY})),
    )
    monkeypatch.setattr(
        fixed_thumb.jobs,
        "enqueue",
        lambda queue, type, payload, **kw: r.queued.append({"type": type, **payload, **kw}),  # noqa: A006
    )
    monkeypatch.setattr(
        fixed_thumb.events,
        "emit",
        lambda type, level="info", *a, **kw: r.events.append((type, level)),  # noqa: A006
    )
    monkeypatch.setattr(deliver, "alert_once", lambda kind, title, body: r.alerts.append(kind))
    return r


class Storage:
    def public_url(self, key: str) -> str:
        return f"https://pub/{key}"


def job(attempt: int = 0, first_at: datetime | None = None) -> Any:
    @dataclass
    class J:
        payload: dict[str, Any]

    return J(
        {
            "post_id": "p1",
            "video_id": "v1",
            "user": "u",
            "attempt": attempt,
            "first_at": (first_at or datetime.now(UTC)).isoformat(),
        }
    )


def test_sets_the_fixed_thumbnail(rec: Recorder) -> None:
    client = FakeUploadPostClient()
    fixed_thumb.handle(job(), client=client, storage=Storage())
    assert client.thumbnails == {"v1": f"https://pub/{KEY}"}
    assert rec.events == [("cover.youtube_fixed_applied", "info")] and rec.queued == []


def test_backs_off_while_processing_then_alerts(rec: Recorder) -> None:
    client = FakeUploadPostClient()
    client.thumbnail_pending = 99
    for attempt in range(len(fixed_thumb.BACKOFF_MIN)):
        fixed_thumb.handle(job(attempt), client=client, storage=Storage())
    assert [q["attempt"] for q in rec.queued] == [1, 2, 3, 4]
    assert rec.events == [("cover.youtube_fixed_failed", "warn")]
    assert rec.alerts == ["youtube_fixed_pending"]


def test_403_alerts_and_stops(rec: Recorder) -> None:
    client = FakeUploadPostClient()
    client.thumbnail_error = AccountAuthError("HTTP 403: unverified")
    fixed_thumb.handle(job(), client=client, storage=Storage())
    assert rec.alerts == ["youtube_fixed_403"] and rec.queued == []


def test_429_retries_hourly_until_24h(rec: Recorder) -> None:
    client = FakeUploadPostClient()
    client.thumbnail_error = RateLimited("u/youtube", None, "quota")
    fixed_thumb.handle(job(), client=client, storage=Storage())
    assert len(rec.queued) == 1 and rec.queued[0]["attempt"] == 1
    client.thumbnail_error = RateLimited("u/youtube", None, "quota")
    fixed_thumb.handle(job(3, datetime.now(UTC) - timedelta(hours=25)), client=client, storage=Storage())
    assert len(rec.queued) == 1 and rec.events == [("cover.youtube_fixed_failed", "warn")]


def test_blocked_posting_does_nothing(rec: Recorder) -> None:
    client = FakeUploadPostClient()
    client.thumbnail_error = PostingBlocked("emergency stop")
    fixed_thumb.handle(job(), client=client, storage=Storage())
    assert rec.queued == [] and rec.events == [] and client.thumbnails == {}


def test_off_does_nothing(rec: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(CoverConfig, "load", classmethod(lambda cls: CoverConfig.from_values({})))
    client = FakeUploadPostClient()
    fixed_thumb.handle(job(), client=client, storage=Storage())
    assert client.thumbnails == {} and rec.events == []


def test_on_published_owns_youtube_only_when_set(rec: Recorder) -> None:
    on = CoverConfig.from_values({"cover.youtube_fixed_thumbnail": KEY})
    off = CoverConfig.from_values({})
    assert fixed_thumb.on_published("p1", "youtube", "v1", "u", on) is True
    assert rec.queued[0]["type"] == fixed_thumb.JOB and rec.queued[0]["video_id"] == "v1"
    assert fixed_thumb.on_published("p2", "instagram", "x", "u", on) is False
    assert fixed_thumb.on_published("p3", "youtube", "v3", "u", off) is False
    assert len(rec.queued) == 1


def test_after_published_skips_the_cover_engine_on_youtube(
    rec: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    looked_up: list[str] = []

    class Db:
        def fetch_one(self, sql: str, params: Any = None) -> None:
            looked_up.append(sql)

    monkeypatch.setattr(deliver, "db", Db())
    deliver.after_published("p1", "youtube", "v1", "u")
    assert looked_up == [] and rec.queued[0]["type"] == fixed_thumb.JOB
