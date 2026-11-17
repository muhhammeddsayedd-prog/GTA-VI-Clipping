"""The Twitch Clips step of a discovery pass (decision D110).

    run(twitch, discovery_config, now) -> ClipsReport

One pass: read the most viewed clips of the target game made in the look-back window (one Helix request
per 100 clips), drop the ones that do not fit (`select.unfit_reason`), rank the rest, and queue the best
few that are new, from a broadcaster who may be clipped, and not a moment we already have. Each queued
clip is a `viewer_clips` row plus a `viewer_clip.process` job.

The broadcaster is the creator. The existing tier rules apply to them exactly as for videos:
`discovery.excluded_channels`, the `restricted` / `blocked` tiers, and the permission check (P6) before
the first clip when `discovery.require_permission_check` is on. The viewer who made the clip is stored
(`viewer_clips.clipped_by`) and never credited.

The step does nothing when `twitch_clips.enabled` is false, when Twitch is not configured, or while
`control.emergency_stop` is set. It never raises: a failure is reported in the summary like any other
source's.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from sixstars import settings
from sixstars.clients._http import ClientError
from sixstars.clients.anthropic_client import AnthropicClient
from sixstars.clients.twitch import TwitchClient, TwitchClip, TwitchUser
from sixstars.discovery import sources, tiers
from sixstars.discovery.config import DiscoveryConfig
from sixstars.discovery.models import Candidate, Creator
from sixstars.discovery.tiers import ChannelInfo
from sixstars.logging import get_logger
from sixstars.viewer_clips import SOURCE_NAME, select, store
from sixstars.viewer_clips.config import ClipsConfig, enabled

ORIGIN: Final = "twitch_clips"
TOP_N: Final = 10
# Creators are looked up (and checked) for this many times more clips than there is room for, so that a
# refused creator or a repeated moment near the top does not leave the room unused.
SHORTLIST_FACTOR: Final = 4
ERROR_CHARS: Final = 300
# A row whose job is gone (it failed for good while its worker was dead) is closed after the grace period
# of the VOD lane, so it does not count against `twitch_clips.max_backlog` for ever.
ORPHAN_GRACE_KEY: Final = "vod.orphan_grace_s"
ORPHAN_GRACE_DEFAULT_S: Final = 300.0

_log = get_logger("discovery")


@dataclass
class ClipsReport:
    """What the step did. `status` is ok, empty, skipped, not_configured or error, as for every source."""

    status: str
    found: int = 0
    detail: str = ""
    eligible: int = 0
    queued: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    permission_checked: int = 0
    permission_jobs: int = 0
    top: list[dict[str, Any]] = field(default_factory=list)

    def as_source(self) -> dict[str, Any]:
        """The entry in the summary's `sources` list."""
        return {"name": SOURCE_NAME, "status": self.status, "found": self.found, "detail": self.detail}

    def counts(self) -> dict[str, Any]:
        """The numbers for the summary and the `discovery.run` event (no clip list)."""
        return {
            "eligible": self.eligible,
            "queued": self.queued,
            "skipped": dict(self.skipped),
            "permission_checked": self.permission_checked,
            "permission_jobs": self.permission_jobs,
        }


def _add(totals: dict[str, int], more: dict[str, int]) -> None:
    for reason, count in more.items():
        if count:
            totals[reason] = totals.get(reason, 0) + count


def _candidate(clip: TwitchClip, login: str) -> Candidate:
    return Candidate(
        platform="twitch",
        source_type="vod",
        source_id=clip.id,
        url=clip.url,
        title=clip.title,
        channel_id=clip.broadcaster_id,
        channel_handle=login,
        channel_name=clip.broadcaster_name,
        origin=ORIGIN,
        language=select.language_of(clip),
    )


def _resolve_creators(
    shortlist: list[select.Ranked],
    twitch: TwitchClient,
    config: DiscoveryConfig,
    now: datetime,
    anthropic: AnthropicClient | None,
    report: ClipsReport,
) -> dict[str, Creator]:
    """The creator row of every broadcaster on the shortlist, by Twitch user id, after the permission
    checks of this pass. A broadcaster we have never stored and whose login cannot be read is left out
    (the clip waits for a later pass): the login is the handle shown in the credit."""
    channel_ids = list(dict.fromkeys(item.clip.broadcaster_id for item in shortlist))
    have = store.creators_by_channel(channel_ids)
    missing = [channel_id for channel_id in channel_ids if channel_id not in have]
    users: dict[str, TwitchUser] = {}
    if missing:
        try:
            users = {user.id: user for user in twitch.get_users(missing)}
        except ClientError as exc:
            _log.warning("twitch_clips_users_failed", error=str(exc)[:ERROR_CHARS])
    candidates = [
        _candidate(
            item.clip,
            str(have[item.clip.broadcaster_id]["handle"])
            if item.clip.broadcaster_id in have
            else users[item.clip.broadcaster_id].login,
        )
        for item in shortlist
        if item.clip.broadcaster_id in have or item.clip.broadcaster_id in users
    ]
    creators = {
        creator.channel_id: creator
        for creator in tiers.upsert_creators(candidates, config.excluded_channels).values()
    }
    info = {
        ("twitch", user.id): ChannelInfo(name=user.display_name or user.login, description=user.description)
        for user in users.values()
    }
    due = [
        creator
        for creator in creators.values()
        if not creator.forbidden and tiers.needs_check(creator, now, config.permission_recheck_days)
    ]
    first = [creator for creator in due if creator.description_checked_at is None]
    first = first[: config.permission_checks_per_run]
    checked = (
        tiers.check_creators(
            first, youtube=None, twitch=twitch, anthropic=anthropic, now=now, known_info=info
        )
        if first
        else {}
    )
    report.permission_checked = len(checked)
    for creator in checked.values():
        creators[creator.channel_id] = creator
    for creator in due:
        if report.permission_jobs >= config.permission_jobs_per_run:
            break
        if creator.id not in checked and tiers.enqueue_permission_job(creator.id) is not None:
            report.permission_jobs += 1
    return creators


