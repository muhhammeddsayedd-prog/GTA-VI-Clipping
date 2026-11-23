"""The daily report: `GET /v1/reports/daily?format=telegram|json` (WF06 hourly with `due=1`, and `/report`).

`build()` collects the numbers of one day (by default the day that just ended, in `app.timezone`)
as a JSON object; `render_telegram()` turns it into text for Telegram's HTML parse mode. Every value
that comes from the database or from a platform (clip titles, handles, stop reasons, reject reasons) is
escaped, and the text never exceeds `TELEGRAM_MAX_CHARS`: the least important lines are dropped first
and a line is never cut in the middle of a tag or an entity.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from html import escape
from statistics import median
from typing import Any

from psycopg.types.json import Jsonb

from sixstars import clock, db, settings
from sixstars.config import env
from sixstars.guardian import stop
from sixstars.guardian.health import BUDGETS

TELEGRAM_LIMIT = 4096  # Telegram's limit for one message
TELEGRAM_MAX_CHARS = 3900  # ours: room for the "[STG] " prefix and for entities counted differently
MAX_TITLE_CHARS = 60
MAX_REASON_CHARS = 24
MAX_ISSUES = 6
MAX_MANUAL_LINKS = 5
YPP_WINDOW_D = 90  # the YouTube Partner Program counts Shorts views over 90 days (a platform rule)
PLATFORM_SHORT = {"tiktok": "TT", "instagram": "IG", "youtube": "YT"}
ISSUE_TYPES = (
    "account.rate_limited",
    "account.auth_failed",
    "control.stop",
    "control.start",
    "guardian.stop",
    "health.incident",
    "vod.paused",
    "job.stuck_reset",
)


# ── day bounds ───────────────────────────────────────────────


def report_zone() -> tzinfo:
    """The report day follows the app's display zone (`app.timezone`)."""
    return clock.app_zone()


def day_bounds(day: date, zone: tzinfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=zone)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def default_day(now: datetime, zone: tzinfo) -> date:
    """The day that ended most recently."""
    return (now.astimezone(zone) - timedelta(days=1)).date()


SENT_EVENT = "report.daily_sent"


def claim_due_day(now: datetime | None = None) -> date | None:
    """The local day whose report is due now, or None when it was already sent.

    WF06 calls every hour; the first call after midnight in `app.timezone` claims the day that just
    ended by writing a `report.daily_sent` event, so the report follows the zone (and summer time) and
    goes out once per day.
    """
    now = now or datetime.now(UTC)
    zone = report_zone()
    day = default_day(now, zone)
    # The lock serialises overlapping calls (a manual WF06 run during the hourly one); the not-exists
    # check alone lets two calls insert before either commits, and the report would go out twice.
    with db.transaction():
        db.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"{SENT_EVENT}:{day}",))
        row = db.fetch_one(
            """
            insert into events (type, level, service, entity_type, entity_id, message, data)
            select %s, 'info', %s, 'report', %s, %s, %s
            where not exists (
                select 1 from events where type = %s and entity_type = 'report' and entity_id = %s
            )
            returning id
            """,
            (
                SENT_EVENT,
                env().SERVICE_NAME,
                day.isoformat(),
                f"daily report for {day.isoformat()} ({clock.zone_label(zone)})",
                Jsonb({"day": day.isoformat(), "timezone": clock.zone_name(zone)}),
                SENT_EVENT,
                day.isoformat(),
            ),
        )
    return day if row is not None else None


# ── queries ──────────────────────────────────────────────────


def _count(sql: str, params: Sequence[Any]) -> int:
    row = db.fetch_one(sql, params)
    return int(row["n"]) if row and row["n"] is not None else 0


