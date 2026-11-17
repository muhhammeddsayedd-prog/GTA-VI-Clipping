"""Client for the Twitch Helix API using an app access token: streams, users, videos and game lookups. Twitch
IRC chat is handled separately in `sixstars.chat`. Implemented in M02.

Sources (checked 2026-11-17):
- token: https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/#client-credentials-grant-flow
- endpoints: https://dev.twitch.tv/docs/api/reference (Get Games, Get Streams, Get Videos, Get Users,
  Get Channel Followers)

Twitch keys are optional (decision D100). Nothing here touches the network or the environment at import
time; call `is_configured()` before using the client and skip Twitch sources when it returns False.
"""

import re
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any

import httpx

from sixstars import usage
from sixstars.clients import _http
from sixstars.clients._http import ApiError, NotConfiguredError
from sixstars.config import env

SERVICE = "twitch"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
API_BASE = "https://api.twitch.tv/helix"
TOKEN_REFRESH_MARGIN_S = 300.0
PAGE_SIZE_MAX = 100
IDS_PER_REQUEST_MAX = 100

_DURATION_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


@dataclass(frozen=True)
class TwitchStream:
    """One live stream from Get Streams."""

    id: str
    user_id: str
    user_login: str
    user_name: str
    game_id: str
    game_name: str
    title: str
    viewer_count: int
    started_at: datetime
    language: str
    tags: tuple[str, ...]
    thumbnail_url: str


@dataclass(frozen=True)
class TwitchVideo:
    """One video (VOD, highlight or upload) from Get Videos. `stream_id` is set for archives only."""

    id: str
    stream_id: str | None
    user_id: str
    user_login: str
    user_name: str
    title: str
    description: str
    created_at: datetime
    published_at: datetime | None
    url: str
    thumbnail_url: str
    view_count: int
    language: str
    type: str
    duration_s: int


@dataclass(frozen=True)
class TwitchUser:
    """One user from Get Users."""

    id: str
    login: str
    display_name: str
    description: str
    profile_image_url: str
    broadcaster_type: str
    created_at: datetime | None


def is_configured() -> bool:
    """True when both Twitch credentials are present in the environment."""
    settings = env()
    return bool(settings.TWITCH_CLIENT_ID) and settings.TWITCH_CLIENT_SECRET is not None


