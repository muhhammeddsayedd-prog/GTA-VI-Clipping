"""Builders for the guardian unit tests."""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sixstars.clients.fakes import FakeYouTubeClient
from sixstars.clients.youtube import YouTubeVideoStatus
from sixstars.guardian.config import GuardianConfig
from sixstars.guardian.removal import PostRef

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def post(
    post_id: str = "p1",
    platform: str = "youtube",
    *,
    creator: str = "c1",
    privacy: str | None = "public",
    platform_post_id: str | None = "vid00000001",
    request_id: str | None = "post:clip:acct",
    url: str | None = "https://www.youtube.com/shorts/vid00000001",
    status: str = "published",
    claim_status: str | None = None,
) -> PostRef:
    handles = {"youtube": "@6starsclips-m3x", "tiktok": "@6starsclipping", "instagram": "@6starsclippy"}
    return PostRef(
        id=post_id,
        clip_id=f"clip-{post_id}",
        creator_id=creator,
        creator_handle=f"Streamer_{creator}",
        account_id=f"{platform}_main",
        platform=platform,
        upload_post_user="default",
        account_handle=handles.get(platform, "@us"),
        privacy=privacy,
        platform_post_id=platform_post_id,
        upload_request_id=request_id,
        post_url=url,
        status=status,
        published_at=NOW,
        claim_status=claim_status,
        title="A clip",
    )


def config(**changes: Any) -> GuardianConfig:
    values: dict[str, Any] = {
        "check_lookback_h": 72.0,
        "check_min_age_s": 900.0,
        "check_max_posts": 80,
        "check_budget_s": 20.0,
        "check_private_posts": False,
        "mass_missing_min": 5,
        "mass_missing_ratio": 0.8,
        "strike_limit": 2,
        "strike_window_d": 7.0,
        "takedown_body_max_chars": 20000,
        "takedown_max_per_hour": 60,
        "handle_match_min_chars": 4,
    }
    values.update(changes)
    return GuardianConfig(**values)


def video_status(
    video_id: str = "vid00000001",
    *,
    upload: str = "processed",
    privacy: str = "public",
    rejection: str | None = None,
    blocked: Sequence[str] = (),
    allowed: Sequence[str] | None = None,
) -> YouTubeVideoStatus:
    return YouTubeVideoStatus(
        video_id=video_id,
        upload_status=upload,
        privacy_status=privacy,
        rejection_reason=rejection,
        failure_reason=None,
        license="youtube",
        licensed_content=False,
        region_blocked=tuple(blocked),
        region_allowed=None if allowed is None else tuple(allowed),
    )


class YouTube(FakeYouTubeClient):
    """The fake YouTube client with videos that can be missing, and calls that can fail."""

    def __init__(self, oauth: bool = False, configured: bool = True) -> None:
        super().__init__(oauth=oauth)
        self.configured = configured
        self.missing: set[str] = set()
        self.fail_oauth: Exception | None = None
        self.fail_list: Exception | None = None
        self.fail_delete: Exception | None = None
        self.deleted: list[str] = []

    def is_configured(self) -> bool:
        return self.configured

    def videos_list(self, ids: Sequence[str], parts: Sequence[str] = ("snippet",)) -> list[dict[str, Any]]:
        if self.fail_list is not None:
            raise self.fail_list
        return [item for item in super().videos_list(ids, parts) if item["id"] not in self.missing]

    def get_status(self, video_id: str) -> YouTubeVideoStatus | None:
        self._record("get_status", video_id=video_id)
        if self.fail_oauth is not None:
            raise self.fail_oauth
        if video_id in self.missing:
            return None
        return self.statuses.get(video_id, video_status(video_id))

    def delete_video(self, video_id: str) -> bool:
        self._record("delete_video", video_id=video_id)
        if self.fail_delete is not None:
            raise self.fail_delete
        if video_id in self.missing:
            return False
        self.deleted.append(video_id)
        return True


def analytics(
    platform: str, *, metrics: dict[str, Any] | None = None, error: str | None = None
) -> dict[str, Any]:
    """An Upload-Post post-analytics answer in the real shape (checked 2026-11-17)."""
    entry: dict[str, Any] = {"success": True, "platform_post_id": "x", "available_metrics": ["followers"]}
    if metrics is not None:
        entry["post_metrics"] = metrics
    if error is not None:
        entry["post_metrics_error"] = error
    return {
        "success": True,
        "post": {"platform": platform, "profile_username": "default"},
        "platforms": {platform: entry},
    }


NOT_FOUND = {
    "youtube": "YouTube video not found (ID: vid00000001).",
    "tiktok": "TikTok video not found (ID: 7000000000000000000).",
    "instagram": "Instagram API error (HTTP 400). The post may have been deleted or the token expired.",
}
