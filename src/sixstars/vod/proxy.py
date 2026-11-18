"""The residential proxy of YouTube video downloads: sticky sessions, retries, traffic and budget.

YouTube refuses the server's data-centre address ("Sign in to confirm you're not a bot"). Video jobs go
through a residential proxy instead (DataImpulse, paid per GB). The provider gives every port of a range
its own exit IP, held for about 30 minutes (`PROXY_STICKY_PORT_MIN` / `PROXY_STICKY_PORT_MAX`), so:

- A video job picks ONE random port and uses it for every request of the video (chat replay, captions,
  PO token, windows): YouTube sees one consistent viewer, and the token matches the download address.
- A refused attempt (bot check, 403 on every window, a proxy that failed) is tried again on a NEW port,
  up to `proxy.max_attempts` times. Then, only when `proxy.cookies_mode` is `fallback` and a cookies file
  exists, once more with cookies. After that the video waits as before (`vod.blocked_backoff_s`).
- A refusal by the proxy account itself (401 / 402 / 407: credentials, traffic used up) is not retried:
  another port would answer the same.
- The bytes a job moved through the proxy are written to `api_usage` (provider `DataImpulse`, operation
  `youtube_download`, units = GB, cost = GB x `proxy.usd_per_gb`). Media bytes are measured from the
  files; each yt-dlp call adds `proxy.overhead_mb_per_call` for pages, manifests and the player.
- At `proxy.daily_budget_usd` spent today, YouTube video jobs wait for the next UTC day.

Only the VOD lane uses the proxy (`proxy.lanes`, see `chat/net.py`). Pure helpers first; the functions
at the end read the database.
"""

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import urlsplit, urlunsplit

from sixstars import db, events, settings, usage
from sixstars.chat.net import proxy_refusal
from sixstars.config import env
from sixstars.factory.pipeline import next_budget_reset
from sixstars.logging import get_logger
from sixstars.vod.ytdlp import Access, TrafficMeter, access_for

USAGE_PROVIDER: Final = "DataImpulse"
USAGE_OPERATION: Final = "youtube_download"
SERVICE: Final = "vod"
BYTES_PER_GB: Final = 1_000_000_000  # providers bill decimal gigabytes
BYTES_PER_MB: Final = 1_000_000
# A refusal by the proxy account: another port gets the same answer.
ACCOUNT_STATUSES: Final = frozenset({401, 402, 407})
PORT_DRAWS: Final = 50  # tries to find a port not used yet by this job

CookiesMode = Literal["fallback", "first", "never"]
COOKIES_MODES: Final[tuple[CookiesMode, ...]] = ("fallback", "first", "never")

# Mirrors supabase/migrations/0025_proxy.sql, so a database without the migration behaves the same.
DEFAULTS: Final[dict[str, Any]] = {
    "proxy.max_attempts": 3,
    "proxy.cookies_mode": "fallback",
    "proxy.daily_budget_usd": 3.0,
    "proxy.usd_per_gb": 1.0,
    "proxy.overhead_mb_per_call": 2.0,
    "proxy.cycle_gb": 5.0,
    "proxy.cycle_started_at": "2026-11-18T00:00:00+00:00",
    "proxy.low_traffic_share": 0.8,
    "proxy.health_interval_s": 900,
}


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    """The `proxy.*` settings."""

    max_attempts: int
    cookies_mode: CookiesMode
    daily_budget_usd: float
    usd_per_gb: float
    overhead_mb_per_call: float
    cycle_gb: float
    cycle_started_at: datetime
    low_traffic_share: float
    health_interval_s: float

    @classmethod
    def load(cls) -> "ProxyConfig":
        def get(key: str) -> Any:
            return settings.get(key, DEFAULTS[key])

        mode = str(get("proxy.cookies_mode"))
        started = settings.get_dt("proxy.cycle_started_at", None) or datetime.fromisoformat(
            str(DEFAULTS["proxy.cycle_started_at"])
        )
        return cls(
            max_attempts=max(1, int(get("proxy.max_attempts"))),
            cookies_mode=mode if mode in COOKIES_MODES else "fallback",
            daily_budget_usd=float(get("proxy.daily_budget_usd")),
            usd_per_gb=float(get("proxy.usd_per_gb")),
            overhead_mb_per_call=float(get("proxy.overhead_mb_per_call")),
            cycle_gb=float(get("proxy.cycle_gb")),
            cycle_started_at=started,
            low_traffic_share=float(get("proxy.low_traffic_share")),
            health_interval_s=float(get("proxy.health_interval_s")),
        )


