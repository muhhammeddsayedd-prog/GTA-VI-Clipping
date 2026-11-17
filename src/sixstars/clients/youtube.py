"""Client for YouTube: channel RSS feeds, the Data API with an API key for public data, and OAuth for the
private-upload claim pre-check. Implemented in M02.

Sources (checked 2026-11-17):
- Data API v3: https://developers.google.com/youtube/v3/docs (videos.list, channels.list, videos.insert,
  videos.delete)
- quota: https://developers.google.com/youtube/v3/determine_quota_cost (list 1 unit, delete 50 units,
  videos.insert 1 unit in its own bucket of 100 calls per day)
- search.list: https://developers.google.com/youtube/v3/docs/search/list . Since the revision of
  2026-06-01 (https://developers.google.com/youtube/v3/revision_history) it has its own quota bucket:
  "Projects that enable the YouTube Data API have a default quota allocation of 100 `search.list` calls,
  100 `videos.insert` calls, and 10,000 units per day combined for all other endpoints", one unit per
  call (it used to cost 100 units of the shared 10,000). Checked 2026-11-17.
- resumable upload: https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol
- OAuth for desktop apps: https://developers.google.com/youtube/v3/guides/auth/installed-apps

The OAuth refresh token is optional (decision D103): `has_oauth()` says whether the owner-channel methods
can be used. The API key is sent in the `X-goog-api-key` header so it never appears in a URL or a log.
"""

import base64
import hashlib
import secrets
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from sixstars import usage
from sixstars.clients import _http
from sixstars.clients._http import ApiError, NotConfiguredError
from sixstars.config import env

SERVICE = "youtube"
API_BASE = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
FEED_URL = "https://www.youtube.com/feeds/videos.xml"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
)
IDS_PER_REQUEST_MAX = 50
TOKEN_REFRESH_MARGIN_S = 300.0
QUOTA_LIST = 1
QUOTA_DELETE = 50
QUOTA_INSERT = 1  # counted in the separate videos.insert bucket (100 calls per day by default)
QUOTA_SEARCH = 1  # counted in the separate search.list bucket (100 calls per day by default)
SEARCH_RESULTS_MAX = 50
SEARCH_EVENT_TYPES = ("live", "completed", "upcoming")
SEARCH_ORDERS = ("date", "rating", "relevance", "title", "videoCount", "viewCount")
SEARCH_DURATIONS = ("any", "long", "medium", "short")
CATEGORY_GAMING = "20"

DEFAULT_VIDEO_PARTS = ("snippet", "status", "contentDetails", "statistics", "liveStreamingDetails")
DEFAULT_CHANNEL_PARTS = ("snippet", "statistics")

_ATOM = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}


@dataclass(frozen=True)
class FeedEntry:
    """One video from a channel's RSS feed."""

    video_id: str
    channel_id: str
    title: str
    published_at: datetime
    updated_at: datetime | None = None


@dataclass(frozen=True)
class YouTubeChannel:
    """The channel an OAuth token belongs to."""

    id: str
    title: str


@dataclass(frozen=True)
class YouTubeVideoStatus:
    """Processing, privacy and restriction state of one of our own uploads (used by the claim pre-check)."""

    video_id: str
    upload_status: str | None
    privacy_status: str | None
    rejection_reason: str | None
    failure_reason: str | None
    license: str | None
    licensed_content: bool | None
    region_blocked: tuple[str, ...]
    region_allowed: tuple[str, ...] | None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def is_restricted(self) -> bool:
        """True when the video is rejected, or blocked or limited in any country."""
        return (
            self.upload_status in ("rejected", "failed")
            or bool(self.region_blocked)
            or self.region_allowed is not None
        )


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def parse_feed(xml_text: str) -> list[FeedEntry]:
    """Parse a channel Atom feed into entries, newest first as YouTube sends them."""
    root = ET.fromstring(xml_text)
    entries: list[FeedEntry] = []
    for entry in root.findall("atom:entry", _ATOM):
        video_id = entry.findtext("yt:videoId", default="", namespaces=_ATOM)
        published = _parse_time(entry.findtext("atom:published", default="", namespaces=_ATOM))
        if not video_id or published is None:
            continue
        entries.append(
            FeedEntry(
                video_id=video_id,
                channel_id=entry.findtext("yt:channelId", default="", namespaces=_ATOM),
                title=entry.findtext("atom:title", default="", namespaces=_ATOM),
                published_at=published,
                updated_at=_parse_time(entry.findtext("atom:updated", default="", namespaces=_ATOM)),
            )
        )
    return entries