def _accounts(start: datetime, end: datetime) -> list[dict[str, Any]]:
    rows = db.fetch_all(
        """
        select a.id as account_id, a.platform, a.handle, a.enabled, a.paused_until, a.cap_24h,
               latest.followers, latest.views_total,
               before.followers as followers_before, before.views_total as views_before,
               coalesce(p.published, 0) as published, coalesce(p.failed, 0) as failed,
               coalesce(p.dry_run, 0) as dry_run
        from platform_accounts a
        left join lateral (
          select s.followers, s.views_total from account_stats s
          where s.account_id = a.id and s.captured_at < %(end)s
          order by s.captured_at desc limit 1
        ) latest on true
        left join lateral (
          select s.followers, s.views_total from account_stats s
          where s.account_id = a.id and s.captured_at < %(start)s
          order by s.captured_at desc limit 1
        ) before on true
        left join lateral (
          select
            count(*) filter (
              where not dry_run and status in ('published', 'removed', 'remove_failed')
                and published_at >= %(start)s and published_at < %(end)s) as published,
            count(*) filter (
              where not dry_run and status = 'failed'
                and created_at >= %(start)s and created_at < %(end)s) as failed,
            count(*) filter (
              where dry_run and created_at >= %(start)s and created_at < %(end)s) as dry_run
          from posts where account_id = a.id
        ) p on true
        order by a.id
        """,
        {"start": start, "end": end},
    )
    out = []
    for row in rows:
        followers, before = row["followers"], row["followers_before"]
        views, views_before = row["views_total"], row["views_before"]
        out.append(
            {
                "account_id": row["account_id"],
                "platform": row["platform"],
                "handle": row["handle"],
                "enabled": bool(row["enabled"]),
                "paused_until": row["paused_until"].astimezone(UTC).isoformat()
                if row["paused_until"]
                else None,
                "cap_24h": int(row["cap_24h"]),
                "followers": None if followers is None else int(followers),
                "followers_change": None if followers is None or before is None else int(followers - before),
                "views_total": None if views is None else int(views),
                "views_change": None if views is None or views_before is None else int(views - views_before),
                "published": int(row["published"]),
                "failed": int(row["failed"]),
                "dry_run": int(row["dry_run"]),
            }
        )
    return out


def _views(accounts: Sequence[Mapping[str, Any]], end: datetime) -> dict[str, Any]:
    enabled = [item for item in accounts if item["enabled"]]
    totals = [item["views_total"] for item in enabled if item["views_total"] is not None]
    changes = [item["views_change"] for item in enabled if item["views_change"] is not None]
    rows = db.fetch_all(
        """
        select platform, views from posts
        where not dry_run and status = 'published' and views is not null
          and published_at >= %s and published_at < %s
        """,
        (end - timedelta(days=settings.get_float("stats.post_lookback_d")), end),
    )
    by_platform: dict[str, list[int]] = {}
    for row in rows:
        by_platform.setdefault(str(row["platform"]), []).append(int(row["views"]))
    return {
        "total": sum(totals) if totals else None,
        "change": sum(changes) if changes else None,
        "median_by_platform": {name: int(median(values)) for name, values in sorted(by_platform.items())},
    }


def _top_clips(end: datetime, limit: int) -> list[dict[str, Any]]:
    rows = db.fetch_all(
        """
        select c.title, p.views, p.platform, p.post_url
        from posts p join clips c on c.id = p.clip_id
        where not p.dry_run and p.status = 'published' and p.views is not null
          and p.published_at >= %s and p.published_at < %s
        order by p.views desc, p.published_at desc limit %s
        """,
        (end - timedelta(days=settings.get_float("stats.post_lookback_d")), end, max(1, limit)),
    )
    return [
        {
            "title": row["title"] or "",
            "views": int(row["views"]),
            "platform": row["platform"],
            "post_url": row["post_url"],
        }
        for row in rows
    ]


def _ypp(accounts: Sequence[Mapping[str, Any]], end: datetime) -> dict[str, Any]:
    targets = settings.get_dict("reports.ypp_targets")
    subscribers = [
        item["followers"]
        for item in accounts
        if item["platform"] == "youtube" and item["enabled"] and item["followers"] is not None
    ]
    row = db.fetch_one(
        """
        select coalesce(sum(views), 0) as n from posts
        where platform = 'youtube' and not dry_run and views is not null
          and published_at >= %s and published_at < %s
        """,
        (end - timedelta(days=YPP_WINDOW_D), end),
    )
    return {
        "subscribers": max(subscribers) if subscribers else None,
        "subscribers_target": targets.get("subscribers"),
        "shorts_views_90d": int(row["n"]) if row else 0,
        "shorts_views_target": targets.get("shorts_views_90d"),
    }


