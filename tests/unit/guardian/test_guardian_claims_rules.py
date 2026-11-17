"""How the claims check reads the platforms: YouTube statuses, Upload-Post answers (in their real
shape), the two-pass rule for ambiguous answers, and which credential is used."""

from datetime import timedelta
from typing import Any

import pytest
from guardian_helpers import NOT_FOUND, NOW, YouTube, analytics, config, post, video_status

from sixstars.clients._http import ApiError
from sixstars.clients.fakes import FakeUploadPostClient
from sixstars.clients.upload_post import AccountAuthError, PostRejected, RateLimited
from sixstars.clients.youtube import YouTubeVideoStatus
from sixstars.guardian import claims
from sixstars.guardian.claims import Observation

# ── YouTube status ────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "privacy", "seen", "kind"),
    [
        (video_status(), "public", "ok", None),
        (None, "public", "gone", "removed_by_platform"),  # not listed any more
        (video_status(upload="deleted"), "public", "gone", "removed_by_platform"),
        (video_status(upload="rejected", rejection="copyright"), "public", "restricted", "claimed"),
        (video_status(upload="rejected"), "public", "restricted", "claimed"),
        (video_status(blocked=["DE", "FR"]), "public", "restricted", "blocked"),
        (video_status(allowed=["US"]), "public", "restricted", "blocked"),
        (video_status(allowed=[]), "public", "restricted", "blocked"),  # allowed nowhere
        (video_status(privacy="private"), "public", "restricted", "blocked"),  # we posted it public
        (video_status(privacy="unlisted"), "public", "restricted", "blocked"),
        (video_status(privacy="private"), "private", "ok", None),  # staging posts are private on purpose
        (video_status(upload="uploaded"), "public", "ok", None),  # still processing is not a problem
        (video_status(upload="failed"), "public", "ok", None),  # a processing failure is not a claim
    ],
)
def test_youtube_status_rules(
    status: YouTubeVideoStatus | None, privacy: str, seen: str, kind: str | None
) -> None:
    result = claims.youtube_observation(post(privacy=privacy), status, "test")
    assert (result.seen, result.kind) == (seen, kind)


def test_the_rejection_reason_and_the_countries_are_reported() -> None:
    rejected = claims.youtube_observation(post(), video_status(upload="rejected", rejection="copyright"), "t")
    assert "copyright" in rejected.detail
    blocked = claims.youtube_observation(post(), video_status(blocked=["DE", "FR"]), "t")
    assert "DE, FR" in blocked.detail


# ── Upload-Post answers ───────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("platform", "body", "seen"),
    [
        ("tiktok", analytics("tiktok", metrics={"views": 10}), "ok"),
        ("tiktok", analytics("tiktok", error=NOT_FOUND["tiktok"]), "gone"),
        ("youtube", analytics("youtube", error=NOT_FOUND["youtube"]), "gone"),
        # Instagram cannot tell a deleted post from an expired token: never "gone" on its own.
        ("instagram", analytics("instagram", error=NOT_FOUND["instagram"]), "ambiguous"),
        ("instagram", analytics("instagram", metrics={"views": 3, "likes": 1}), "ok"),
        ("tiktok", analytics("tiktok", error="Rate limit exceeded, try later"), "unknown"),
        ("tiktok", analytics("tiktok", error="Token expired"), "unknown"),
        ("tiktok", analytics("tiktok"), "unknown"),  # neither metrics nor an error
        ("tiktok", analytics("tiktok", metrics={}), "unknown"),
        ("tiktok", analytics("instagram", metrics={"views": 1}), "unknown"),  # entry of another platform
        ("tiktok", {"success": False, "platforms": {}}, "unknown"),
        ("tiktok", {}, "unknown"),
        ("tiktok", {"platforms": "nonsense"}, "unknown"),
        ("tiktok", {"post_metrics": {"views": 5}}, "ok"),  # flat shape (lookup by platform id, older docs)
        ("tiktok", {"post_metrics_error": "TikTok video not found (ID: 1)."}, "gone"),
        ("tiktok", analytics("tiktok", error="   "), "unknown"),
        ("tiktok", analytics("tiktok", error=None), "unknown"),
    ],
)
def test_upload_post_answers(platform: str, body: dict[str, Any], seen: str) -> None:
    assert claims.analytics_observation(post(platform=platform), body).seen == seen


