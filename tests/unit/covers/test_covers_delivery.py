"""Cover engine C7 (docs/29a stage 6) and the arm choice: upload fields, the YouTube follow-up job, the
publisher hooks. No network, no database."""

import random
from dataclasses import dataclass, field
from typing import Any

import pytest

from sixstars.clients.fakes import FakeUploadPostClient
from sixstars.clients.upload_post import AccountAuthError, RateLimited
from sixstars.covers import deliver, learn
from sixstars.covers.config import CoverConfig
from sixstars.covers.hookline import Candidate


def test_fields_for_each_platform() -> None:
    cover = {"public_url": "https://x/covers-public/staging/c.jpg", "storage_path": "covers-work/c/c.jpg"}
    assert deliver.fields_for("instagram", cover, 4250, tiktok_image=False, signed_url=None) == {
        "cover_url": "https://x/covers-public/staging/c.jpg"
    }
    assert deliver.fields_for("instagram", None, 4250, tiktok_image=False, signed_url=None) == {
        "thumb_offset": "4250"
    }
    assert deliver.fields_for("instagram", None, None, tiktok_image=False, signed_url=None) == {}
    assert deliver.fields_for("tiktok", cover, 4250, tiktok_image=False, signed_url="https://s") == {
        "cover_timestamp": "4250"
    }
    assert deliver.fields_for("tiktok", cover, 4250, tiktok_image=True, signed_url="https://s") == {
        "cover_timestamp": "4250",
        "tiktok_cover_image_url": "https://s",
    }
    assert deliver.fields_for("youtube", cover, 4250, tiktok_image=True, signed_url="https://s") == {}


# ── arms ─────────────────────────────────────────────────────

QUOTE = Candidate("SIX STARS?!", "quote", "STARS", "six stars", ("SIX STARS?!",))
LABEL = Candidate("FIRST CAR STOLEN", "label", "CAR", "stole a car", ("FIRST CAR", "STOLEN"))


def test_allowed_layouts() -> None:
    assert learn.allowed_layouts("instagram", "face_led") == ("face_led", "split")
    assert learn.allowed_layouts("tiktok", "split") == ("split", "tiktok_native")
    assert learn.arm_name("split", "quote") == "split|quote"


def test_choose_stays_inside_the_allowed_arms() -> None:
    rng = random.Random(7)
    seen = set()
    for _ in range(400):
        layout, line = learn.choose("tiktok", "moment_led", [QUOTE, LABEL], [], exploration=0.2, rng=rng)
        seen.add((layout, None if line is None else line.style))
        assert layout in ("moment_led", "split", "tiktok_native")
    assert ("moment_led", None) in seen  # the no-line control arm shows up
    assert {("split", "quote"), ("tiktok_native", "label")} <= seen


def test_thompson_prefers_a_winning_arm() -> None:
    stats = {"split|quote": learn.ArmStats(40, 50), "split|label": learn.ArmStats(5, 50)}
    rng = random.Random(1)
    picks = [learn.thompson(stats, ["split|quote", "split|label"], rng) for _ in range(200)]
    assert picks.count("split|quote") > 190


# ── YouTube follow-up ────────────────────────────────────────


@dataclass
class Job:
    payload: dict[str, Any]


@dataclass
class Recorder:
    statuses: list[tuple[str, str]] = field(default_factory=list)
    queued: list[dict[str, Any]] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)


