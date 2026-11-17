"""The publisher against the fake Upload-Post client and an in-memory store: every way one post can end.
Nothing here touches the network or a database."""

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from memory_store import MemoryStore
from posting_helpers import LAUNCH_AT, T0, hourly_clip, make_params, production_accounts, spike_clip

from sixstars.clients._http import NotConfiguredError
from sixstars.clients.fakes import FakeUploadPostClient
from sixstars.clients.upload_post import (
    AccountAuthError,
    PlatformResult,
    PostingBlocked,
    PostRejected,
    RateLimited,
    UploadPostError,
    UploadStatus,
)
from sixstars.posting.models import Account, Clip
from sixstars.posting.publisher import (
    AUTH_PAUSE,
    LINK_REFRESH_S,
    PENDING_DEADLINE,
    POLL_AGAIN_S,
    RETRY_BACKOFF_S,
    Outcome,
    Publisher,
)

YT = "youtube_main"
IG = "instagram_main"
TT = "tiktok_main"
SLOT = T0  # T0 is exactly on the hour
NOW = T0 + timedelta(minutes=3)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Setup:
    """One planned post with its store, fake client, clock and publisher."""

    def __init__(
        self,
        tmp_path: Path,
        account_id: str = YT,
        *,
        clip: Clip | None = None,
        client: FakeUploadPostClient | None = None,
        app_env: str = "production",
        video: bool = True,
        dry_run_row: bool = False,
        **account_changes: Any,
    ) -> None:
        self.clock = Clock()
        self.store = MemoryStore(make_params(app_env))
        self.client = client or FakeUploadPostClient()
        self.account: Account = replace(production_accounts()[account_id], **account_changes)
        self.clip = clip or hourly_clip("clip-1", at=NOW)
        path = tmp_path / "clip-1.mp4"
        if video:
            path.write_bytes(b"not a real video")
        self.video = path
        slot = SLOT if self.clip.lane.value == "hourly" else None
        self.post_id = self.store.add_post(
            self.clip, self.account, NOW, video=str(path), slot_at=slot, dry_run=dry_run_row
        )
        self.publisher = Publisher(self.client, self.store, self.clock)

    def publish(self) -> Outcome:
        return self.publisher.publish(self.post_id).outcome

    @property
    def post(self) -> Any:
        return self.store.posts[self.post_id]

    def uploads(self) -> list[dict[str, Any]]:
        return self.client.calls_to("upload_video")


# ── success ───────────────────────────────────────────────────────────────────────────────────────────────
def test_a_pending_post_is_uploaded_once_and_stored_as_published(tmp_path: Path) -> None:
    s = Setup(tmp_path)

    assert s.publish() is Outcome.PUBLISHED

    (upload,) = s.uploads()
    assert upload["idempotency_key"] == upload["request_id"] == "post:clip-1:youtube_main"
    assert (upload["user"], upload["platform"], upload["video_path"]) == ("default", "youtube", s.video)
    assert upload["fields"]["privacyStatus"] == "public" and upload["fields"]["youtube_title"]
    assert s.post.status == "published" and s.post.published_at == NOW and s.post.attempts == 1
    assert s.post.platform_post_id == "youtube-post:clip-1:youtube_main"
    assert s.post.post_url == "https://example.test/youtube/youtube-post:clip-1:youtube_main"
    assert "post.published" in s.store.event_types()
    assert s.store.clips["clip-1"].clip.status == "posted"
    assert s.store.notifications == [] and s.store.retries == []


def test_publishing_twice_sends_one_upload(tmp_path: Path) -> None:
    s = Setup(tmp_path)

    assert s.publish() is Outcome.PUBLISHED
    assert s.publish() is Outcome.NOOP

    assert len(s.uploads()) == 1
    assert s.store.event_types().count("post.published") == 1


def test_a_spike_post_records_the_time_from_spike_to_post(tmp_path: Path) -> None:
    s = Setup(tmp_path, IG, clip=spike_clip("clip-1", at=NOW))

    assert s.publish() is Outcome.PUBLISHED
    assert s.store.spike_to_post == ["clip-1"]


def test_an_unknown_post_id_does_nothing(tmp_path: Path) -> None:
    s = Setup(tmp_path)

    assert s.publisher.publish("no-such-post").outcome is Outcome.NOOP
    assert s.uploads() == []


