"""Client for Apify actors used by discovery to find trending YouTube creators and videos. Starts actor runs,
waits for them and reads dataset items. Implemented in M02.

Sources (checked 2026-11-17):
- API v2: https://docs.apify.com/api/v2 (run actor `POST /v2/acts/{actorId}/runs`, get run
  `GET /v2/actor-runs/{runId}`, dataset items `GET /v2/datasets/{datasetId}/items`)
- input schema of `streamers/youtube-scraper`: `GET /v2/acts/streamers~youtube-scraper/builds/default`
- output fields: two real runs on 2026-11-17

The actor is pay-per-event ($0.003 per video written on 2026-11-17), so every run is started with a
`maxTotalChargeUsd` cap. The token is sent as a Bearer header, never in the URL.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any

import httpx

from sixstars import usage
from sixstars.clients import _http
from sixstars.clients._http import ClientError, NotConfiguredError
from sixstars.config import env

SERVICE = "apify"
API_BASE = "https://api.apify.com/v2"
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"})
DEFAULT_MAX_CHARGE_USD = 1.0
DEFAULT_RUN_TIMEOUT_S = 300
POLL_INTERVAL_S = 5.0
RESULT_EVENT = "result"


class ApifyRunFailed(ClientError):
    """An actor run ended in a status other than SUCCEEDED, or did not finish in time."""

    def __init__(self, run_id: str, status: str) -> None:
        super().__init__(f"Apify run {run_id} ended with status {status}")
        self.run_id = run_id
        self.status = status


@dataclass(frozen=True)
class ApifyRun:
    """State of one actor run."""

    id: str
    status: str
    dataset_id: str | None
    cost_usd: float
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def finished(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def succeeded(self) -> bool:
        return self.status == "SUCCEEDED"


@dataclass(frozen=True)
class YouTubeItem:
    """One normalised YouTube search result. For a live stream `viewers` is the current audience and
    `views` is None; for a finished video it is the other way round."""

    video_id: str
    channel_id: str
    channel_name: str
    title: str
    is_live: bool
    viewers: int | None
    views: int | None
    published_at: datetime | None
    duration_s: int | None
    likes: int | None
    comments: int | None
    subscribers: int | None = None
    url: str = ""
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return int(value)
    return None


def parse_clock(value: Any) -> int | None:
    """Convert the actor's `HH:MM:SS` (or `MM:SS`) duration to seconds; None when missing or unreadable."""
    if not isinstance(value, str) or not value:
        return None
    parts = value.split(":")
    if not 1 <= len(parts) <= 3 or not all(part.isdigit() for part in parts):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


def date_filter_for(hours: int | None) -> str | None:
    """Map a look-back in hours to the actor's `dateFilter` (the smallest window that covers it)."""
    if hours is None:
        return None
    if hours <= 1:
        return "hour"
    if hours <= 24:
        return "today"
    if hours <= 24 * 7:
        return "week"
    if hours <= 24 * 31:
        return "month"
    return "year"


def build_youtube_search_input(
    terms: list[str],
    live: bool,
    uploaded_within_hours: int | None,
    max_results: int,
    sort: str | None = None,
) -> dict[str, Any]:
    """Input for `streamers/youtube-scraper`.

    Verified against the actor's schema and real runs: live searches need `isLive` together with
    `maxResultStreams` (with `maxResults` alone a live search returns nothing); finished videos use
    `maxResults`. Each limit is per search term. `sort` is `relevance`, `rating`, `date` or `views`.
    """
    payload: dict[str, Any] = {
        "searchQueries": list(terms),
        "maxResults": 0 if live else max_results,
        "maxResultsShorts": 0,
        "maxResultStreams": max_results if live else 0,
    }
    if live:
        payload["isLive"] = True
    date_filter = None if live else date_filter_for(uploaded_within_hours)
    if date_filter:
        payload["dateFilter"] = date_filter
    if sort:
        payload["sortingOrder"] = sort
    return payload


def normalise_youtube_item(item: dict[str, Any]) -> YouTubeItem | None:
    """Turn one dataset row into a `YouTubeItem`. Rows without a video id (the actor writes
    `{"error": "NO_VIDEOS", ...}` rows when a search finds nothing) give None."""
    video_id = item.get("id")
    if not isinstance(video_id, str) or not video_id or item.get("error"):
        return None
    is_live = item.get("streamType") == "live" or item.get("isLive") is True
    view_count = _int(item.get("viewCount"))
    return YouTubeItem(
        video_id=video_id,
        channel_id=str(item.get("channelId") or ""),
        channel_name=str(item.get("channelName") or ""),
        title=str(item.get("title") or ""),
        is_live=is_live,
        viewers=view_count if is_live else None,
        views=None if is_live else view_count,
        published_at=_parse_time(item.get("date")),
        duration_s=parse_clock(item.get("duration")),
        likes=_int(item.get("likes")),
        comments=_int(item.get("commentsCount")),
        subscribers=_int(item.get("numberOfSubscribers")),
        url=str(item.get("url") or f"https://www.youtube.com/watch?v={video_id}"),
        raw=item,
    )


def _run_cost(data: dict[str, Any]) -> float:
    """Cost of a run in USD: charged events times their price for pay-per-event actors, otherwise the
    platform usage. The larger of the two is used because `usageTotalUsd` can lag behind."""
    total = float(data.get("usageTotalUsd") or 0.0)
    counts = data.get("chargedEventCounts") or {}
    prices = ((data.get("pricingInfo") or {}).get("pricingPerEvent") or {}).get("actorChargeEvents") or {}
    events = sum(
        float(count or 0) * float((prices.get(name) or {}).get("eventPriceUsd") or 0.0)
        for name, count in counts.items()
    )
    return max(total, events)