def parse_duration(value: str) -> int:
    """Convert a Twitch duration such as `1h0m5s` or `3m21s` to seconds (0 when it cannot be read)."""
    match = _DURATION_RE.match(value.strip())
    if match is None:
        return 0
    hours, minutes, seconds = (int(part) if part else 0 for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _stream(item: dict[str, Any]) -> TwitchStream:
    return TwitchStream(
        id=str(item["id"]),
        user_id=str(item["user_id"]),
        user_login=str(item.get("user_login", "")),
        user_name=str(item.get("user_name", "")),
        game_id=str(item.get("game_id", "")),
        game_name=str(item.get("game_name", "")),
        title=str(item.get("title", "")),
        viewer_count=int(item.get("viewer_count", 0)),
        started_at=_parse_time(item.get("started_at")) or datetime.now(UTC),
        language=str(item.get("language", "")),
        tags=tuple(item.get("tags") or ()),
        thumbnail_url=str(item.get("thumbnail_url", "")),
    )


def _video(item: dict[str, Any]) -> TwitchVideo:
    stream_id = item.get("stream_id")
    return TwitchVideo(
        id=str(item["id"]),
        stream_id=str(stream_id) if stream_id else None,
        user_id=str(item["user_id"]),
        user_login=str(item.get("user_login", "")),
        user_name=str(item.get("user_name", "")),
        title=str(item.get("title", "")),
        description=str(item.get("description", "")),
        created_at=_parse_time(item.get("created_at")) or datetime.now(UTC),
        published_at=_parse_time(item.get("published_at")),
        url=str(item.get("url", "")),
        thumbnail_url=str(item.get("thumbnail_url", "")),
        view_count=int(item.get("view_count", 0)),
        language=str(item.get("language", "")),
        type=str(item.get("type", "")),
        duration_s=parse_duration(str(item.get("duration", ""))),
    )


def _user(item: dict[str, Any]) -> TwitchUser:
    return TwitchUser(
        id=str(item["id"]),
        login=str(item.get("login", "")),
        display_name=str(item.get("display_name", "")),
        description=str(item.get("description", "")),
        profile_image_url=str(item.get("profile_image_url", "")),
        broadcaster_type=str(item.get("broadcaster_type", "")),
        created_at=_parse_time(item.get("created_at")),
    )


class TwitchClient:
    """Helix client. Thread-safe; one instance per process is enough."""

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        settings = env()
        self._client_id = client_id or settings.TWITCH_CLIENT_ID
        secret = settings.TWITCH_CLIENT_SECRET
        self._client_secret = client_secret or (secret.get_secret_value() if secret is not None else None)
        self._http = http or _http.new_client()
        self._lock = threading.Lock()
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._followers_denied = False

    def is_configured(self) -> bool:
        """True when this instance has a client id and secret."""
        return bool(self._client_id) and bool(self._client_secret)

    # ── token ────────────────────────────────────────────────

    def _fetch_token(self) -> str:
        if not self.is_configured():
            raise NotConfiguredError("TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are not set")
        response = _http.send(
            SERVICE,
            "POST /oauth2/token",
            lambda: self._http.post(
                TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "grant_type": "client_credentials",
                },
            ),
        )
        _http.raise_for_status(SERVICE, "POST /oauth2/token", response)
        body = response.json()
        self._token = str(body["access_token"])
        self._token_expires_at = time.monotonic() + float(body.get("expires_in", 0))
        return self._token

    def _get_token(self, force: bool = False) -> str:
        """Return the cached app token; fetch a new one when it is missing or within 5 minutes of expiry."""
        with self._lock:
            fresh = time.monotonic() < self._token_expires_at - TOKEN_REFRESH_MARGIN_S
            if force or self._token is None or not fresh:
                return self._fetch_token()
            return self._token

    # ── requests ─────────────────────────────────────────────

    def _request(self, path: str, params: Sequence[tuple[str, str | int]]) -> httpx.Response:
        """GET one Helix page. A 401 forces one token refresh and one more try."""
        endpoint = f"GET /helix{path}"
        response: httpx.Response | None = None
        for force in (False, True):
            token = self._get_token(force=force)
            headers = {"Client-Id": str(self._client_id), "Authorization": f"Bearer {token}"}
            call = partial(self._http.get, API_BASE + path, params=list(params), headers=headers)
            response = _http.send(SERVICE, endpoint, call)
            if response.status_code != 401:
                break
        assert response is not None
        return response

    def _get(self, path: str, params: Sequence[tuple[str, str | int]]) -> dict[str, Any]:
        response = self._request(path, params)
        _http.raise_for_status(SERVICE, f"GET /helix{path}", response)
        body = response.json()
        assert isinstance(body, dict)
        return body

    def _paginate(
        self, operation: str, path: str, params: Sequence[tuple[str, str | int]], max_items: int
    ) -> list[dict[str, Any]]:
        """Follow `pagination.cursor` until `max_items` rows are collected or the pages run out."""
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor: str | None = None
        requests = 0
        while len(items) < max_items:
            page_size = max(1, min(PAGE_SIZE_MAX, max_items - len(items)))
            page_params = [*params, ("first", page_size)]
            if cursor:
                page_params.append(("after", cursor))
            body = self._get(path, page_params)
            requests += 1
            data = body.get("data") or []
            for item in data:
                key = str(item.get("id"))
                if key not in seen:
                    seen.add(key)
                    items.append(item)
            cursor = (body.get("pagination") or {}).get("cursor")
            if not cursor or not data:
                break
        usage.record(SERVICE, operation, units=requests)
        return items[:max_items]

    # ── public methods ───────────────────────────────────────

    def get_game_id(self, name: str) -> str | None:
        """Return the category id for the exact game name, or None when Twitch has no such category."""
        body = self._get("/games", [("name", name)])
        usage.record(SERVICE, "get_game_id", units=1)
        data = body.get("data") or []
        return str(data[0]["id"]) if data else None

    def get_streams(self, game_id: str, max_items: int = 300) -> list[TwitchStream]:
        """Live streams in a category, most viewers first, following `after` up to `max_items`."""
        items = self._paginate("get_streams", "/streams", [("game_id", game_id), ("type", "live")], max_items)
        return [_stream(item) for item in items]

    def get_videos_by_game(
        self,
        game_id: str,
        period: str = "day",
        sort: str = "views",
        type: str = "archive",  # noqa: A002
        first: int = 50,
    ) -> list[TwitchVideo]:
        """Videos of a category (`period` all|day|week|month, `sort` time|trending|views)."""
        params = [("game_id", game_id), ("period", period), ("sort", sort), ("type", type)]
        return [_video(item) for item in self._paginate("get_videos_by_game", "/videos", params, first)]

    def get_videos_by_user(self, user_id: str, type: str = "archive", first: int = 5) -> list[TwitchVideo]:  # noqa: A002
        """The newest videos of one broadcaster."""
        params = [("user_id", user_id), ("type", type)]
        return [_video(item) for item in self._paginate("get_videos_by_user", "/videos", params, first)]

    def get_users(self, ids: Sequence[str]) -> list[TwitchUser]:
        """Users by id (description and profile image), 100 ids per request."""
        users: list[TwitchUser] = []
        unique = list(dict.fromkeys(ids))
        requests = 0
        for start in range(0, len(unique), IDS_PER_REQUEST_MAX):
            chunk = unique[start : start + IDS_PER_REQUEST_MAX]
            body = self._get("/users", [("id", user_id) for user_id in chunk])
            requests += 1
            users.extend(_user(item) for item in body.get("data") or [])
        if requests:
            usage.record(SERVICE, "get_users", units=requests)
        return users

    def get_followers_total(self, broadcaster_id: str) -> int | None:
        """Total followers of a channel, or None when the app token is not allowed to read it.

        The reference lists only a user access token for this endpoint, so a 401 or 403 is expected with
        some apps: it is remembered and later calls return None without a request.
        """
        if self._followers_denied:
            return None
        path = "/channels/followers"
        response = self._request(path, [("broadcaster_id", broadcaster_id), ("first", 1)])
        usage.record(SERVICE, "get_followers_total", units=1)
        if response.status_code in (401, 403):
            self._followers_denied = True
            return None
        try:
            _http.raise_for_status(SERVICE, f"GET /helix{path}", response)
        except ApiError as exc:
            if exc.status == 400:
                return None
            raise
        total = response.json().get("total")
        return int(total) if isinstance(total, int) else None