def test_an_error_text_is_cut_and_kept_as_the_detail() -> None:
    result = claims.analytics_observation(
        post(platform="tiktok"), analytics("tiktok", error="not found " * 100)
    )
    assert result.seen == "gone" and len(result.detail) <= 300


# ── judging one pass ──────────────────────────────────────────────────────────────────────────────────────


def ambiguous(post_id: str, claim_status: str | None) -> Observation:
    ref = post(post_id, "instagram", claim_status=claim_status)
    return Observation(ref, "ambiguous", detail=NOT_FOUND["instagram"], source="upload_post")


def ok(post_id: str, platform: str) -> Observation:
    return Observation(post(post_id, platform), "ok", source="upload_post")


@pytest.mark.parametrize(
    ("claim_status", "other_ok_platform", "is_finding"),
    [
        (None, "instagram", False),  # first time: a suspect only
        ("ok", "instagram", False),
        ("suspect", "instagram", True),  # second pass in a row, and the token works for another post
        ("suspect", None, False),  # no other Instagram post read fine: it may be the token
        ("suspect", "tiktok", False),  # a healthy TikTok says nothing about the Instagram token
    ],
)
def test_an_ambiguous_answer_needs_two_passes_and_a_working_token(
    claim_status: str | None, other_ok_platform: str | None, is_finding: bool
) -> None:
    observations = [ambiguous("p1", claim_status)]
    if other_ok_platform:
        observations.append(ok("p2", other_ok_platform))
    findings, suspects = claims.judge(observations)
    assert [finding.post.id for finding in findings] == (["p1"] if is_finding else [])
    assert [item.post.id for item in suspects] == ([] if is_finding else ["p1"])
    if is_finding:
        assert findings[0].kind == "removed_by_platform" and findings[0].exists is False


def test_gone_and_restricted_posts_are_findings_and_unknown_ones_are_not() -> None:
    observations = [
        Observation(post("gone"), "gone", "removed_by_platform", "x", "t"),
        Observation(post("claimed"), "restricted", "claimed", "y", "t"),
        Observation(post("unknown"), "unknown", detail="timeout", source="t"),
        ok("fine", "youtube"),
    ]
    findings, suspects = claims.judge(observations)
    assert [(finding.post.id, finding.kind, finding.exists) for finding in findings] == [
        ("gone", "removed_by_platform", False),
        ("claimed", "claimed", True),
    ]
    assert suspects == []


@pytest.mark.parametrize(
    ("age", "checked"),
    [
        (timedelta(seconds=899), False),  # too fresh
        (timedelta(seconds=900), True),
        (timedelta(hours=71, minutes=59), True),
        (timedelta(hours=72), True),
        (timedelta(hours=72, seconds=1), False),  # older than the lookback
    ],
)
def test_the_check_window(age: timedelta, checked: bool) -> None:
    assert claims.in_check_window(NOW - age, NOW, 72, 900) is checked


# ── which credential reads YouTube ────────────────────────────────────────────────────────────────────────


def test_without_the_oauth_token_public_videos_are_read_with_the_api_key() -> None:
    youtube = YouTube(oauth=False)
    youtube.missing = {"gone0000001"}
    posts = [post("a", platform_post_id="here0000001"), post("b", platform_post_id="gone0000001")]
    errors: list[str] = []
    observations, leftover = claims.observe_youtube(posts, youtube, config(), errors)
    assert [(item.post.id, item.seen, item.source) for item in observations] == [
        ("a", "ok", "youtube_api_key"),
        ("b", "gone", "youtube_api_key"),
    ]
    assert leftover == [] and errors == []
    assert youtube.calls_to("videos_list")[0]["parts"] == ["status", "contentDetails"]
    assert youtube.calls_to("get_status") == []


