"""Upload-Post client: the upload request, status polling, error mapping, analytics paths and delete."""

from pathlib import Path

import httpx
import pytest
import respx

from sixstars.clients import upload_post
from sixstars.clients._http import NotConfiguredError
from sixstars.clients.upload_post import (
    AccountAuthError,
    NotSupported,
    PostingBlocked,
    PostRejected,
    RateLimited,
    UploadPostClient,
    UploadPostError,
    classify_error,
)

UPLOAD_URL = f"{upload_post.API_BASE}/api/upload"
STATUS_URL = f"{upload_post.API_BASE}/api/uploadposts/status"
ACCEPTED = {
    "success": True,
    "message": "Upload initiated successfully in background.",
    "request_id": "req-1",
    "total_platforms": 1,
}


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"MP4DATA")
    return path


def _client() -> UploadPostClient:
    return UploadPostClient(api_key="up-key", posting_guard=None)


def _upload(client: UploadPostClient, clip: Path, **fields: object) -> upload_post.UploadStatus:
    return client.upload_video(
        "default",
        "tiktok",
        clip,
        {"title": "No way", **fields},
        idempotency_key="post:42:tiktok_main",
        request_id="req-1",
    )


@respx.mock
def test_upload_sends_documented_fields_and_headers(clip: Path) -> None:
    route = respx.post(UPLOAD_URL).mock(return_value=httpx.Response(200, json=ACCEPTED))
    status = _upload(_client(), clip, privacy_level="SELF_ONLY", disable_duet=False, first_comment=None)

    assert status.request_id == "req-1" and status.status == "pending" and status.total == 1
    assert status.finished is False
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Apikey up-key"
    assert request.headers["Idempotency-Key"] == "post:42:tiktok_main"
    body = request.content
    for name, value in {
        "user": "default",
        "platform[]": "tiktok",
        "title": "No way",
        "privacy_level": "SELF_ONLY",
        "disable_duet": "false",
        "async_upload": "true",
        "request_id": "req-1",
    }.items():
        assert f'name="{name}"\r\n\r\n{value}\r\n'.encode() in body
    assert b'name="first_comment"' not in body
    assert b'name="video"; filename="clip.mp4"' in body and b"MP4DATA" in body


@respx.mock
def test_upload_retries_5xx_with_the_same_idempotency_key(clip: Path, sleeps: list[float]) -> None:
    route = respx.post(UPLOAD_URL).mock(side_effect=[httpx.Response(502), httpx.Response(200, json=ACCEPTED)])
    assert _upload(_client(), clip).status == "pending"
    keys = {call.request.headers["Idempotency-Key"] for call in route.calls}
    assert route.call_count == 2 and keys == {"post:42:tiktok_main"}
    assert all(b"MP4DATA" in call.request.content for call in route.calls)


@respx.mock
def test_upload_429_is_rate_limited_without_retry(clip: Path, sleeps: list[float]) -> None:
    body = {"success": False, "message": "You have reached the daily limit of 15 uploads for: TikTok (x)"}
    route = respx.post(UPLOAD_URL).mock(
        return_value=httpx.Response(429, json=body, headers={"Retry-After": "3600"})
    )
    with pytest.raises(RateLimited) as raised:
        _upload(_client(), clip)
    assert route.call_count == 1 and sleeps == []
    assert raised.value.account == "default/tiktok" and raised.value.retry_after == 3600.0
    assert "daily limit" in str(raised.value)


@respx.mock
@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, AccountAuthError),
        (403, AccountAuthError),
        (400, PostRejected),
        (404, PostRejected),
        (422, PostRejected),
    ],
)
def test_upload_error_mapping(clip: Path, status: int, error: type[Exception]) -> None:
    respx.post(UPLOAD_URL).mock(
        return_value=httpx.Response(status, json={"success": False, "message": "nope"})
    )
    with pytest.raises(error, match="nope"):
        _upload(_client(), clip)


@respx.mock
def test_upload_500_after_retries_is_generic_error(clip: Path) -> None:
    respx.post(UPLOAD_URL).mock(return_value=httpx.Response(500, json={"success": False, "error": "kaput"}))
    with pytest.raises(UploadPostError, match="kaput"):
        _upload(_client(), clip)


@respx.mock
def test_synchronous_upload_response_is_parsed(clip: Path) -> None:
    body = {
        "success": True,
        "results": {
            "youtube": {"success": True, "url": "https://youtube.com/shorts/abc", "video_id": "abc"},
            "instagram": {"success": False, "error": "Expired access token"},
        },
    }
    respx.post(UPLOAD_URL).mock(return_value=httpx.Response(200, json=body))
    status = _upload(_client(), clip, async_upload=False)
    youtube = status.result_for("youtube")
    instagram = status.result_for("instagram")
    assert (
        youtube is not None
        and youtube.post_id == "abc"
        and youtube.post_url == "https://youtube.com/shorts/abc"
    )
    assert instagram is not None and instagram.success is False and instagram.error_kind == "auth"
    assert status.status == "completed" and status.total == 2


def test_posting_guard_blocks_before_any_request(clip: Path) -> None:
    def guard() -> None:
        raise PostingBlocked("launch.dry_run is on")

    with respx.mock(assert_all_called=False) as router:
        route = router.post(UPLOAD_URL).mock(return_value=httpx.Response(200, json=ACCEPTED))
        with pytest.raises(PostingBlocked):
            UploadPostClient(api_key="k", posting_guard=guard).upload_video(
                "default", "tiktok", clip, {}, "k", "r"
            )
        assert route.call_count == 0