def _posts(start: datetime, end: datetime) -> dict[str, Any]:
    row = db.fetch_one(
        """
        select
          count(*) filter (where not dry_run and lane = 'hourly'
                           and published_at >= %(start)s and published_at < %(end)s
                           and status in ('published','removed','remove_failed')) as hourly,
          count(*) filter (where not dry_run and lane = 'spike'
                           and published_at >= %(start)s and published_at < %(end)s
                           and status in ('published','removed','remove_failed')) as spike,
          count(*) filter (where not dry_run and status = 'failed'
                           and created_at >= %(start)s and created_at < %(end)s) as failed,
          count(*) filter (where dry_run and created_at >= %(start)s and created_at < %(end)s) as dry_run,
          count(distinct slot_at) filter (where lane = 'hourly' and slot_at >= %(start)s and slot_at < %(end)s
                           and (dry_run or status in ('published','removed','remove_failed'))) as slots_used
        from posts
        """,
        {"start": start, "end": end},
    )
    assert row is not None
    skipped = _count(
        "select count(*) as n from events where type in ('posting.slot_skipped', 'posting.slot_empty') "
        "and created_at >= %s and created_at < %s",
        (start, end),
    )
    return {
        "hourly_published": int(row["hourly"]),
        "spike_published": int(row["spike"]),
        "failed": int(row["failed"]),
        "dry_run": int(row["dry_run"]),
        "slots_used": int(row["slots_used"]),
        "slots_skipped_events": skipped,
    }


def _spikes(start: datetime, end: datetime) -> dict[str, Any]:
    rows = db.fetch_all(
        "select tier, count(*) as n from spikes where detected_at >= %s and detected_at < %s group by tier",
        (start, end),
    )
    tiers = {str(row["tier"] or "unrated"): int(row["n"]) for row in rows}
    latency = db.fetch_all(
        "select spike_to_post_s from spikes "
        "where spike_to_post_s is not null and detected_at >= %s and detected_at < %s",
        (start, end),
    )
    values = [float(row["spike_to_post_s"]) for row in latency]
    return {
        "detected": sum(tiers.values()),
        "S": tiers.get("S", 0),
        "A": tiers.get("A", 0),
        "B": tiers.get("B", 0),
        "rejected": tiers.get("rejected", 0),
        "median_spike_to_post_s": int(median(values)) if values else None,
    }


def reason_label(reason: str | None) -> str:
    """A short label for a reject reason: the part before the first colon, at most a few words."""
    text = (reason or "unknown").split(":", 1)[0].strip().lower() or "unknown"
    return text[:MAX_REASON_CHARS]


def _clips(start: datetime, end: datetime) -> dict[str, Any]:
    window = (start, end)
    rejected = db.fetch_all(
        "select reject_reason from clips where status = 'rejected' and updated_at >= %s and updated_at < %s",
        window,
    )
    by_reason: dict[str, int] = {}
    for row in rejected:
        label = reason_label(row["reject_reason"])
        by_reason[label] = by_reason.get(label, 0) + 1
    queue = db.fetch_all("select lane, count(*) as n from clips where status = 'approved' group by lane")
    depth = {str(row["lane"]): int(row["n"]) for row in queue}
    return {
        "rendered": _count(
            "select count(*) as n from clips "
            "where created_at >= %s and created_at < %s and status <> 'rendering'",
            window,
        ),
        "approved": _count(
            "select count(*) as n from clips where approved_at >= %s and approved_at < %s", window
        ),
        "rejected": len(rejected),
        "rejected_by_reason": dict(sorted(by_reason.items(), key=lambda item: (-item[1], item[0]))),
        "expired": _count(
            "select count(*) as n from clips "
            "where status = 'expired' and updated_at >= %s and updated_at < %s",
            window,
        ),
        "queue": {
            "spike": depth.get("spike", 0),
            "hourly": depth.get("hourly", 0),
            "low_watermark": settings.get("posting.queue_low_watermark", None),
        },
    }


def _discovery(start: datetime, end: datetime) -> dict[str, Any]:
    window = (start, end)
    tiers = db.fetch_all(
        "select tier, count(*) as n from creators "
        "where discovered_at >= %s and discovered_at < %s group by tier",
        window,
    )
    by_tier = {str(row["tier"]): int(row["n"]) for row in tiers}
    return {
        "videos_processed": _count(
            "select count(*) as n from videos where status = 'done' and updated_at >= %s and updated_at < %s",
            window,
        ),
        "candidates_checked": _count(
            "select count(*) as n from candidates where checked_at >= %s and checked_at < %s", window
        ),
        "new_creators": sum(by_tier.values()),
        "new_by_tier": by_tier,
    }