def test_without_the_oauth_token_a_private_video_is_not_judged_by_the_api_key() -> None:
    # The public API does not list private videos: reading "absent" as "removed" would block a creator
    # for every staging post.
    youtube = YouTube(oauth=False)
    youtube.missing = {"priv0000001"}
    private = post("a", privacy="private", platform_post_id="priv0000001")
    observations, leftover = claims.observe_youtube([private], youtube, config(), [])
    assert observations == [] and leftover == [private]


def test_with_the_oauth_token_private_videos_are_read_too_with_no_code_change() -> None:
    youtube = YouTube(oauth=True)
    youtube.missing = {"priv0000002"}
    posts = [
        post("a", privacy="private", platform_post_id="priv0000001"),
        post("b", privacy="private", platform_post_id="priv0000002"),
    ]
    observations, leftover = claims.observe_youtube(posts, youtube, config(), [])
    assert [(item.post.id, item.seen, item.source) for item in observations] == [
        ("a", "ok", "youtube_oauth"),
        ("b", "gone", "youtube_oauth"),
    ]
    assert leftover == [] and youtube.calls_to("videos_list") == []


def test_a_broken_oauth_token_falls_back_to_the_api_key() -> None:
    youtube = YouTube(oauth=True)
    youtube.fail_oauth = ApiError("youtube", "GET /videos", 401, "invalid_grant")
    errors: list[str] = []
    observations, leftover = claims.observe_youtube([post("a")], youtube, config(), errors)
    assert [(item.seen, item.source) for item in observations] == [("ok", "youtube_api_key")]
    assert leftover == [] and errors == ["youtube oauth: ApiError"]


def test_an_api_error_is_never_read_as_a_removed_video() -> None:
    youtube = YouTube(oauth=False)
    youtube.fail_list = ApiError("youtube", "GET /videos", 403, "quotaExceeded")
    errors: list[str] = []
    observations, leftover = claims.observe_youtube([post("a"), post("b")], youtube, config(), errors)
    assert observations == [] and leftover == []
    assert errors == ["youtube api: ApiError"]


def test_a_post_without_a_video_id_is_unknown_not_gone() -> None:
    lost = post("a", platform_post_id=None, url=None)
    observations, _ = claims.observe_youtube([lost], YouTube(), config(), [])
    assert [(item.seen, item.detail) for item in observations] == [("unknown", "no video id stored")]


def test_the_video_id_is_read_from_the_link_when_the_platform_id_is_missing() -> None:
    youtube = YouTube()
    youtube.missing = {"abcDEF12345"}
    linked = post("a", platform_post_id=None, url="https://youtu.be/abcDEF12345")
    observations, _ = claims.observe_youtube([linked], youtube, config(), [])
    assert observations[0].seen == "gone"


def test_without_any_youtube_credential_the_posts_go_to_upload_post() -> None:
    public = post("a")
    observations, leftover = claims.observe_youtube([public], YouTube(configured=False), config(), [])
    assert observations == [] and leftover == [public]


# ── reading through Upload-Post ───────────────────────────────────────────────────────────────────────────


class UploadPost(FakeUploadPostClient):
    def __init__(self) -> None:
        super().__init__()
        self.by_request: dict[str, Any] = {}
        self.by_platform_id: dict[str, Any] = {}

    def get_post_analytics(self, request_id: str, platform: str | None = None) -> dict[str, Any]:
        self._record("get_post_analytics", request_id=request_id, platform=platform)
        answer = self.by_request.get(request_id, PostRejected("HTTP 404: Post not found."))
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)

    def get_post_analytics_by_platform_id(
        self, user: str, platform: str, platform_post_id: str
    ) -> dict[str, Any]:
        self._record("by_platform_id", user=user, platform=platform, platform_post_id=platform_post_id)
        answer = self.by_platform_id.get(platform_post_id, analytics(platform, metrics={"views": 1}))
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)


def tiktok(post_id: str, request_id: str | None = None, platform_post_id: str | None = None) -> Any:
    return post(post_id, "tiktok", request_id=request_id, platform_post_id=platform_post_id, url=None)