def test_unconfigured(clip: Path) -> None:
    client = UploadPostClient(posting_guard=None)
    assert client.is_configured() is False
    with pytest.raises(NotConfiguredError):
        client.get_upload_status("r")


@respx.mock
def test_status_shapes() -> None:
    done = {
        "request_id": "req-1",
        "status": "completed",
        "completed": 1,
        "total": 1,
        "results": [
            {
                "platform": "tiktok",
                "success": True,
                "message": "Published",
                "platform_post_id": "7123",
                "post_url": "Video sent to Inbox (No Public URL)",
            }
        ],
    }
    route = respx.get(STATUS_URL).mock(return_value=httpx.Response(200, json=done))
    status = _client().get_upload_status("req-1")
    assert route.calls[0].request.url.params["request_id"] == "req-1"
    assert status.finished and status.completed == 1
    result = status.result_for("tiktok")
    assert (
        result is not None and result.post_id == "7123" and result.post_url is None and result.error is None
    )

    route.mock(
        return_value=httpx.Response(404, json={"status": "not_found", "message": "No upload request found"})
    )
    missing = _client().get_upload_status("nope")
    assert missing.status == "not_found" and missing.finished


@respx.mock
def test_wait_for_upload_polls_until_final(sleeps: list[float]) -> None:
    failed = {
        "request_id": "req-1",
        "status": "failed",
        "results": [
            {"platform": "tiktok", "success": False, "error": "spam_risk", "error_code": "content_rejected"}
        ],
    }
    route = respx.get(STATUS_URL).mock(
        side_effect=[
            httpx.Response(200, json={"request_id": "req-1", "status": "queued"}),
            httpx.Response(200, json={"request_id": "req-1", "status": "processing"}),
            httpx.Response(200, json=failed),
        ]
    )
    status = _client().wait_for_upload("req-1", timeout_s=600)
    assert route.call_count == 3 and sleeps == [5.0, 10.0, 10.0]
    result = status.result_for("tiktok")
    assert status.status == "failed" and result is not None and result.error_kind == "rejected"


@respx.mock
def test_wait_for_upload_gives_up_after_timeout(sleeps: list[float]) -> None:
    route = respx.get(STATUS_URL).mock(return_value=httpx.Response(200, json={"status": "processing"}))
    status = _client().wait_for_upload("req-1", timeout_s=30, poll_s=10)
    assert status.finished is False and status.request_id == "req-1"
    assert route.call_count == 3 and sum(sleeps) <= 30


@pytest.mark.parametrize(
    ("code", "message", "kind"),
    [
        ("account_reauth_required", "", "auth"),
        (None, "Your LinkedIn session has expired. Please reconnect", "auth"),
        ("account_restricted", "", "rate_limited"),
        (None, "You have reached the daily limit of 5 uploads", "rate_limited"),
        ("youtube_channel_daily_limit", "", "rate_limited"),
        ("content_rejected", "spam_risk", "rejected"),
        (None, None, "rejected"),
    ],
)
def test_classify_error(code: str | None, message: str | None, kind: str) -> None:
    assert classify_error(code, message) == kind


@respx.mock
def test_analytics_paths() -> None:
    profile = respx.get(f"{upload_post.API_BASE}/api/analytics/default").mock(
        return_value=httpx.Response(200, json={"tiktok": {"followers": 10}})
    )
    by_request = respx.get(f"{upload_post.API_BASE}/api/uploadposts/post-analytics/req-1").mock(
        return_value=httpx.Response(200, json={"success": True, "platforms": {}})
    )
    by_post = respx.get(f"{upload_post.API_BASE}/api/uploadposts/post-analytics").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    users = respx.get(f"{upload_post.API_BASE}/api/uploadposts/users").mock(
        return_value=httpx.Response(200, json={"success": True, "profiles": [{"username": "default"}]})
    )
    client = _client()
    assert client.get_profile_analytics("default", ["tiktok", "youtube"]) == {"tiktok": {"followers": 10}}
    assert profile.calls[0].request.url.params["platforms"] == "tiktok,youtube"
    assert client.get_post_analytics("req-1", platform="youtube")["success"] is True
    assert by_request.calls[0].request.url.params["platform"] == "youtube"
    client.get_post_analytics_by_platform_id("default", "instagram", "1789")
    params = by_post.calls[0].request.url.params
    assert (params["platform_post_id"], params["platform"], params["user"]) == (
        "1789",
        "instagram",
        "default",
    )
    assert client.list_profiles() == [{"username": "default"}]
    assert users.calls[0].request.headers["Authorization"] == "Apikey up-key"


@respx.mock
def test_get_is_retried_on_429(sleeps: list[float]) -> None:
    route = respx.get(f"{upload_post.API_BASE}/api/analytics/default").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200, json={})]
    )
    assert _client().get_profile_analytics("default", ["tiktok"]) == {}
    assert route.call_count == 2 and sleeps == [2.0]


@respx.mock
def test_delete_post() -> None:
    route = respx.post(f"{upload_post.API_BASE}/api/uploadposts/posts/unpublish").mock(
        side_effect=[
            httpx.Response(200, json={"success": True}),
            httpx.Response(404, json={"message": "no post"}),
        ]
    )
    client = _client()
    assert client.delete_post("default", "youtube", "dQw4w9WgXcQ") is True
    sent = httpx.Response(200, content=route.calls[0].request.content).json()
    assert sent == {"platform": "youtube", "user": "default", "post_id": "dQw4w9WgXcQ"}
    assert client.delete_post("default", "youtube", "gone") is False
    for platform in ("instagram", "tiktok"):
        with pytest.raises(NotSupported):
            client.delete_post("default", platform, "1")
    assert route.call_count == 2