@pytest.fixture
def youtube(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(
        deliver.CoverConfig,
        "load",
        classmethod(
            lambda cls: CoverConfig.from_values({"cover.enabled": True, "cover.youtube_custom_enabled": True})
        ),
    )
    monkeypatch.setattr(
        deliver.db, "fetch_one", lambda sql, params=None: {"storage_path": "covers-work/c/x.jpg"}
    )

    class Storage:
        def signed_url(self, key: str, expires_in_s: int = 0) -> str:
            return f"https://signed/{key}"

    monkeypatch.setattr(deliver.clip_storage, "ClipStorage", Storage)
    monkeypatch.setattr(
        deliver.store, "set_status", lambda cover_id, status, **kw: rec.statuses.append((cover_id, status))
    )
    monkeypatch.setattr(
        deliver,
        "queue_youtube_thumb",
        lambda cover_id, video_id, user, *, attempt, delay_min=None: rec.queued.append(
            {"attempt": attempt, "delay": delay_min}
        ),
    )
    monkeypatch.setattr(deliver, "disable_youtube", lambda reason: rec.disabled.append(reason))
    return rec


def job(attempt: int = 0) -> Job:
    return Job({"cover_id": "c1", "video_id": "v1", "user": "default", "attempt": attempt})


def test_youtube_thumbnail_set(youtube: Recorder) -> None:
    client = FakeUploadPostClient()
    deliver.handle_youtube_thumb(job(), client=client)  # type: ignore[arg-type]
    assert client.thumbnails == {"v1": "https://signed/covers-work/c/x.jpg"}
    assert youtube.statuses == [("c1", "applied")]


def test_youtube_thumbnail_backs_off_while_processing(youtube: Recorder) -> None:
    client = FakeUploadPostClient()
    client.thumbnail_pending = 5
    deliver.handle_youtube_thumb(job(0), client=client)  # type: ignore[arg-type]
    deliver.handle_youtube_thumb(job(1), client=client)  # type: ignore[arg-type]
    deliver.handle_youtube_thumb(job(2), client=client)  # type: ignore[arg-type]
    assert [q["attempt"] for q in youtube.queued] == [1, 2]
    assert youtube.statuses == [("c1", "apply_failed")]


def test_youtube_403_switches_it_off(youtube: Recorder) -> None:
    client = FakeUploadPostClient()
    client.thumbnail_error = AccountAuthError("HTTP 403: channel not eligible")
    deliver.handle_youtube_thumb(job(), client=client)  # type: ignore[arg-type]
    assert youtube.disabled and youtube.statuses == [("c1", "apply_failed")]


def test_youtube_429_retries_later(youtube: Recorder) -> None:
    client = FakeUploadPostClient()
    client.thumbnail_error = RateLimited("default/youtube", None, "quota")
    deliver.handle_youtube_thumb(job(), client=client)  # type: ignore[arg-type]
    assert (
        youtube.queued == [{"attempt": 1, "delay": deliver.RATE_LIMIT_RETRY_MIN}] and youtube.statuses == []
    )


# ── waiting for the cover (owner, 23 Nov) ─────────────────────


class FakeDb:
    def __init__(self, decided_after: int | None, pending: bool) -> None:
        self.calls = 0
        self.decided_after = decided_after
        self.pending = pending

    def fetch_one(self, sql: str, params: Any = None) -> dict[str, int] | None:
        if "from covers" in sql:
            self.calls += 1
            done = self.decided_after is not None and self.calls > self.decided_after
            return {"x": 1} if done else None
        return {"x": 1} if self.pending else None


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.mark.parametrize(
    ("decided_after", "pending", "expected", "waited"),
    [
        (0, True, True, 0.0),  # ready: no wait
        (3, True, True, 9.0),  # ready after three checks
        (None, True, False, 60.0),  # never ready: gives up at the deadline
        (None, False, False, 0.0),  # no cover job: no wait
    ],
)
def test_wait_for_cover(
    monkeypatch: pytest.MonkeyPatch, decided_after: int | None, pending: bool, expected: bool, waited: float
) -> None:
    fake, clock = FakeDb(decided_after, pending), FakeClock()
    monkeypatch.setattr(deliver, "db", fake)
    assert deliver.wait_for_cover("c", "a", 60, sleep=clock.sleep, clock=clock.time) is expected
    assert sum(clock.slept) == pytest.approx(waited)