def _guardian(start: datetime, end: datetime, now: datetime) -> dict[str, Any]:
    rows = db.fetch_all(
        "select source, status, count(*) as n from takedowns "
        "where received_at >= %s and received_at < %s group by 1, 2",
        (start, end),
    )
    claims = sum(int(row["n"]) for row in rows if row["source"] in ("claim", "platform"))
    emails = [row for row in rows if row["source"] == "email"]
    window_d = settings.get_float("guardian.strike_window_d")
    since = stop.window_start(now, window_d, stop.last_stop_at())
    manual = db.fetch_all(
        "select platform, post_url from posts "
        "where status = 'remove_failed' and not dry_run order by updated_at"
    )
    reason = db.fetch_one("select value from settings where key = %s", (stop.REASON_KEY,))
    return {
        "claims": claims,
        "takedowns": sum(int(row["n"]) for row in emails if row["status"] in ("actioned", "partial")),
        "emails_ignored": sum(int(row["n"]) for row in emails if row["status"] == "ignored"),
        "emails_open": _count(
            "select count(*) as n from takedowns where source = 'email' and status = 'open'", ()
        ),
        "strikes": len(stop.strike_times(since)),
        "strike_limit": settings.get_int("guardian.strike_limit"),
        "emergency_stop": stop.is_stopped(),
        "stop_reason": reason["value"] if reason and isinstance(reason["value"], str) else None,
        "manual_removals": [{"platform": row["platform"], "post_url": row["post_url"]} for row in manual],
    }


def _spend(start: datetime, end: datetime) -> list[dict[str, Any]]:
    rows = db.fetch_all(
        "select provider, coalesce(sum(cost_usd), 0) as total from api_usage "
        "where created_at >= %s and created_at < %s group by provider",
        (start, end),
    )
    spent = {str(row["provider"]): float(row["total"]) for row in rows}
    out: list[dict[str, Any]] = []
    covered: set[str] = set()
    for label, providers, key, _ in BUDGETS:
        covered.update(providers)
        budget = settings.get(key, None)
        out.append(
            {
                "provider": label,
                "spent_usd": round(sum(spent.get(name, 0.0) for name in providers), 4),
                "budget_usd": float(budget)
                if isinstance(budget, int | float) and not isinstance(budget, bool)
                else None,
            }
        )
    for name, total in sorted(spent.items()):
        if name not in covered and total > 0:
            out.append({"provider": name, "spent_usd": round(total, 4), "budget_usd": None})
    return out


def _issues(start: datetime, end: datetime, zone: tzinfo) -> list[str]:
    rows = db.fetch_all(
        """
        select created_at, type, message from events
        where type = any(%s) and created_at >= %s and created_at < %s
        order by id desc limit 30
        """,
        (list(ISSUE_TYPES), start, end),
    )
    return [
        f"{row['created_at'].astimezone(zone):%H:%M} {row['message'] or row['type']}"[:160]
        for row in reversed(rows)
    ]


def build(day: date | None = None, now: datetime | None = None) -> dict[str, Any]:
    """The report of one day as a JSON-serialisable object."""
    now = now or datetime.now(UTC)
    zone = report_zone()
    day = day or default_day(now, zone)
    start, end = day_bounds(day, zone)
    accounts = _accounts(start, end)
    return {
        "day": day.isoformat(),
        "timezone": clock.zone_name(zone),
        "timezone_label": clock.zone_label(zone),
        "generated_at": now.astimezone(UTC).isoformat(),
        "env": env().APP_ENV,
        "accounts": accounts,
        "views": _views(accounts, end),
        "top_clips": _top_clips(end, settings.get_int("reports.top_clips")),
        "ypp": _ypp(accounts, end),
        "posts": _posts(start, end),
        "spikes": _spikes(start, end),
        "clips": _clips(start, end),
        "discovery": _discovery(start, end),
        "guardian": _guardian(start, end, now),
        "spend": _spend(start, end),
        "issues": _issues(start, end, zone),
    }


# ── text ─────────────────────────────────────────────────────


