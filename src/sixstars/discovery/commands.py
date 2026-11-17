"""Implementation of the `sixstars discovery` commands (M09): `run-once` runs one pass and prints the
summary with the best live streams and videos, `permissions` runs the permission check (P6) for creators
that are due, `set-tier` is the owner's override (for example to block a creator), `doctor` checks every
source path once from this machine.
"""

import json
import sys
from datetime import UTC, datetime
from typing import Any, Final

import typer

from sixstars import db
from sixstars.clients.twitch import TwitchClient
from sixstars.clients.youtube import YouTubeClient
from sixstars.discovery import store, tiers
from sixstars.discovery.config import DiscoveryConfig
from sixstars.discovery.run import DiscoverySummary, run_discovery

EXIT_ERROR: Final = 1
EXIT_USAGE: Final = 2
TITLE_WIDTH: Final = 60
CHANNEL_WIDTH: Final = 24
TIERS: Final = ("preferred", "auto", "restricted", "blocked")


def _safe_stdout() -> None:
    """Never crash on a console that cannot show a character (emoji on a Windows code page)."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(errors="replace")


def _cut(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 3] + "..."


def format_row(row: dict[str, Any], live: bool) -> str:
    """One line of the top list: score, audience, channel, title and flags."""
    audience = row["viewers"] if live else row["views"]
    number = "?" if audience is None else f"{audience:,}"
    flags = [row["tier"] if row["checked"] else f"{row['tier']}?"]
    if not live and row["has_chat"]:
        flags.append("chat")
    if row["language"] and row["language"] != "en":
        flags.append(row["language"])
    if row["picked"]:
        flags.append("PICKED")
    return (
        f"{row['score']:6.2f}  {number:>11}  {_cut(row['channel'], CHANNEL_WIDTH):<{CHANNEL_WIDTH}}  "
        f"{_cut(row['title'], TITLE_WIDTH):<{TITLE_WIDTH}}  [{', '.join(flags)}]"
    )


def format_summary(summary: DiscoverySummary) -> str:
    """The text `run-once` prints."""
    widened = ", widened: hourly queue is low" if summary.vod_widened else ""
    sources = "; ".join(
        f"{s['name']} {s['status']} {s['found']}" + (f" ({s['detail']})" if s["detail"] else "")
        for s in summary.sources
    )
    lines = [
        f"Discovery for {summary.target_game}: {summary.duration_s:.1f} s",
        f"  live streams {summary.live_seen}, videos {summary.videos_seen}, creators {summary.creators_seen} "
        f"({summary.new_creators} new), candidates stored {summary.candidates_stored}",
        f"  live slots: {summary.sessions_assigned} assigned, {summary.sessions_released} released, "
        f"{summary.sessions_active} active",
        f"  videos queued: {summary.videos_queued} (cutoff {summary.vod_cutoff:g}, max {summary.vod_max}"
        f"{widened})",
        f"  permission checks: {summary.permission_checked} made, {summary.permission_jobs} jobs queued",
        f"  cost: {summary.youtube_searches} yt-dlp searches, {summary.youtube_quota_units} YouTube quota "
        f"units ({summary.youtube_quota_units_today:.0f} today), {summary.youtube_search_api_calls} Data "
        f"API searches ({summary.youtube_search_api_calls_today} today), Apify "
        f"{summary.apify_cost_usd:.3f} USD ({summary.apify_spent_today_usd:.2f} today, "
        f"{summary.apify_spent_month_usd:.2f} this month)",
        f"  sources: {sources}",
    ]
    lines += [f"  note: {note}" for note in summary.notes]
    if summary.skipped:
        lines.append("  skipped: " + ", ".join(f"{k} {v}" for k, v in sorted(summary.skipped.items())))

    def header(audience: str) -> str:
        return f"{'score':>6}  {audience:>11}  {'channel':<{CHANNEL_WIDTH}}  title"

    lines += ["", f"Top {len(summary.top_live)} live streams", header("viewers")]
    lines += [format_row(row, live=True) for row in summary.top_live]
    lines += ["", f"Top {len(summary.top_videos)} videos", header("views")]
    lines += [format_row(row, live=False) for row in summary.top_videos]
    clips = summary.twitch_clips
    if clips or summary.top_clips:
        skipped = ", ".join(f"{k} {v}" for k, v in sorted((clips.get("skipped") or {}).items()))
        lines += [
            "",
            f"Twitch clips: {clips.get('eligible', 0)} fit, {clips.get('queued', 0)} queued"
            + (f"; skipped: {skipped}" if skipped else ""),
            f"{'rank':>8}  {'views':>7}  {'age h':>6}  {'len s':>5}  {'channel':<{CHANNEL_WIDTH}}  title",
        ]
        lines += [format_clip_row(row) for row in summary.top_clips]
    lines.append("")
    lines.append("Flags: tier (a ? means the permission check has not run yet), chat = has a chat replay.")
    return "\n".join(lines)


def format_clip_row(row: dict[str, Any]) -> str:
    """One line of the Twitch clips table."""
    flag = "PICKED" if row.get("picked") else (row.get("skipped") or "")
    language = row.get("language") or "?"
    return (
        f"{float(row.get('rank') or 0):>8.1f}  {int(row.get('views') or 0):>7,}  "
        f"{float(row.get('age_h') or 0):>6.1f}  {float(row.get('duration_s') or 0):>5.0f}  "
        f"{_cut(str(row.get('channel') or ''), CHANNEL_WIDTH):<{CHANNEL_WIDTH}}  "
        f"{_cut(str(row.get('title') or ''), 60)}  [{language}] {flag}".rstrip()
    )


def run_once(as_json: bool = False) -> int:
    """Run one pass against the configured database and print the result."""
    _safe_stdout()
    summary = run_discovery()
    if as_json:
        typer.echo(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
    else:
        typer.echo(format_summary(summary))
    return 0


def permissions(limit: int) -> int:
    """Run the permission check now for up to `limit` creators that are due, most recently seen first."""
    _safe_stdout()
    config = DiscoveryConfig.from_settings()
    now = datetime.now(UTC)
    rows = db.fetch_all(
        """
        select * from creators
        where tier <> 'blocked'
          and (description_checked_at is null or description_checked_at < now() - make_interval(days => %s))
        order by updated_at desc, id
        limit %s
        """,
        (config.permission_recheck_days, limit),
    )
    creators = [store.creator_from_row(row) for row in rows]
    if not creators:
        typer.echo("no creator is due for a permission check")
        return 0
    checked = tiers.check_creators(creators, youtube=YouTubeClient(), twitch=TwitchClient(), now=now)
    reasons = {str(row["id"]): row["tier_reason"] for row in store.creators_by_id(list(checked)).values()}
    for creator in creators:
        result = checked.get(creator.id)
        name = _cut(creator.display_name or creator.handle, 30)
        if result is None:
            typer.echo(f"{creator.platform:8} {name:30} not checked")
            continue
        reason = _cut(reasons.get(creator.id) or "", 110)
        typer.echo(f"{creator.platform:8} {name:30} {result.tier:10} {reason}")
    typer.echo(f"{len(checked)} of {len(creators)} checked")
    return 0 if checked else EXIT_ERROR


def set_tier(platform: str, channel: str, tier: str, reason: str) -> int:
    """Owner override of one creator's tier."""
    if platform not in ("youtube", "twitch") or tier not in TIERS:
        typer.echo(f"platform must be youtube or twitch and tier one of {', '.join(TIERS)}", err=True)
        return EXIT_USAGE
    creator = tiers.set_tier(platform, channel, tier, reason)
    if creator is None:
        typer.echo(
            f"unknown {platform} creator {channel!r}: to block a channel discovery has not seen yet, add "
            "it to the setting discovery.excluded_channels",
            err=True,
        )
        return EXIT_ERROR
    typer.echo(f"{creator.platform} {creator.handle} ({creator.channel_id}): {creator.tier}")
    return 0


def doctor(video: str = "", search_api: bool = False, as_json: bool = False) -> int:
    """Check every source path once from this machine and print which work (read-only, cheap)."""
    from sixstars.discovery import doctor as checks

    _safe_stdout()
    try:
        results = checks.run_doctor(video=video or None, search_api=search_api)
    except ValueError as exc:  # --video is not a YouTube id or URL
        typer.echo(str(exc), err=True)
        return EXIT_USAGE
    if as_json:
        typer.echo(json.dumps(checks.as_rows(results), ensure_ascii=False, indent=2))
    else:
        typer.echo(checks.format_table(results))
    return 0
