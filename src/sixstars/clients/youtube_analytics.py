"""YouTube Analytics API v2: per-video completion, watch time and subscribers gained (docs/28 Part A).

Docs (checked 2026-11-19): https://developers.google.com/youtube/analytics/reference/reports/query

- `GET https://youtubeanalytics.googleapis.com/v2/reports` with `ids=channel==MINE`, `startDate`,
  `endDate` (YYYY-MM-DD), `metrics`, `dimensions=video`, `filters=video==id1,id2,...` (up to 500 ids).
- Answer: `columnHeaders[{name, columnType, dataType}]` (dimensions first, then metrics in request order)
  and `rows[[...]]`; `rows` is left out when there is no data.
- Accepted scopes include `https://www.googleapis.com/auth/youtube`, which `sixstars youtube auth` already
  requests, so the existing OAuth client works once a refresh token exists (none yet: 19 Nov 2026).
- Analytics data lags: a video can be missing for a day or more. Missing means None, never 0.

The OAuth token comes from `YouTubeClient` (same client id, secret and refresh token).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from sixstars.clients import _http
from sixstars.clients.youtube import YouTubeClient

SERVICE = "youtube_analytics"
REPORTS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
METRICS = (
    "views",
    "averageViewPercentage",
    "averageViewDuration",
    "subscribersGained",
    "estimatedMinutesWatched",
)
IDS_PER_REQUEST_MAX = 200


@dataclass(frozen=True, slots=True)
class VideoAnalytics:
    video_id: str
    views: int | None
    avg_view_pct: float | None  # 0..1
    avg_view_duration_s: float | None
    subscribers_gained: int | None
    minutes_watched: float | None


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def parse_report(body: Any) -> dict[str, VideoAnalytics]:
    """Rows of a `dimensions=video` report keyed by video id."""
    if not isinstance(body, Mapping):
        return {}
    headers = body.get("columnHeaders")
    rows = body.get("rows")
    if not isinstance(headers, list) or not isinstance(rows, list):
        return {}
    names = [h.get("name") if isinstance(h, Mapping) else None for h in headers]
    out: dict[str, VideoAnalytics] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != len(names):
            continue
        cells = dict(zip(names, row, strict=True))
        video = cells.get("video")
        if not isinstance(video, str):
            continue
        views = _num(cells.get("views"))
        pct = _num(cells.get("averageViewPercentage"))
        subs = _num(cells.get("subscribersGained"))
        out[video] = VideoAnalytics(
            video_id=video,
            views=int(views) if views is not None else None,
            avg_view_pct=pct / 100 if pct is not None else None,
            avg_view_duration_s=_num(cells.get("averageViewDuration")),
            subscribers_gained=int(subs) if subs is not None else None,
            minutes_watched=_num(cells.get("estimatedMinutesWatched")),
        )
    return out


class YouTubeAnalyticsClient:
    def __init__(self, youtube: YouTubeClient | None = None) -> None:
        self._youtube = youtube or YouTubeClient()

    def is_configured(self) -> bool:
        return self._youtube.has_oauth()

    def video_report(self, video_ids: Sequence[str], start: date, end: date) -> dict[str, VideoAnalytics]:
        """Lifetime-to-date numbers per video between `start` and `end` (inclusive)."""
        out: dict[str, VideoAnalytics] = {}
        ids = [v for v in dict.fromkeys(video_ids) if v]
        for i in range(0, len(ids), IDS_PER_REQUEST_MAX):
            chunk = ids[i : i + IDS_PER_REQUEST_MAX]
            params = {
                "ids": "channel==MINE",
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "metrics": ",".join(METRICS),
                "dimensions": "video",
                "filters": "video==" + ",".join(chunk),
                "maxResults": str(len(chunk)),
            }
            endpoint = "GET /v2/reports"
            response = self._youtube._oauth_request("GET", REPORTS_URL, endpoint, params=params)
            _http.raise_for_status(SERVICE, endpoint, response)
            out |= parse_report(response.json())
        return out