def _run(data: dict[str, Any]) -> ApifyRun:
    return ApifyRun(
        id=str(data["id"]),
        status=str(data.get("status", "")),
        dataset_id=data.get("defaultDatasetId"),
        cost_usd=_run_cost(data),
        started_at=_parse_time(data.get("startedAt")),
        finished_at=_parse_time(data.get("finishedAt")),
    )


class ApifyClient:
    """Apify API v2 client."""

    def __init__(
        self, token: str | None = None, youtube_actor: str | None = None, http: httpx.Client | None = None
    ) -> None:
        settings = env()
        configured = settings.APIFY_TOKEN
        self._token = token or (configured.get_secret_value() if configured is not None else None)
        self._youtube_actor = youtube_actor or settings.APIFY_YOUTUBE_ACTOR
        self._http = http or _http.new_client()

    def is_configured(self) -> bool:
        """True when the Apify token is present."""
        return bool(self._token)

    def _headers(self) -> dict[str, str]:
        if not self._token:
            raise NotConfiguredError("APIFY_TOKEN is not set")
        return {"Authorization": f"Bearer {self._token}"}

    def start_run(
        self,
        actor_id: str,
        input: dict[str, Any],  # noqa: A002
        max_charge_usd: float = DEFAULT_MAX_CHARGE_USD,
        timeout_s: int = DEFAULT_RUN_TIMEOUT_S,
    ) -> str:
        """Start an actor run and return its run id. `actor_id` may be `user/actor` or `user~actor`.

        Starting a run is not retried on 5xx or network errors, so one request can never start two
        paid runs; a 429 is retried because nothing was started.
        """
        endpoint = "POST /v2/acts/{actor}/runs"
        url = f"{API_BASE}/acts/{actor_id.replace('/', '~')}/runs"
        params = {"maxTotalChargeUsd": max_charge_usd, "timeout": timeout_s}
        call = partial(self._http.post, url, params=params, json=input, headers=self._headers())
        response = _http.send(SERVICE, endpoint, call, retry_statuses=(429,), max_attempts=_http.MAX_ATTEMPTS)
        _http.raise_for_status(SERVICE, endpoint, response)
        return str(response.json()["data"]["id"])

    def get_run(self, run_id: str) -> ApifyRun:
        """Current state of a run."""
        endpoint = "GET /v2/actor-runs/{run}"
        call = partial(self._http.get, f"{API_BASE}/actor-runs/{run_id}", headers=self._headers())
        response = _http.send(SERVICE, endpoint, call)
        _http.raise_for_status(SERVICE, endpoint, response)
        return _run(response.json()["data"])

    def wait_for_run(
        self, run_id: str, timeout_s: float = 360.0, poll_s: float = POLL_INTERVAL_S, operation: str = "run"
    ) -> ApifyRun:
        """Poll until the run reaches a final status, record its cost and return it.

        Raises `ApifyRunFailed` when the run fails, is aborted, times out on Apify or is still running
        after `timeout_s`.
        """
        waited = 0.0
        run = self.get_run(run_id)
        while not run.finished and waited < timeout_s:
            _http.sleep(poll_s)
            waited += poll_s
            run = self.get_run(run_id)
        usage.record(SERVICE, operation, units=1, cost_usd=run.cost_usd, ref_id=run_id)
        if not run.succeeded:
            raise ApifyRunFailed(run_id, run.status if run.finished else "STILL-RUNNING")
        return run

    def get_items(self, dataset_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        """Read up to `limit` clean items of a dataset."""
        endpoint = "GET /v2/datasets/{dataset}/items"
        params: dict[str, str | int] = {"clean": "true", "format": "json", "limit": limit}
        call = partial(
            self._http.get, f"{API_BASE}/datasets/{dataset_id}/items", params=params, headers=self._headers()
        )
        response = _http.send(SERVICE, endpoint, call)
        _http.raise_for_status(SERVICE, endpoint, response)
        items = response.json()
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def youtube_search(
        self,
        terms: list[str],
        live: bool = False,
        uploaded_within_hours: int | None = None,
        max_results: int = 50,
        sort: str | None = None,
        max_charge_usd: float = DEFAULT_MAX_CHARGE_USD,
    ) -> list[YouTubeItem]:
        """Search YouTube through `APIFY_YOUTUBE_ACTOR` and return normalised, de-duplicated items.

        `max_results` is per search term. `uploaded_within_hours` is ignored for live searches. An empty
        result is a normal outcome (YouTube search is flaky), not an error.
        """
        payload = build_youtube_search_input(terms, live, uploaded_within_hours, max_results, sort)
        run_id = self.start_run(self._youtube_actor, payload, max_charge_usd=max_charge_usd)
        operation = "youtube_search_live" if live else "youtube_search"
        run = self.wait_for_run(run_id, operation=operation)
        if run.dataset_id is None:
            return []
        rows = self.get_items(run.dataset_id, limit=max(1, max_results) * max(1, len(terms)) + len(terms))
        found: dict[str, YouTubeItem] = {}
        for row in rows:
            item = normalise_youtube_item(row)
            if item is not None and item.video_id not in found:
                found[item.video_id] = item
        return list(found.values())