# ── failures reported by the platform ─────────────────────────────────────────────────────────────────────
def test_a_failed_platform_inside_an_http_200_answer_is_a_failed_post(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.client.platform_results["youtube"] = PlatformResult(
        "youtube", success=False, error="Video too short", error_code="invalid_video"
    )

    assert s.publish() is Outcome.FAILED

    assert s.post.status == "failed" and "Video too short" in s.post.error
    assert s.post.platform_post_id is None and s.post.published_at is None
    assert ("post.failed", "error", s.post_id) in s.store.events
    assert [n[0] for n in s.store.notifications] == ["warn"]
    assert s.store.pauses == [] and s.store.retries == []
    assert s.store.clips["clip-1"].clip.status == "approved"


def test_a_rejected_upload_fails_without_a_retry(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.client.upload_error = PostRejected("POST /api/upload: HTTP 400: title is required")

    assert s.publish() is Outcome.FAILED
    assert s.post.status == "failed" and s.store.retries == [] and len(s.uploads()) == 1


def test_a_tiktok_video_that_went_to_the_drafts_inbox_is_not_a_published_post(tmp_path: Path) -> None:
    s = Setup(tmp_path, TT)
    s.client.platform_results["tiktok"] = PlatformResult(
        "tiktok", success=True, post_id="tmp-1", raw={"fallback_to_inbox": True}
    )

    assert s.publish() is Outcome.FAILED
    assert s.post.status == "failed" and s.post.error.startswith("tiktok_inbox")


def test_a_finished_upload_without_a_result_for_the_platform_fails(tmp_path: Path) -> None:
    class NoResult(FakeUploadPostClient):
        def get_upload_status(self, request_id: str) -> UploadStatus:
            return UploadStatus(request_id, "failed", message="processing error")

    s = Setup(tmp_path, client=NoResult())

    assert s.publish() is Outcome.FAILED
    assert "processing error" in s.post.error


# ── rate limits ───────────────────────────────────────────────────────────────────────────────────────────
def test_http_429_pauses_the_account_for_an_hour(tmp_path: Path) -> None:
    s = Setup(tmp_path, IG)
    s.client.upload_error = RateLimited("default/instagram", 60.0, "too many requests")

    assert s.publish() is Outcome.FAILED

    assert s.store.pauses == [(IG, NOW + timedelta(hours=1), None)]
    assert s.store.accounts[IG].paused_until == NOW + timedelta(hours=1)
    assert s.post.status == "failed" and s.post.error.startswith("rate_limited")
    assert ("account.rate_limited", "warn", IG) in s.store.events
    assert [(n[0], n[1]) for n in s.store.notifications] == [("warn", "Rate limit on instagram_main")]
    assert s.store.retries == []


def test_a_youtube_limit_inside_the_result_lowers_the_cap_by_two(tmp_path: Path) -> None:
    s = Setup(tmp_path, YT, cap_24h=28)
    s.client.platform_results["youtube"] = PlatformResult(
        "youtube", success=False, error="daily limit", error_code="youtube_channel_daily_limit"
    )

    assert s.publish() is Outcome.FAILED

    ((account_id, until, new_cap),) = s.store.pauses
    assert (account_id, new_cap) == (YT, 26)
    assert until == NOW + timedelta(hours=24)  # until the oldest post in the window (this one) is 24 h old
    assert s.store.accounts[YT].cap_24h == 26


# ── authorisation ─────────────────────────────────────────────────────────────────────────────────────────
def test_an_auth_error_pauses_the_account_and_raises_a_critical_alert(tmp_path: Path) -> None:
    s = Setup(tmp_path, TT)
    s.client.upload_error = AccountAuthError("POST /api/upload: HTTP 401: invalid api key")

    assert s.publish() is Outcome.FAILED

    assert s.store.pauses == [(TT, NOW + AUTH_PAUSE, None)]
    assert ("account.auth_failed", "critical", TT) in s.store.events
    assert [n[0] for n in s.store.notifications] == ["critical"]
    assert s.post.status == "failed" and s.store.retries == []


@pytest.mark.parametrize(
    "result",
    [
        PlatformResult("tiktok", success=False, error="token", error_code="tiktok_reconnect_required"),
        PlatformResult("tiktok", success=False, skipped=True),
    ],
    ids=["reconnect_required", "platform_not_connected"],
)
def test_an_auth_problem_inside_the_result_pauses_the_account(tmp_path: Path, result: PlatformResult) -> None:
    s = Setup(tmp_path, TT)
    s.client.platform_results["tiktok"] = result

    assert s.publish() is Outcome.FAILED
    assert s.store.pauses == [(TT, NOW + AUTH_PAUSE, None)]
    assert [n[0] for n in s.store.notifications] == ["critical"]


def test_a_missing_api_key_fails_the_post_with_a_critical_alert(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.client.upload_error = NotConfiguredError("UPLOAD_POST_API_KEY is not set")

    assert s.publish() is Outcome.FAILED
    assert [n[0] for n in s.store.notifications] == ["critical"] and s.store.pauses == []


# ── timeouts and retries ──────────────────────────────────────────────────────────────────────────────────
def test_a_timeout_is_retried_later_with_the_same_idempotency_key(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.client.upload_error = httpx.ReadTimeout("timed out")

    first = s.publisher.publish(s.post_id)

    assert first.outcome is Outcome.PENDING
    assert first.retry_at == NOW + timedelta(seconds=RETRY_BACKOFF_S[0])
    assert s.post.status == "pending" and s.post.attempts == 1
    assert s.store.retries == [(s.post_id, first.retry_at)]
    assert ("post.retry_scheduled", "warn", s.post_id) in s.store.events

    s.clock.advance(RETRY_BACKOFF_S[0])
    assert s.publish() is Outcome.PUBLISHED

    first_call, second_call = s.uploads()
    assert first_call["idempotency_key"] == second_call["idempotency_key"] == "post:clip-1:youtube_main"
    assert first_call["request_id"] == second_call["request_id"]
    assert s.post.status == "published" and s.post.attempts == 2


class AcceptsThenTimesOut(FakeUploadPostClient):
    """The upload reaches Upload-Post, but the answer is lost."""

    def upload_video(self, *args: Any, **kwargs: Any) -> UploadStatus:
        super().upload_video(*args, **kwargs)
        raise httpx.ReadTimeout("timed out")


def test_after_a_timeout_an_upload_that_did_arrive_is_not_sent_again(tmp_path: Path) -> None:
    s = Setup(tmp_path, client=AcceptsThenTimesOut())

    assert s.publish() is Outcome.PENDING
    s.clock.advance(RETRY_BACKOFF_S[0])
    assert s.publish() is Outcome.PUBLISHED

    assert len(s.uploads()) == 1
    assert s.post.status == "published"


class AlwaysDown(FakeUploadPostClient):
    def upload_video(self, *args: Any, **kwargs: Any) -> UploadStatus:
        self._record("upload_video", idempotency_key=kwargs.get("idempotency_key", args[4]))
        raise UploadPostError("POST /api/upload: HTTP 503: unavailable")


def test_the_send_is_retried_three_times_then_the_post_fails(tmp_path: Path) -> None:
    s = Setup(tmp_path, client=AlwaysDown())
    outcomes = []
    for delay in (*RETRY_BACKOFF_S, 0):
        outcomes.append(s.publish())
        s.clock.advance(delay)

    assert outcomes == [Outcome.PENDING, Outcome.PENDING, Outcome.PENDING, Outcome.FAILED]
    assert [run_at - NOW for _, run_at in s.store.retries] == [
        timedelta(seconds=30),
        timedelta(seconds=30 + 120),
        timedelta(seconds=30 + 120 + 300),
    ]
    assert len({call["idempotency_key"] for call in s.uploads()}) == 1 and len(s.uploads()) == 4
    assert s.post.status == "failed" and "gave up" in s.post.error
    assert s.publish() is Outcome.NOOP and len(s.uploads()) == 4


def test_an_upload_that_is_already_in_flight_is_polled_not_failed(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.client.upload_error = PostRejected("POST /api/upload: HTTP 409: IDEMPOTENCY_KEY_IN_USE")

    result = s.publisher.publish(s.post_id)

    assert result.outcome is Outcome.PENDING and s.post.status == "pending"
    assert result.retry_at == NOW + timedelta(seconds=POLL_AGAIN_S)


def test_emergency_stop_during_the_retry_wait_stops_the_second_send(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.client.upload_error = httpx.ReadTimeout("timed out")
    assert s.publish() is Outcome.PENDING

    s.store.params = replace(s.store.params, emergency_stop=True)
    s.clock.advance(RETRY_BACKOFF_S[0])

    assert s.publish() is Outcome.FAILED
    assert len(s.uploads()) == 1
    assert s.post.status == "failed" and "emergency_stop" in s.post.error


# ── status polling ────────────────────────────────────────────────────────────────────────────────────────
class Slow(FakeUploadPostClient):
    """The status stays `processing` for the first `slow_reads` reads."""

    slow_reads = 2

    def get_upload_status(self, request_id: str) -> UploadStatus:
        if self.slow_reads > 0 and request_id in self.uploads:
            self.slow_reads -= 1
            return UploadStatus(request_id, "processing", completed=0, total=1)
        return super().get_upload_status(request_id)


def test_an_upload_still_processing_is_polled_again_later_without_a_second_upload(tmp_path: Path) -> None:
    s = Setup(tmp_path, client=Slow())

    first = s.publisher.publish(s.post_id)

    assert first.outcome is Outcome.PENDING
    assert first.retry_at == NOW + timedelta(seconds=POLL_AGAIN_S)
    assert s.post.status == "pending" and s.post.upload_request_id == "post:clip-1:youtube_main"
    assert len(s.client.calls_to("wait_for_upload")) == 1

    s.clock.advance(POLL_AGAIN_S)
    assert s.publish() is Outcome.PENDING
    s.clock.advance(POLL_AGAIN_S)
    assert s.publish() is Outcome.PUBLISHED

    assert len(s.uploads()) == 1
    assert s.post.status == "published" and s.post.published_at == NOW + timedelta(seconds=2 * POLL_AGAIN_S)


def test_an_upload_without_a_final_status_after_the_deadline_fails(tmp_path: Path) -> None:
    client = Slow()
    client.slow_reads = 1000
    s = Setup(tmp_path, client=client)
    assert s.publish() is Outcome.PENDING

    s.clock.advance(PENDING_DEADLINE.total_seconds() + 1)

    assert s.publish() is Outcome.FAILED
    assert s.post.status == "failed" and "no final status" in s.post.error
    assert len(s.uploads()) == 1


def test_a_status_that_cannot_be_read_is_polled_again(tmp_path: Path) -> None:
    class StatusDown(FakeUploadPostClient):
        def wait_for_upload(self, request_id: str, timeout_s: float = 600.0, poll_s: float = 10.0) -> Any:
            raise httpx.ConnectError("down")

    s = Setup(tmp_path, client=StatusDown())

    assert s.publish() is Outcome.PENDING
    assert s.post.status == "pending" and len(s.store.retries) == 1


# ── TikTok: published without a URL ───────────────────────────────────────────────────────────────────────
def test_a_tiktok_post_without_a_url_is_published_and_its_url_is_read_later(tmp_path: Path) -> None:
    s = Setup(tmp_path, TT)
    s.client.platform_results["tiktok"] = PlatformResult("tiktok", success=True, post_id="temp-77")

    assert s.publish() is Outcome.PUBLISHED

    assert s.post.status == "published" and s.post.post_url is None
    assert s.post.platform_post_id == "temp-77"
    assert s.store.retries == [(s.post_id, NOW + timedelta(seconds=LINK_REFRESH_S))]

    s.client.platform_results["tiktok"] = PlatformResult(
        "tiktok", success=True, post_id="7300", post_url="https://www.tiktok.com/@x/video/7300"
    )
    s.clock.advance(LINK_REFRESH_S)
    assert s.publish() is Outcome.PUBLISHED

    assert s.post.post_url == "https://www.tiktok.com/@x/video/7300" and s.post.platform_post_id == "7300"
    assert len(s.uploads()) == 1


def test_a_url_that_never_arrives_stops_being_asked_for(tmp_path: Path) -> None:
    s = Setup(tmp_path, TT)
    s.client.platform_results["tiktok"] = PlatformResult("tiktok", success=True, post_id="temp-77")
    assert s.publish() is Outcome.PUBLISHED

    s.clock.advance(3600)
    assert s.publish() is Outcome.NOOP

    assert len(s.store.retries) == 1 and s.post.status == "published"


# ── the gate is checked again right before the send ───────────────────────────────────────────────────────
def no_send(s: Setup, code: str) -> None:
    assert s.uploads() == [] and s.client.uploads == {}
    assert s.post.status == "skipped" and s.post.error.startswith(code)
    assert s.post.idempotency_key == f"void:{s.post_id}"  # the real key is free again
    assert ("post.cancelled", "warn", s.post_id) in s.store.events


def test_emergency_stop_switched_on_between_plan_and_send_sends_nothing(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.store.params = replace(s.store.params, emergency_stop=True)

    assert s.publish() is Outcome.CANCELLED
    no_send(s, "emergency_stop")


def test_dry_run_switched_on_between_plan_and_send_sends_nothing(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.store.params = replace(s.store.params, dry_run=True)

    assert s.publish() is Outcome.CANCELLED
    no_send(s, "dry_run_setting")


def test_before_launch_nothing_is_sent_even_with_dry_run_false(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    assert s.store.params.dry_run is False
    s.clock.now = LAUNCH_AT - timedelta(seconds=1)
    s.store.posts[s.post_id] = replace(s.post, created_at=s.clock.now, slot_at=LAUNCH_AT - timedelta(hours=1))

    assert s.publish() is Outcome.CANCELLED
    no_send(s, "before_launch")


def test_a_dry_run_row_is_never_sent(tmp_path: Path) -> None:
    s = Setup(tmp_path, dry_run_row=True)

    assert s.publish() is Outcome.NOOP
    assert s.uploads() == [] and s.post.status == "skipped" and s.post.dry_run is True


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"enabled": False}, "account_disabled"),
        ({"paused_until": NOW + timedelta(minutes=5)}, "account_paused"),
        ({"privacy": None}, "privacy_invalid"),
    ],
)
def test_an_account_that_closed_between_plan_and_send_gets_nothing(
    tmp_path: Path, changes: dict[str, Any], code: str
) -> None:
    s = Setup(tmp_path, **changes)

    assert s.publish() is Outcome.CANCELLED
    no_send(s, code)


@pytest.mark.parametrize(
    ("account_id", "changes", "code"),
    [
        (YT, {"privacy": "public"}, "staging_privacy_not_private"),
        (TT, {"privacy": "PUBLIC_TO_EVERYONE"}, "staging_privacy_not_private"),
        (IG, {}, "staging_instagram_not_allowed"),
    ],
)
def test_staging_never_sends_a_post_that_could_be_public(
    tmp_path: Path, account_id: str, changes: dict[str, Any], code: str
) -> None:
    s = Setup(tmp_path, account_id, app_env="staging", **changes)

    assert s.publish() is Outcome.CANCELLED
    no_send(s, code)


def test_staging_sends_a_private_youtube_post(tmp_path: Path) -> None:
    s = Setup(tmp_path, YT, app_env="staging", privacy="private")

    assert s.publish() is Outcome.PUBLISHED
    assert s.uploads()[0]["fields"]["privacyStatus"] == "private"


def test_a_clip_that_is_no_longer_approved_is_not_sent(tmp_path: Path) -> None:
    s = Setup(tmp_path, clip=hourly_clip("clip-1", at=NOW, status="removed"))

    assert s.publish() is Outcome.CANCELLED
    no_send(s, "clip_not_approved")


def test_an_hourly_post_is_not_sent_after_its_slot(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.clock.now = SLOT + timedelta(hours=1)

    assert s.publish() is Outcome.CANCELLED
    no_send(s, "too_late")


def test_an_hourly_post_planned_late_is_not_sent_in_the_next_slot(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.store.posts[s.post_id] = replace(s.post, created_at=SLOT + timedelta(minutes=58))
    s.clock.now = SLOT + timedelta(minutes=61)

    assert s.publish() is Outcome.CANCELLED
    no_send(s, "slot_expired")


def test_a_spike_post_is_not_sent_when_the_spike_is_too_old(tmp_path: Path) -> None:
    s = Setup(tmp_path, IG, clip=spike_clip("clip-1", at=NOW))
    s.clock.advance(1800 - 240 + 1)
    s.store.posts[s.post_id] = replace(s.post, created_at=s.clock.now)

    assert s.publish() is Outcome.CANCELLED
    no_send(s, "spike_too_old")


def test_the_last_line_of_defence_in_the_client_cancels_the_post(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.client.upload_error = PostingBlocked("launch.dry_run is on")

    assert s.publish() is Outcome.CANCELLED
    assert s.post.status == "skipped" and s.client.uploads == {}


def test_unusable_settings_cancel_the_post(tmp_path: Path) -> None:
    s = Setup(tmp_path)

    def broken() -> Any:
        raise KeyError("missing setting posting.platform_max_24h")

    s.store.load_params = broken  # type: ignore[method-assign]

    assert s.publish() is Outcome.CANCELLED
    assert s.uploads() == []


# ── problems on our side ──────────────────────────────────────────────────────────────────────────────────
def test_a_missing_video_file_fails_the_post_without_an_upload(tmp_path: Path) -> None:
    s = Setup(tmp_path, video=False)

    assert s.publish() is Outcome.FAILED
    assert s.uploads() == [] and "video file is missing" in s.post.error


def test_a_clip_without_a_creator_handle_fails_without_an_upload(tmp_path: Path) -> None:
    s = Setup(tmp_path, clip=hourly_clip("clip-1", at=NOW, creator_handle=""))

    assert s.publish() is Outcome.FAILED
    assert s.uploads() == [] and "upload fields refused" in s.post.error


def test_the_api_key_never_reaches_events_or_alerts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPLOAD_POST_API_KEY", "sk-super-secret")
    s = Setup(tmp_path)
    s.client.upload_error = AccountAuthError("POST /api/upload: HTTP 401: invalid api key")

    s.publish()

    text = repr(s.store.events) + repr(s.store.notifications) + repr(s.post)
    assert "sk-super-secret" not in text