def test_a_post_is_read_by_request_id_then_by_platform_id() -> None:
    client = UploadPost()
    client.by_request["r1"] = analytics("tiktok", metrics={"views": 9})
    client.by_platform_id["777"] = analytics("tiktok", error=NOT_FOUND["tiktok"])
    posts = [tiktok("a", "r1", "111"), tiktok("b", "unknown-request", "777"), tiktok("c", None, None)]
    observations = claims.observe_upload_post(posts, client, [], deadline=10.0, clock=lambda: 0.0)
    assert [(item.post.id, item.seen) for item in observations] == [
        ("a", "ok"),
        ("b", "gone"),
        ("c", "unknown"),
    ]
    # The email of nobody and no id from outside is involved: only ids from our own rows are sent.
    assert [call["platform_post_id"] for call in client.calls_to("by_platform_id")] == ["777"]


def test_a_request_id_unknown_to_upload_post_is_not_evidence_of_removal() -> None:
    client = UploadPost()
    observations = claims.observe_upload_post(
        [tiktok("a", "gone-request", None)], client, [], 10.0, lambda: 0.0
    )
    assert [(item.seen, item.detail) for item in observations] == [("unknown", "unknown to Upload-Post")]


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (RateLimited("default/tiktok", 60.0, "too many"), "upload-post: rate limited"),
        (AccountAuthError("HTTP 401"), "upload-post: not authorised"),
    ],
)
def test_a_rate_limit_or_an_auth_error_ends_the_pass_without_findings(
    failure: Exception, message: str
) -> None:
    client = UploadPost()
    client.by_request["r1"] = analytics("tiktok", metrics={"views": 9})
    client.by_request["r2"] = failure
    errors: list[str] = []
    posts = [tiktok("a", "r1"), tiktok("b", "r2"), tiktok("c", "r3", "999")]
    observations = claims.observe_upload_post(posts, client, errors, 10.0, lambda: 0.0)
    assert [(item.post.id, item.seen) for item in observations] == [("a", "ok")]
    assert errors == [message]


def test_any_other_error_marks_only_that_post_unknown() -> None:
    client = UploadPost()
    client.by_request["r1"] = RuntimeError("boom")
    client.by_request["r2"] = analytics("tiktok", metrics={"views": 9})
    observations = claims.observe_upload_post(
        [tiktok("a", "r1"), tiktok("b", "r2")], client, [], 10.0, lambda: 0.0
    )
    assert [(item.post.id, item.seen) for item in observations] == [("a", "unknown"), ("b", "ok")]


def test_the_time_budget_stops_the_pass() -> None:
    client = UploadPost()
    ticks = iter([0.0, 5.0, 25.0, 30.0])
    errors: list[str] = []
    posts = [tiktok(name, f"r-{name}", "1") for name in "abcd"]
    observations = claims.observe_upload_post(posts, client, errors, deadline=20.0, clock=lambda: next(ticks))
    assert [item.post.id for item in observations] == ["a", "b"]
    assert len(errors) == 1 and "time budget" in errors[0]


def test_private_posts_are_unverifiable_unless_the_setting_allows_them() -> None:
    client = UploadPost()
    private = [
        post("yt", "youtube", privacy="private"),
        post("tt", "tiktok", privacy="SELF_ONLY", request_id=None, platform_post_id="5"),
    ]
    public = post("ig", "instagram", privacy=None, request_id=None, platform_post_id="6")
    youtube = YouTube(oauth=False)
    observations, unverifiable = claims.observe(
        [*private, public], client, youtube, config(), [], lambda: 0.0
    )
    assert [item.post.id for item in observations] == ["ig"] and unverifiable == 2

    observations, unverifiable = claims.observe(
        [*private, public], client, youtube, config(check_private_posts=True), [], lambda: 0.0
    )
    assert sorted(item.post.id for item in observations) == ["ig", "tt", "yt"] and unverifiable == 0


def test_a_post_waiting_for_manual_removal_is_always_read() -> None:
    client = UploadPost()
    waiting = post(
        "tt", "tiktok", privacy="SELF_ONLY", status="remove_failed", request_id=None, platform_post_id="5"
    )
    observations, unverifiable = claims.observe([waiting], client, YouTube(), config(), [], lambda: 0.0)
    assert [item.post.id for item in observations] == ["tt"] and unverifiable == 0