# ── pure ─────────────────────────────────────────────────────


def with_port(url: str, port: int) -> str:
    """`url` with its port replaced (credentials, scheme and host kept). Pure."""
    parts = urlsplit(url)
    if parts.hostname is None:
        raise ValueError("the proxy URL has no host")
    userinfo, at, _ = parts.netloc.rpartition("@")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    netloc = f"{userinfo}{at}{host}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def port_range(low: int | None, high: int | None) -> tuple[int, int] | None:
    """The sticky port range, or None when it is not configured or impossible. Pure."""
    if low is None or high is None or not 0 < low <= high <= 65535:
        return None
    return low, high


def pick_port(low: int, high: int, rand: Callable[[], float], used: Collection[int] = ()) -> int:
    """A random port of `low..high`, one not in `used` when the range has one left. Pure given `rand`."""
    size = high - low + 1
    port = low
    for _ in range(PORT_DRAWS):
        port = low + min(size - 1, int(rand() * size))
        if port not in used:
            return port
    free = [candidate for candidate in range(low, high + 1) if candidate not in used]
    return free[0] if free else port


def cookies_plan(attempts: int, mode: CookiesMode, have_cookies: bool) -> list[bool]:
    """Whether each attempt of a job sends cookies. Pure.

    `fallback`: `attempts` without cookies, then one with them (when there is a file). `first`: all with
    cookies. `never`: none. Without a cookies file every attempt goes without."""
    attempts = max(1, attempts)
    if not have_cookies or mode == "never":
        return [False] * attempts
    if mode == "first":
        return [True] * attempts
    return [False] * attempts + [True]


def gigabytes(size_bytes: int) -> float:
    return max(0, size_bytes) / BYTES_PER_GB


def traffic_bytes(measured_bytes: int, calls: int, overhead_mb_per_call: float) -> int:
    """Measured media bytes plus the estimated overhead of the yt-dlp calls. Pure."""
    return max(0, measured_bytes) + max(0, calls) * int(overhead_mb_per_call * BYTES_PER_MB)


def cost_usd(size_bytes: int, usd_per_gb: float) -> float:
    return round(gigabytes(size_bytes) * usd_per_gb, 6)


def is_account_failure(detail: str) -> bool:
    """The proxy account refused (credentials, no traffic left): no other port can help. Pure."""
    refusal = proxy_refusal(detail)
    return refusal is not None and refusal.status in ACCOUNT_STATUSES


def provider_name(url: str | None) -> str:
    """`DataImpulse` for its gateway, else `The proxy` (for messages). Pure."""
    host = (urlsplit(url).hostname or "") if url else ""
    return USAGE_PROVIDER if "dataimpulse" in host.lower() else "The proxy"


def budget_reached(spent_usd: float, budget_usd: float) -> bool:
    """A budget of 0 or less means no limit. Pure."""
    return budget_usd > 0 and spent_usd >= budget_usd


def low_traffic(used_gb: float, cycle_gb: float, share: float) -> bool:
    """The tracked use of the billing cycle passed `share` of the plan. Pure."""
    return cycle_gb > 0 and used_gb >= cycle_gb * share


# ── one video job ────────────────────────────────────────────


def sticky_range() -> tuple[int, int] | None:
    settings_env = env()
    return port_range(settings_env.PROXY_STICKY_PORT_MIN, settings_env.PROXY_STICKY_PORT_MAX)


