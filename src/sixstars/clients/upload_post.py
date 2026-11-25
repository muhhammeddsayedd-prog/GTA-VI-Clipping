"""Client for the Upload-Post API that publishes clips to TikTok, Instagram Reels and YouTube Shorts, with
idempotency keys of the form `post:<clip_id>:<account_id>`. Implemented in M02.

Sources (checked 2026-11-17, raw pages under https://docs.upload-post.com/<page>.md):
- upload: `api/upload-video` (`POST /api/upload`, multipart, `Authorization: Apikey <key>`)
- status: `api/upload-status` (`GET /api/uploadposts/status?request_id=`)
- analytics: `api/get-analytics` (`GET /api/analytics/{profile}?platforms=`,
  `GET /api/uploadposts/post-analytics/{request_id}`,
  `GET /api/uploadposts/post-analytics?platform_post_id=&platform=&user=`)
- delete: `api/unpublish-post` (`POST /api/uploadposts/posts/unpublish`; not for Instagram or TikTok)
- profiles: `api/user-profiles` (`GET /api/uploadposts/users`)
- errors and limits: `guides/error-handling`, `guides/rate-limits`, `resources/common-errors`

An upload answers HTTP 200 even when a platform failed: the outcome of each platform is in `results`, so
always read `PlatformResult.success`. `upload_video` refuses to run while `control.emergency_stop` or
`launch.dry_run` is set (the posting engine decides what to do instead; this is the last line of defence).
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Literal

import httpx

from sixstars import settings, usage
from sixstars.clients import _http
from sixstars.clients._http import ClientError, NotConfiguredError
from sixstars.config import env

SERVICE = "upload_post"
API_BASE = "https://api.upload-post.com"
FINAL_STATUSES = frozenset({"completed", "failed", "not_found"})
POLL_INITIAL_DELAY_S = 5.0
POLL_INTERVAL_S = 10.0
NO_DELETE_PLATFORMS = frozenset({"instagram", "tiktok", "threads"})

ErrorKind = Literal["rate_limited", "auth", "rejected"]

_AUTH_CODES = frozenset(
    {
        "account_reauth_required",
        "account_checkpoint_required",
        "tiktok_reconnect_required",
        "facebook_reauth_required",
        "profile_platform_mapping_invalid",
    }
)
_RATE_CODES = frozenset(
    {"account_restricted", "youtube_channel_daily_limit", "reached_active_user_cap", "x_daily_write_limit"}
)
_AUTH_WORDS = ("expired", "reconnect", "re-connect", "reauth", "not authorized", "unauthorized", "revoked")
_RATE_WORDS = ("daily limit", "monthly limit", "rate limit", "too many", "reached the number of videos")


class UploadPostError(ClientError):
    """Upload-Post answered with an error this client has no narrower class for."""


class RateLimited(UploadPostError):
    """A rate limit or a daily or monthly cap was hit. `retry_after` is in seconds when the API says."""

    def __init__(self, account: str, retry_after: float | None, message: str = "") -> None:
        super().__init__(f"rate limited for {account}: {message}"[:400])
        self.account = account
        self.retry_after = retry_after


class AccountAuthError(UploadPostError):
    """The API key, the profile or a connected social account is not authorised (reconnect needed)."""


class PostRejected(UploadPostError):
    """The request or the content was rejected as invalid; retrying the same post will not help."""


class NotSupported(UploadPostError):
    """Upload-Post cannot do this (for example deleting an Instagram or TikTok post)."""


class PostingBlocked(UploadPostError):
    """Posting is switched off by `control.emergency_stop` or `launch.dry_run`."""


@dataclass(frozen=True)
class PlatformResult:
    """Outcome of one platform within an upload."""

    platform: str
    success: bool
    status: str | None = None
    post_id: str | None = None
    post_url: str | None = None
    error: str | None = None
    error_code: str | None = None
    skipped: bool = False
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def error_kind(self) -> ErrorKind | None:
        """How to treat a failed platform: `rate_limited` (wait), `auth` (reconnect) or `rejected`."""
        return None if self.success else classify_error(self.error_code, self.error)


@dataclass(frozen=True)
class UploadStatus:
    """State of an upload request. `status` is one of pending, queued, processing, in_progress,
    completed, failed, not_found."""

    request_id: str
    status: str
    completed: int = 0
    total: int = 0
    results: tuple[PlatformResult, ...] = ()
    message: str | None = None

    @property
    def finished(self) -> bool:
        return self.status in FINAL_STATUSES

    def result_for(self, platform: str) -> PlatformResult | None:
        return next((result for result in self.results if result.platform == platform), None)


def classify_error(error_code: str | None, message: str | None) -> ErrorKind:
    """Sort a per-platform failure into rate limit, account authorisation or rejection."""
    code = (error_code or "").lower()
    text = (message or "").lower()
    if code in _RATE_CODES or any(word in text for word in _RATE_WORDS):
        return "rate_limited"
    if code in _AUTH_CODES or any(word in text for word in _AUTH_WORDS):
        return "auth"
    return "rejected"


def _first_str(raw: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str | int) and not isinstance(value, bool) and str(value):
            return str(value)
    return None


def parse_platform_result(platform: str, raw: Mapping[str, Any]) -> PlatformResult:
    """Normalise one platform entry from an upload response or a status response."""
    url = _first_str(raw, ("post_url", "url"))
    return PlatformResult(
        platform=platform,
        success=bool(raw.get("success")),
        status=_first_str(raw, ("status",)),
        post_id=_first_str(raw, ("platform_post_id", "post_id", "video_id", "publish_id", "container_id")),
        post_url=url if url and url.startswith("http") else None,
        error=_first_str(raw, ("error", "error_message"))
        or (None if raw.get("success") else _first_str(raw, ("message",))),
        error_code=_first_str(raw, ("error_code",)),
        skipped=bool(raw.get("skipped")),
        raw=dict(raw),
    )


def parse_results(results: Any) -> tuple[PlatformResult, ...]:
    """Accept both shapes the API uses: a dict keyed by platform (upload) or a list (status)."""
    if isinstance(results, Mapping):
        return tuple(
            parse_platform_result(str(platform), raw)
            for platform, raw in results.items()
            if isinstance(raw, Mapping)
        )
    if isinstance(results, list):
        return tuple(
            parse_platform_result(str(raw.get("platform", "")), raw)
            for raw in results
            if isinstance(raw, Mapping)
        )
    return ()


def parse_status(request_id: str, body: Mapping[str, Any]) -> UploadStatus:
    """Build an `UploadStatus` from a status response or from a synchronous upload response."""
    results = parse_results(body.get("results"))
    status = body.get("status")
    if not isinstance(status, str):
        if results:
            status = "completed" if any(result.success for result in results) else "failed"
        else:
            status = "pending"
    message = body.get("message")
    return UploadStatus(
        request_id=str(body.get("request_id") or request_id),
        status=status,
        completed=int(body.get("completed") or 0),
        total=int(body.get("total") or body.get("total_platforms") or len(results)),
        results=results,
        message=message if isinstance(message, str) else None,
    )


def default_posting_guard() -> None:
    """Raise `PostingBlocked` when posting is switched off in the `settings` table."""
    if settings.get_bool("control.emergency_stop"):
        raise PostingBlocked("control.emergency_stop is on")
    if settings.get_bool("launch.dry_run"):
        raise PostingBlocked("launch.dry_run is on")


class UploadPostClient:
    """Upload-Post API client."""

    def __init__(
        self,
        api_key: str | None = None,
        http: httpx.Client | None = None,
        upload_http: httpx.Client | None = None,
        posting_guard: Callable[[], None] | None = default_posting_guard,
    ) -> None:
        configured = env().UPLOAD_POST_API_KEY
        self._api_key = api_key or (configured.get_secret_value() if configured is not None else None)
        self._http = http or _http.new_client()
        self._upload_http = upload_http or http or _http.new_client(_http.UPLOAD_TIMEOUT)
        self._posting_guard = posting_guard

    def is_configured(self) -> bool:
        """True when the API key is present."""
        return bool(self._api_key)

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise NotConfiguredError("UPLOAD_POST_API_KEY is not set")
        return {"Authorization": f"Apikey {self._api_key}"}

    def _raise(self, endpoint: str, response: httpx.Response, account: str) -> None:
        """Map an error response to the client's exception classes."""
        status = response.status_code
        if status < 400:
            return
        message = _http.error_message(response)
        if status == 429:
            retry_after = _http.retry_after_seconds(response)
            if retry_after is None:
                try:
                    body = response.json()
                    value = body.get("retry_after") if isinstance(body, dict) else None
                    retry_after = float(value) if isinstance(value, int | float) else None
                except ValueError:
                    retry_after = None
            raise RateLimited(account, retry_after, message)
        if status in (401, 403):
            raise AccountAuthError(f"{endpoint}: HTTP {status}: {message}")
        if status in (400, 404, 409, 422):
            raise PostRejected(f"{endpoint}: HTTP {status}: {message}")
        raise UploadPostError(f"{endpoint}: HTTP {status}: {message}")

    def _get(
        self, endpoint: str, path: str, params: Mapping[str, str] | None, account: str
    ) -> httpx.Response:
        call = partial(self._http.get, API_BASE + path, params=dict(params or {}), headers=self._headers())
        return _http.send(SERVICE, endpoint, call)

    # ── upload ───────────────────────────────────────────────

    def upload_video(
        self,
        user: str,
        platform: str,
        video_path: Path,
        fields: Mapping[str, Any],
        idempotency_key: str,
        request_id: str,
    ) -> UploadStatus:
        """Publish a video file to one platform of profile `user`.

        `fields` are the multipart fields from the docs: `title` (required), and per platform
        `privacy_level`, `youtube_title`, `youtube_description`, `privacyStatus`, `categoryId`,
        `media_type`, `first_comment`, ... Booleans are sent as `true`/`false`. The upload is asynchronous
        (`async_upload=true`) unless `fields` says otherwise: the returned status is usually `pending`
        and `wait_for_upload(request_id)` gives the per-platform result.

        The same `Idempotency-Key` and `request_id` are sent on every attempt, so a retry after a
        network error or a 5xx cannot create a second post. A 429 is not retried here: daily and monthly
        caps do not clear in seconds, so it is raised as `RateLimited` at once.
        """
        if self._posting_guard is not None:
            self._posting_guard()
        endpoint = "POST /api/upload"
        data: dict[str, str] = {"async_upload": "true"}
        for key, value in fields.items():
            if value is None:
                continue
            data[key] = ("true" if value else "false") if isinstance(value, bool) else str(value)
        data.update({"user": user, "platform[]": platform, "request_id": request_id})
        headers = {**self._headers(), "Idempotency-Key": idempotency_key}

        def call() -> httpx.Response:
            with video_path.open("rb") as handle:
                files = {"video": (video_path.name, handle, "video/mp4")}
                return self._upload_http.post(
                    API_BASE + "/api/upload", data=data, files=files, headers=headers
                )

        retry_statuses = (500, 502, 503, 504)
        response = _http.send(SERVICE, endpoint, call, retry_statuses=retry_statuses)
        self._raise(endpoint, response, account=f"{user}/{platform}")
        usage.record(SERVICE, "upload_video", units=1, ref_id=request_id)
        return parse_status(request_id, response.json())

    def upload_video_url(
        self,
        user: str,
        platform: str,
        video_url: str,
        fields: Mapping[str, Any],
        idempotency_key: str,
        request_id: str,
    ) -> UploadStatus:
        """Publish a video that Upload-Post fetches from `video_url` (M20 long-form episodes).

        Docs (upload-video, checked 2026-11-25): `video` may be "a file upload or a video URL"; a URL is
        fetched by Upload-Post (up to 20 GB) and with `async_upload=true` the `request_id` comes back within
        seconds whatever the size. List values in `fields` (YouTube `tags`) are sent as repeated `<key>[]`
        fields. Same idempotency and posting guard as `upload_video`.
        """
        if self._posting_guard is not None:
            self._posting_guard()
        endpoint = "POST /api/upload (url)"
        parts: list[tuple[str, tuple[None, str]]] = [("async_upload", (None, "true"))]
        for key, value in fields.items():
            if value is None:
                continue
            if isinstance(value, list | tuple):
                parts += [(f"{key}[]", (None, str(item))) for item in value]
            else:
                text = ("true" if value else "false") if isinstance(value, bool) else str(value)
                parts.append((key, (None, text)))
        parts += [
            ("user", (None, user)),
            ("platform[]", (None, platform)),
            ("request_id", (None, request_id)),
            ("video", (None, video_url)),
        ]
        headers = {**self._headers(), "Idempotency-Key": idempotency_key}

        def call() -> httpx.Response:
            return self._http.post(API_BASE + "/api/upload", files=parts, headers=headers)

        response = _http.send(SERVICE, endpoint, call, retry_statuses=(500, 502, 503, 504))
        self._raise(endpoint, response, account=f"{user}/{platform}")
        usage.record(SERVICE, "upload_video", units=1, ref_id=request_id)
        return parse_status(request_id, response.json())

    def get_upload_status(self, request_id: str) -> UploadStatus:
        """Status of an upload by request id. An unknown id gives status `not_found`."""
        endpoint = "GET /api/uploadposts/status"
        response = self._get(endpoint, "/api/uploadposts/status", {"request_id": request_id}, request_id)
        if response.status_code == 404:
            return UploadStatus(
                request_id=request_id, status="not_found", message=_http.error_message(response)
            )
        self._raise(endpoint, response, account=request_id)
        return parse_status(request_id, response.json())

    def wait_for_upload(
        self, request_id: str, timeout_s: float = 600.0, poll_s: float = POLL_INTERVAL_S
    ) -> UploadStatus:
        """Poll the status until it is final or `timeout_s` passes; returns the last status seen.

        A status that is still not final after the timeout is returned as it is (check `finished`). The
        docs advise polling no faster than every 5 to 10 seconds.
        """
        waited = min(POLL_INITIAL_DELAY_S, timeout_s)
        _http.sleep(waited)
        status = self.get_upload_status(request_id)
        while not status.finished and waited + poll_s <= timeout_s:
            _http.sleep(poll_s)
            waited += poll_s
            status = self.get_upload_status(request_id)
        return status

    # ── read-only ────────────────────────────────────────────

    def list_profiles(self) -> list[dict[str, Any]]:
        """Profiles of the account with their connected social accounts."""
        endpoint = "GET /api/uploadposts/users"
        response = self._get(endpoint, "/api/uploadposts/users", None, "account")
        self._raise(endpoint, response, account="account")
        usage.record(SERVICE, "list_profiles", units=1)
        body = response.json()
        profiles = body.get("profiles") if isinstance(body, dict) else body
        return [profile for profile in profiles or [] if isinstance(profile, dict)]

    def get_profile_analytics(self, user: str, platforms: Sequence[str]) -> dict[str, Any]:
        """Profile analytics (followers, reach, views) keyed by platform."""
        endpoint = "GET /api/analytics/{profile}"
        response = self._get(endpoint, f"/api/analytics/{user}", {"platforms": ",".join(platforms)}, user)
        self._raise(endpoint, response, account=user)
        usage.record(SERVICE, "get_profile_analytics", units=1)
        body = response.json()
        return body if isinstance(body, dict) else {}

    def get_post_analytics(self, request_id: str, platform: str | None = None) -> dict[str, Any]:
        """Per-post analytics by the **request id** of the upload; `platform` narrows it to one platform
        (faster). The answer has `post` and `platforms` (each with `platform_post_id`, `post_url`,
        `post_metrics`)."""
        endpoint = "GET /api/uploadposts/post-analytics/{request_id}"
        params = {"platform": platform} if platform else None
        response = self._get(endpoint, f"/api/uploadposts/post-analytics/{request_id}", params, request_id)
        self._raise(endpoint, response, account=request_id)
        usage.record(SERVICE, "get_post_analytics", units=1, ref_id=request_id)
        body = response.json()
        return body if isinstance(body, dict) else {}

    def get_post_analytics_by_platform_id(
        self, user: str, platform: str, platform_post_id: str
    ) -> dict[str, Any]:
        """Per-post analytics by the platform's own post id (limited to 100 requests per 5 minutes)."""
        endpoint = "GET /api/uploadposts/post-analytics"
        params = {"platform_post_id": platform_post_id, "platform": platform, "user": user}
        response = self._get(endpoint, "/api/uploadposts/post-analytics", params, user)
        self._raise(endpoint, response, account=f"{user}/{platform}")
        usage.record(SERVICE, "get_post_analytics", units=1, ref_id=platform_post_id)
        body = response.json()
        return body if isinstance(body, dict) else {}

    # ── thumbnails (cover engine, docs/29b) ─────────────────

    def set_youtube_thumbnail(self, user: str, video_id: str, thumbnail_url: str) -> bool:
        """Set or replace a YouTube video's thumbnail (`POST /api/uploadposts/youtube/thumbnail`, JSON).
        Returns True when it is set (200) and False while the video is still processing (202, retry
        later). A 403 (channel not allowed) raises `AccountAuthError`, a 429 (YouTube quota) `RateLimited`.
        Respects `control.emergency_stop` and `launch.dry_run` like an upload."""
        if self._posting_guard is not None:
            self._posting_guard()
        endpoint = "POST /api/uploadposts/youtube/thumbnail"
        payload = {"user": user, "video_id": video_id, "thumbnail_url": thumbnail_url}
        call = partial(
            self._http.post,
            API_BASE + "/api/uploadposts/youtube/thumbnail",
            json=payload,
            headers=self._headers(),
        )
        response = _http.send(SERVICE, endpoint, call, retry_statuses=(500, 502, 503, 504))
        self._raise(endpoint, response, account=f"{user}/youtube")
        usage.record(SERVICE, "set_youtube_thumbnail", units=1, ref_id=video_id)
        if response.status_code == 202:
            return False
        try:
            body = response.json()
        except ValueError:
            return True
        return not (isinstance(body, dict) and body.get("pending") is True)

    # ── delete ───────────────────────────────────────────────

    def delete_post(self, user: str, platform: str, post_id: str) -> bool:
        """Delete a published post. Works for YouTube; Upload-Post cannot delete Instagram or TikTok
        posts, so those raise `NotSupported` and the guardian handles them. Returns False when the post
        was already gone."""
        if platform in NO_DELETE_PLATFORMS:
            raise NotSupported(f"Upload-Post cannot delete {platform} posts")
        endpoint = "POST /api/uploadposts/posts/unpublish"
        payload = {"platform": platform, "user": user, "post_id": post_id}
        call = partial(
            self._http.post,
            API_BASE + "/api/uploadposts/posts/unpublish",
            json=payload,
            headers=self._headers(),
        )
        response = _http.send(SERVICE, endpoint, call)
        usage.record(SERVICE, "delete_post", units=1, ref_id=post_id)
        if response.status_code == 404:
            return False
        self._raise(endpoint, response, account=f"{user}/{platform}")
        return True