def parse_video_status(item: dict[str, Any]) -> YouTubeVideoStatus:
    """Build a `YouTubeVideoStatus` from a `videos.list` item with parts `status,contentDetails`."""
    status = item.get("status") or {}
    details = item.get("contentDetails") or {}
    restriction = details.get("regionRestriction") or {}
    allowed = restriction.get("allowed")
    return YouTubeVideoStatus(
        video_id=str(item.get("id", "")),
        upload_status=status.get("uploadStatus"),
        privacy_status=status.get("privacyStatus"),
        rejection_reason=status.get("rejectionReason"),
        failure_reason=status.get("failureReason"),
        license=status.get("license"),
        licensed_content=details.get("licensedContent"),
        region_blocked=tuple(restriction.get("blocked") or ()),
        region_allowed=tuple(allowed) if allowed is not None else None,
        raw=item,
    )


# ── OAuth helpers (used by `sixstars youtube auth`) ──────────────────


def new_pkce_pair() -> tuple[str, str]:
    """Return a PKCE `(code_verifier, code_challenge)` pair using the S256 method."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_auth_url(client_id: str, redirect_uri: str, state: str, code_challenge: str) -> str:
    """Authorization URL of the desktop-app flow for the upload and manage scopes."""
    query = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urlencode(query)}"


def exchange_code(
    client_id: str,
    client_secret: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    http: httpx.Client | None = None,
) -> dict[str, Any]:
    """Exchange an authorization code for tokens. The result holds `refresh_token` and `access_token`."""
    client = http or _http.new_client()
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "code_verifier": code_verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    endpoint = "POST /token"
    response = _http.send(SERVICE, endpoint, partial(client.post, TOKEN_URL, data=data))
    _http.raise_for_status(SERVICE, endpoint, response)
    body = response.json()
    assert isinstance(body, dict)
    return body


class YouTubeClient:
    """YouTube access: public data with the API key, RSS feeds without any key, and the owner channel
    with OAuth."""

    def __init__(
        self,
        api_key: str | None = None,
        oauth_client_id: str | None = None,
        oauth_client_secret: str | None = None,
        oauth_refresh_token: str | None = None,
        http: httpx.Client | None = None,
        upload_http: httpx.Client | None = None,
    ) -> None:
        settings = env()

        def secret(value: Any) -> str | None:
            return value.get_secret_value() if value is not None else None

        self._api_key = api_key or secret(settings.YOUTUBE_API_KEY)
        self._oauth_client_id = oauth_client_id or settings.YOUTUBE_OAUTH_CLIENT_ID
        self._oauth_client_secret = oauth_client_secret or secret(settings.YOUTUBE_OAUTH_CLIENT_SECRET)
        self._refresh_token = oauth_refresh_token or secret(settings.YOUTUBE_OAUTH_REFRESH_TOKEN)
        self._http = http or _http.new_client()
        self._upload_http = upload_http or http or _http.new_client(_http.UPLOAD_TIMEOUT)
        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._access_expires_at = 0.0

    def is_configured(self) -> bool:
        """True when the API key for public data is present."""
        return bool(self._api_key)

    def has_oauth(self) -> bool:
        """True when the OAuth client and a refresh token are present (owner-channel methods usable)."""
        return bool(self._oauth_client_id and self._oauth_client_secret and self._refresh_token)

    # ── API key: public data ─────────────────────────────────

    def _list(self, resource: str, ids: Sequence[str], parts: Sequence[str]) -> list[dict[str, Any]]:
        if not self._api_key:
            raise NotConfiguredError("YOUTUBE_API_KEY is not set")
        endpoint = f"GET /youtube/v3/{resource}"
        items: list[dict[str, Any]] = []
        unique = list(dict.fromkeys(ids))
        requests = 0
        for start in range(0, len(unique), IDS_PER_REQUEST_MAX):
            chunk = unique[start : start + IDS_PER_REQUEST_MAX]
            params: dict[str, str | int] = {
                "part": ",".join(parts),
                "id": ",".join(chunk),
                "maxResults": IDS_PER_REQUEST_MAX,
            }
            call = partial(
                self._http.get,
                f"{API_BASE}/{resource}",
                params=params,
                headers={"X-goog-api-key": self._api_key},
            )
            response = _http.send(SERVICE, endpoint, call)
            requests += 1
            _http.raise_for_status(SERVICE, endpoint, response)
            items.extend(response.json().get("items") or [])
        if requests:
            usage.record(SERVICE, f"{resource}.list", units=requests * QUOTA_LIST)
        return items

    def videos_list(
        self, ids: Sequence[str], parts: Sequence[str] = DEFAULT_VIDEO_PARTS
    ) -> list[dict[str, Any]]:
        """`videos.list` for up to any number of ids (50 per request). Returns the raw API items; ids
        that do not exist or are private are simply missing from the result."""
        return self._list("videos", ids, parts)

    def channels_list(
        self, ids: Sequence[str], parts: Sequence[str] = DEFAULT_CHANNEL_PARTS
    ) -> list[dict[str, Any]]:
        """`channels.list` by channel id (50 per request). Returns the raw API items."""
        return self._list("channels", ids, parts)

    def search_list(
        self,
        q: str,
        *,
        event_type: str | None = None,
        order: str = "relevance",
        published_after: datetime | None = None,
        video_category_id: str | None = CATEGORY_GAMING,
        relevance_language: str | None = None,
        video_duration: str | None = None,
        max_results: int = SEARCH_RESULTS_MAX,
    ) -> list[dict[str, Any]]:
        """One `search.list` call for videos (`type=video`, `part=snippet`): one page, at most 50 results.

        `q` may use the documented operators: `a|b` matches either term, `-word` excludes. `event_type`
        is `live`, `completed` or `upcoming` (None: any video); `order` one of `SEARCH_ORDERS`;
        `published_after` limits to videos created at or after that time; `video_duration` is `any`,
        `long` (over 20 minutes), `medium` (4 to 20 minutes) or `short`. Returns the raw result items
        (`id.videoId`, `snippet.publishedAt|channelId|title|description|channelTitle|liveBroadcastContent`);
        statistics and durations are not part of a search result and come from `videos_list`.

        Each call is one unit of the separate `search.list` bucket (100 calls per day by default), recorded
        as operation `search.list`. The caller owns the budget: this method never pages.
        """
        if not self._api_key:
            raise NotConfiguredError("YOUTUBE_API_KEY is not set")
        if event_type is not None and event_type not in SEARCH_EVENT_TYPES:
            raise ValueError(f"event_type must be one of {SEARCH_EVENT_TYPES}, not {event_type!r}")
        if order not in SEARCH_ORDERS:
            raise ValueError(f"order must be one of {SEARCH_ORDERS}, not {order!r}")
        if video_duration is not None and video_duration not in SEARCH_DURATIONS:
            raise ValueError(f"video_duration must be one of {SEARCH_DURATIONS}, not {video_duration!r}")
        params: dict[str, str | int] = {
            "part": "snippet",
            "type": "video",  # required by eventType, videoCategoryId and videoDuration
            "q": q,
            "order": order,
            "maxResults": max(1, min(int(max_results), SEARCH_RESULTS_MAX)),
        }
        if event_type is not None:
            params["eventType"] = event_type
        if published_after is not None:
            # RFC 3339, as the docs ask: 1970-01-01T00:00:00Z
            params["publishedAfter"] = published_after.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if video_category_id:
            params["videoCategoryId"] = video_category_id
        if relevance_language:
            params["relevanceLanguage"] = relevance_language
        if video_duration is not None and video_duration != "any":
            params["videoDuration"] = video_duration
        endpoint = "GET /youtube/v3/search"
        call = partial(
            self._http.get,
            f"{API_BASE}/search",
            params=params,  # httpx URL-escapes the pipe of an OR query (%7C), as the docs require
            headers={"X-goog-api-key": self._api_key},
        )
        response = _http.send(SERVICE, endpoint, call)
        # A refused call (quota, bad filter) is recorded too: Google counts refused calls against quota.
        usage.record(SERVICE, "search.list", units=QUOTA_SEARCH)
        _http.raise_for_status(SERVICE, endpoint, response)
        items = response.json().get("items") or []
        return [item for item in items if isinstance(item, dict)]

    # ── RSS ──────────────────────────────────────────────────

    def channel_feed(self, channel_id: str) -> list[FeedEntry]:
        """Recent uploads of a channel from its public Atom feed (no quota). Unknown channel: empty list."""
        endpoint = "GET /feeds/videos.xml"
        call = partial(self._http.get, FEED_URL, params={"channel_id": channel_id})
        response = _http.send(SERVICE, endpoint, call)
        usage.record(SERVICE, "channel_feed", units=0)
        if response.status_code == 404:
            return []
        _http.raise_for_status(SERVICE, endpoint, response)
        return parse_feed(response.text)

    # ── OAuth: owner channel ─────────────────────────────────

    def _refresh_access_token(self) -> str:
        if not self.has_oauth():
            raise NotConfiguredError(
                "YouTube OAuth is not configured: set YOUTUBE_OAUTH_CLIENT_ID, YOUTUBE_OAUTH_CLIENT_SECRET "
                "and YOUTUBE_OAUTH_REFRESH_TOKEN (create the token with `sixstars youtube auth`)"
            )
        data = {
            "client_id": self._oauth_client_id,
            "client_secret": self._oauth_client_secret,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
        }
        endpoint = "POST /token"
        response = _http.send(SERVICE, endpoint, partial(self._http.post, TOKEN_URL, data=data))
        _http.raise_for_status(SERVICE, endpoint, response)
        body = response.json()
        self._access_token = str(body["access_token"])
        self._access_expires_at = time.monotonic() + float(body.get("expires_in", 0))
        return self._access_token

    def _oauth_token(self, force: bool = False) -> str:
        with self._lock:
            fresh = time.monotonic() < self._access_expires_at - TOKEN_REFRESH_MARGIN_S
            if force or self._access_token is None or not fresh:
                return self._refresh_access_token()
            return self._access_token

    def _oauth_request(
        self,
        method: str,
        url: str,
        endpoint: str,
        client: httpx.Client | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """One authorised request. A 401 forces one token refresh and one more try."""
        extra_headers = kwargs.pop("headers", {})
        http = client or self._http
        response: httpx.Response | None = None
        for force in (False, True):
            headers = {**extra_headers, "Authorization": f"Bearer {self._oauth_token(force=force)}"}
            response = _http.send(
                SERVICE, endpoint, partial(http.request, method, url, headers=headers, **kwargs)
            )
            if response.status_code != 401:
                break
        assert response is not None
        return response

    def whoami(self) -> YouTubeChannel:
        """The channel the refresh token belongs to."""
        endpoint = "GET /youtube/v3/channels"
        response = self._oauth_request(
            "GET", f"{API_BASE}/channels", endpoint, params={"part": "snippet", "mine": "true"}
        )
        _http.raise_for_status(SERVICE, endpoint, response)
        usage.record(SERVICE, "channels.list", units=QUOTA_LIST)
        items = response.json().get("items") or []
        if not items:
            raise ApiError(SERVICE, endpoint, 404, "the signed-in Google account has no YouTube channel")
        return YouTubeChannel(id=str(items[0]["id"]), title=str(items[0]["snippet"]["title"]))

    def upload_private(self, path: Path, title: str, description: str) -> str:
        """Upload a video as **private** with the resumable protocol and return its video id.

        Only private uploads are made from our own project (pre-check copies); public posting goes
        through Upload-Post. The file is streamed, never read into memory or logged.
        """
        size = path.stat().st_size
        metadata = {
            "snippet": {"title": title[:100], "description": description, "categoryId": CATEGORY_GAMING},
            "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False},
        }
        start_endpoint = "POST /upload/youtube/v3/videos"
        start = self._oauth_request(
            "POST",
            UPLOAD_URL,
            start_endpoint,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={"X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/*"},
            json=metadata,
        )
        _http.raise_for_status(SERVICE, start_endpoint, start)
        session_url = start.headers.get("Location")
        if not session_url:
            raise ApiError(SERVICE, start_endpoint, start.status_code, "no upload session URL returned")

        put_endpoint = "PUT /upload/youtube/v3/videos"

        def put() -> httpx.Response:
            with path.open("rb") as handle:
                return self._upload_http.put(
                    session_url,
                    content=handle,
                    headers={
                        "Authorization": f"Bearer {self._oauth_token()}",
                        "Content-Length": str(size),
                        "Content-Type": "video/*",
                    },
                )

        response = _http.send(SERVICE, put_endpoint, put)
        _http.raise_for_status(SERVICE, put_endpoint, response)
        usage.record(SERVICE, "videos.insert", units=QUOTA_INSERT)
        return str(response.json()["id"])

    def get_status(self, video_id: str) -> YouTubeVideoStatus | None:
        """Status and region restriction of one of our own videos (private ones included); None when
        the video does not exist."""
        endpoint = "GET /youtube/v3/videos"
        response = self._oauth_request(
            "GET", f"{API_BASE}/videos", endpoint, params={"part": "status,contentDetails", "id": video_id}
        )
        _http.raise_for_status(SERVICE, endpoint, response)
        usage.record(SERVICE, "videos.list", units=QUOTA_LIST, ref_id=video_id)
        items = response.json().get("items") or []
        return parse_video_status(items[0]) if items else None

    def delete_video(self, video_id: str) -> bool:
        """Delete one of our own videos. Returns False when it was already gone."""
        endpoint = "DELETE /youtube/v3/videos"
        response = self._oauth_request("DELETE", f"{API_BASE}/videos", endpoint, params={"id": video_id})
        usage.record(SERVICE, "videos.delete", units=QUOTA_DELETE, ref_id=video_id)
        if response.status_code == 404:
            return False
        _http.raise_for_status(SERVICE, endpoint, response)
        return True