def compact(number: int | float | None) -> str:
    """`950`, `6.1k`, `88k`, `2.1M`, `10M`; a dash for an unknown number."""
    if number is None:
        return "–"
    value = float(number)
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value < 1000:
        return f"{sign}{value:.0f}"
    for limit, suffix in ((1_000_000, "k"), (1_000_000_000, "M")):
        if value < limit:
            scaled = value / (limit / 1000)
            return f"{sign}{scaled:.1f}{suffix}" if scaled < 10 else f"{sign}{scaled:.0f}{suffix}"
    return f"{sign}{value / 1_000_000_000:.1f}B"


def change(number: int | None) -> str:
    if number is None:
        return ""
    return f" ({'+' if number >= 0 else '-'}{compact(abs(number))})"


def clip_title(title: str) -> str:
    """A clip title on one line, cut to size, then escaped (so the cut never splits an entity)."""
    text = " ".join(str(title).split())
    if len(text) > MAX_TITLE_CHARS:
        text = text[: MAX_TITLE_CHARS - 1].rstrip() + "…"
    return escape(text, quote=False)


def duration(seconds: int | None) -> str:
    return "–" if seconds is None else f"{seconds // 60}m{seconds % 60:02d}s"


def percent(value: int | float | None, target: Any) -> str:
    if value is None or not isinstance(target, int | float) or isinstance(target, bool) or target <= 0:
        return ""
    return " ✅" if value >= target else f" ({100 * value / target:.0f}%)"