def _run(
    twitch: TwitchClient,
    config: DiscoveryConfig,
    clips_config: ClipsConfig,
    now: datetime,
    anthropic: AnthropicClient | None,
) -> ClipsReport:
    game_id = sources.twitch_game_id(twitch, config.target_game, now)
    if game_id is None:
        return ClipsReport("empty", detail=f"no Twitch category named {config.target_game!r}")
    clips = twitch.get_clips(
        game_id,
        now - timedelta(hours=clips_config.lookback_h),
        now,
        max_items=clips_config.read_per_pass,
    )
    report = ClipsReport("ok" if clips else "empty", found=len(clips))
    ranked, unfit = select.rank_all(
        clips, now, clips_config, config.title_blocklist, config.score.non_english_penalty
    )
    _add(report.skipped, unfit)
    report.eligible = len(ranked)
    known = store.known_ids([item.clip.id for item in ranked])
    _add(report.skipped, {select.ALREADY_SEEN: sum(1 for item in ranked if item.clip.id in known)})

    orphans = store.close_orphans(settings.get_float(ORPHAN_GRACE_KEY, ORPHAN_GRACE_DEFAULT_S))
    if orphans:
        _log.warning("twitch_clips_orphans_closed", count=len(orphans))
    recent, waiting = store.queue_counts()
    room = select.room(clips_config, recent, waiting)
    selection = select.Selection()
    if room <= 0:
        report.detail = (
            f"no room: {recent} queued in 24 h (cap {clips_config.daily_cap}), "
            f"{waiting} waiting (backlog limit {clips_config.max_backlog})"
        )
    else:
        shortlist = select.shortlist(
            ranked, known, room * SHORTLIST_FACTOR, clips_config.max_per_creator_per_pass
        )
        creators = _resolve_creators(shortlist, twitch, config, now, anthropic, report) if shortlist else {}

        def verdict(clip: TwitchClip) -> str | None:
            creator = creators.get(clip.broadcaster_id)
            if creator is None:
                return select.CREATOR_UNCHECKED
            if creator.forbidden:
                return select.FORBIDDEN_CREATOR
            if not tiers.clip_allowed(creator, config.require_permission_check):
                return select.CREATOR_UNCHECKED
            return None

        taken = store.taken_ranges({item.clip.video_id for item in shortlist if item.clip.video_id})
        selection = select.pick(
            shortlist,
            limit=room,
            max_per_creator=clips_config.max_per_creator_per_pass,
            pad_s=clips_config.source_pad_s,
            verdict=verdict,
            taken=taken,
        )
        queued: list[select.Ranked] = []
        for item in selection.picks:
            if store.queue(item, creators[item.clip.broadcaster_id].id) is not None:
                queued.append(item)
        selection.picks = queued
        report.queued = len(queued)
        _add(report.skipped, selection.counts())
    picked = {item.clip.id for item in selection.picks}
    report.top = select.top_rows(ranked, picked, selection.reasons, known, TOP_N)
    return report


def run(
    twitch: TwitchClient | None,
    config: DiscoveryConfig,
    now: datetime,
    anthropic: AnthropicClient | None = None,
) -> ClipsReport:
    """Run the step and return its report. Never raises."""
    try:
        if not enabled():
            return ClipsReport("skipped", detail="twitch_clips.enabled is false")
        if twitch is None or not twitch.is_configured():
            return ClipsReport("not_configured", detail="no Twitch keys")
        if settings.get_bool("control.emergency_stop", False):
            return ClipsReport("skipped", detail="control.emergency_stop is set")
        report = _run(twitch, config, ClipsConfig.load(), now, anthropic)
    except Exception as exc:  # noqa: BLE001 - a source that fails never fails the pass
        _log.warning("twitch_clips_failed", error=type(exc).__name__, detail=str(exc)[:ERROR_CHARS])
        return ClipsReport("error", detail=f"{type(exc).__name__}: {exc}"[:ERROR_CHARS])
    _log.info(
        "twitch_clips_pass",
        status=report.status,
        found=report.found,
        eligible=report.eligible,
        queued=report.queued,
        skipped=report.skipped,
    )
    return report