def job_accesses(
    platform: str, rand: Callable[[], float], cfg: ProxyConfig, meter: TrafficMeter
) -> list[Access]:
    """The access of every attempt of one video job, in order.

    Without the proxy (or for Twitch) one attempt with the access of today. With it, each attempt has its
    own random sticky port, cookies as `cookies_plan` says, the PO-token provider and the job's meter."""
    base = access_for(platform)
    if base.proxy is None or platform != "youtube":
        return [base]
    ports = sticky_range()
    pot_url = env().POT_PROVIDER_URL or None
    plan = cookies_plan(cfg.max_attempts, cfg.cookies_mode, base.cookies_file is not None)
    used: list[int] = []
    out: list[Access] = []
    for with_cookies in plan:
        url, port = base.proxy, None
        if ports is not None:
            port = pick_port(ports[0], ports[1], rand, used)
            used.append(port)
            url = with_port(base.proxy, port)
        out.append(
            replace(
                base,
                proxy=url,
                cookies_file=base.cookies_file if with_cookies else None,
                cookies_held=base.cookies_file is not None and not with_cookies,
                sticky_port=port,
                pot_url=pot_url,
                meter=meter,
            )
        )
    return out


def size_of(path: Path) -> int:
    """Bytes of a file, or of every file under a folder; 0 when there is nothing."""
    try:
        if path.is_file():
            return path.stat().st_size
        if path.is_dir():
            return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    except OSError:
        return 0
    return 0


def note_attempt(video_id: str, number: int, access: Access, outcome: str, size_bytes: int) -> None:
    """One `vod.proxy_attempt` event per attempt through the proxy (for the success rate per attempt)."""
    if access.proxy is None:
        return
    events.emit(
        "vod.proxy_attempt",
        level="info" if outcome == "ok" else "warn",
        entity_type="video",
        entity_id=video_id,
        message=outcome[:300],
        data={
            "attempt": number,
            "port": access.sticky_port,
            "cookies": access.cookies_file is not None,
            "ok": outcome == "ok",
            "bytes": size_bytes,
        },
        service=SERVICE,
    )


def record_usage(video_id: str, meter: TrafficMeter, cfg: ProxyConfig) -> int:
    """Write the job's proxy traffic to `api_usage`. Returns the bytes recorded (0: nothing to record)."""
    total = traffic_bytes(meter.bytes, meter.calls, cfg.overhead_mb_per_call)
    if total <= 0:
        return 0
    usage.record(
        USAGE_PROVIDER,
        USAGE_OPERATION,
        units=round(gigabytes(total), 6),
        cost_usd=cost_usd(total, cfg.usd_per_gb),
        ref_id=video_id,
    )
    get_logger(SERVICE).info(
        "proxy_traffic", bytes=total, measured_bytes=meter.bytes, calls=meter.calls, provider=USAGE_PROVIDER
    )
    return total


# ── budget and traffic (database) ────────────────────────────


def spent_today_usd() -> float:
    return usage.spent_today(USAGE_PROVIDER)


def budget_pause(now: datetime, cfg: ProxyConfig | None = None) -> datetime | None:
    """When YouTube video jobs may run again if today's proxy budget is used up, else None."""
    cfg = cfg or ProxyConfig.load()
    if not budget_reached(spent_today_usd(), cfg.daily_budget_usd):
        return None
    return next_budget_reset(now)


def used_gb(since: datetime) -> float:
    """GB recorded for the proxy since `since`."""
    row = db.fetch_one(
        "select coalesce(sum(units), 0) as gb from api_usage where provider = %s and created_at >= %s",
        (USAGE_PROVIDER, since),
    )
    return float(row["gb"]) if row is not None else 0.0


def used_gb_today() -> float:
    now = datetime.now(UTC)
    return used_gb(now.replace(hour=0, minute=0, second=0, microsecond=0))


def health_state() -> Mapping[str, Any] | None:
    """The last proxy probe of the guardian (`proxy.health`), or None."""
    value = settings.get("proxy.health", None)
    return value if isinstance(value, dict) else None


def status_line() -> str:
    """`proxy reachable (checked 13:05 UTC), 0.42 GB used today` for the blocked-downloads message."""
    try:
        state = health_state()
        today = used_gb_today()
    except Exception:
        return "proxy status unknown"
    if state is None:
        probe = "proxy not checked yet"
    else:
        checked = str(state.get("checked_at", ""))[11:16]
        verdict = "reachable" if state.get("ok") else f"NOT reachable ({state.get('reason', '?')})"
        probe = f"proxy {verdict} (checked {checked} UTC)"
    return f"{probe}, {today:.2f} GB used today"