def _lines(report: Mapping[str, Any]) -> list[tuple[int, str]]:
    """The report as `(priority, line)`; lines with a higher number are dropped first when it is too long."""
    out: list[tuple[int, str]] = []
    env_name = escape(str(report["env"]), quote=False)
    zone = str(report.get("timezone_label") or report["timezone"])
    out.append(
        (
            0,
            f"⭐ <b>6 Stars Clips</b> — daily report "
            f"({escape(str(report['day']))} {escape(zone)}, {env_name})",
        )
    )
    out.append((0, ""))

    accounts = [item for item in report["accounts"] if item["enabled"]]
    followers = " · ".join(
        f"{escape(str(item['account_id']), quote=False)} {compact(item['followers'])}"
        f"{change(item['followers_change'])}"
        for item in accounts
    )
    out.append((0, f"👥 Followers  {followers or 'no accounts'}"))
    views = report["views"]
    medians = " · ".join(
        f"{PLATFORM_SHORT.get(name, escape(name, quote=False))} {compact(value)}"
        for name, value in views["median_by_platform"].items()
    )
    out.append(
        (
            0,
            f"👀 Views  total {compact(views['total'])}{change(views['change'])}"
            + (f" · median per video: {medians}" if medians else ""),
        )
    )
    for index, clip in enumerate(report["top_clips"]):
        platform = PLATFORM_SHORT.get(str(clip["platform"]), escape(str(clip["platform"]), quote=False))
        label = "🏆 Top clip" if index == 0 else "     also"
        out.append(
            (
                0 if index == 0 else 3,
                f"{label}  “{clip_title(clip['title'])}” — {compact(clip['views'])} views ({platform})",
            )
        )
    ypp = report["ypp"]
    out.append(
        (
            1,
            f"📈 YPP  subs {compact(ypp['subscribers'])} / {compact(ypp['subscribers_target'])}"
            f"{percent(ypp['subscribers'], ypp['subscribers_target'])} · Shorts views 90d "
            f"{compact(ypp['shorts_views_90d'])} / {compact(ypp['shorts_views_target'])}"
            f"{percent(ypp['shorts_views_90d'], ypp['shorts_views_target'])}",
        )
    )
    out.append((0, ""))

    posts = report["posts"]
    out.append(
        (
            0,
            f"📤 Posts  hourly {posts['hourly_published']} ({posts['slots_used']}/24 slots, "
            f"{posts['slots_skipped_events']} skips) · spike {posts['spike_published']} "
            f"· failed {posts['failed']}" + (f" · dry runs {posts['dry_run']}" if posts["dry_run"] else ""),
        )
    )
    per_account = " · ".join(
        f"{escape(str(item['account_id']), quote=False)} {item['published']}/{item['cap_24h']}"
        + (f" (failed {item['failed']})" if item["failed"] else "")
        + (" ⏸" if item["paused_until"] else "")
        for item in accounts
    )
    out.append((1, f"     per account: {per_account or 'none'}"))
    spikes = report["spikes"]
    out.append(
        (
            1,
            f"⚡ Spikes  detected {spikes['detected']} · S {spikes['S']} · A {spikes['A']} "
            f"· B {spikes['B']} · "
            f"rejected {spikes['rejected']} · median spike→post {duration(spikes['median_spike_to_post_s'])}",
        )
    )
    clips = report["clips"]
    reasons = ", ".join(
        f"{escape(name, quote=False)} {count}"
        for name, count in list(clips["rejected_by_reason"].items())[:5]
    )
    out.append(
        (
            0,
            f"🎬 Clips  rendered {clips['rendered']} · approved {clips['approved']} "
            f"· rejected {clips['rejected']}"
            + (f" ({reasons})" if reasons else "")
            + (f" · expired {clips['expired']}" if clips["expired"] else ""),
        )
    )
    queue = clips["queue"]
    low = queue["low_watermark"]
    warn = " ⚠️ low" if isinstance(low, int | float) and queue["hourly"] < low else ""
    out.append((0, f"📦 Queue  hourly {queue['hourly']}{warn} · spike {queue['spike']}"))
    discovery = report["discovery"]
    tiers = ", ".join(
        f"{escape(name, quote=False)} {count}" for name, count in sorted(discovery["new_by_tier"].items())
    )
    out.append(
        (
            2,
            f"🧭 Discovery  videos processed {discovery['videos_processed']} · candidates "
            f"{discovery['candidates_checked']} · new creators {discovery['new_creators']}"
            + (f" ({tiers})" if tiers else ""),
        )
    )
    guardian = report["guardian"]
    state = "ON 🔴" if guardian["emergency_stop"] else "off"
    out.append(
        (
            0,
            f"🛡️ Guardian  claims {guardian['claims']} · takedowns {guardian['takedowns']} · strikes "
            f"{guardian['strikes']}/{guardian['strike_limit']} · stop: {state}"
            + (f" · emails ignored {guardian['emails_ignored']}" if guardian["emails_ignored"] else "")
            + (f" · emails to read {guardian['emails_open']}" if guardian["emails_open"] else ""),
        )
    )
    if guardian["emergency_stop"]:
        out.append(
            (
                0,
                "     stop reason: "
                + escape(str(guardian["stop_reason"] or "none given")[:200], quote=False),
            )
        )
    manual = guardian["manual_removals"]
    if manual:
        out.append((0, f"🔴 <b>{len(manual)} post(s) still need manual removal</b>"))
        for item in manual[:MAX_MANUAL_LINKS]:
            link = escape(str(item["post_url"] or "(no link stored)")[:200], quote=False)
            out.append((1, f"     {escape(str(item['platform']), quote=False)}: {link}"))
    spend = " · ".join(
        f"{escape(str(item['provider']), quote=False)} ${item['spent_usd']:.2f}"
        + (f"/${item['budget_usd']:g}" if item["budget_usd"] is not None else "")
        for item in report["spend"]
    )
    out.append((0, f"💸 Spend  {spend or 'nothing'}"))
    issues = report["issues"]
    if issues:
        out.append((2, f"⚠️ Issues ({len(issues)})"))
        for text in issues[-MAX_ISSUES:]:
            out.append((4, f"     {escape(text, quote=False)}"))
    return out


def render_telegram(report: Mapping[str, Any], max_chars: int = TELEGRAM_MAX_CHARS) -> str:
    """The report as HTML-safe text of at most `max_chars` characters."""
    lines = _lines(report)
    limit = min(max_chars, TELEGRAM_LIMIT)

    def size(items: Sequence[tuple[int, str]]) -> int:
        return sum(len(text) + 1 for _, text in items)

    priority = max((level for level, _ in lines), default=0)
    while size(lines) > limit and priority > 0:
        lines = [item for item in lines if item[0] < priority]
        priority -= 1
    kept: list[str] = []
    used = 0
    for _, text in lines:
        if used + len(text) + 1 > limit:
            break  # whole lines only: a line is never cut inside a tag or an entity
        kept.append(text)
        used += len(text) + 1
    return "\n".join(kept).strip()
