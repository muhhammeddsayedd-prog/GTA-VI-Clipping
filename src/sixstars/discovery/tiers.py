"""Creator rows and their permission tiers (decision D10): `preferred` creators allow clipping, `auto`
creators are clipped within the posting caps, `restricted` and `blocked` creators are never clipped.
Implemented in M09.

- `upsert_creators` registers every channel a pass saw (one statement, idempotent on platform + channel id)
  and blocks the channels listed in `discovery.excluded_channels`.
- The tier comes from prompt P6 on the channel description (`sixstars.ai.calls.read_permission`). It is
  cached on the row (`description_checked_at`) and read again after `discovery.permission_recheck_days`.
- A check runs either inside a discovery pass for creators about to be picked (`check_creators`) or as a
  `creator.permission` job (`handle_permission_job`, queue `normal`, priority 20).
- `blocked` is only ever set by the owner (settings list, CLI, takedown handling) and no check changes it.
"""

from collections.abc import Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from psycopg.types.json import Jsonb

from sixstars import db, events, jobs
from sixstars.ai import calls
from sixstars.ai.schemas import PermissionVerdict
from sixstars.clients._http import ClientError
from sixstars.clients.anthropic_client import AnthropicClient
from sixstars.clients.twitch import TwitchClient
from sixstars.clients.youtube import YouTubeClient
from sixstars.discovery import store
from sixstars.discovery.models import Candidate, Creator
from sixstars.logging import get_logger

PERMISSION_JOB_TYPE: Final = "creator.permission"
PERMISSION_JOB_PRIORITY: Final = 20
EXCLUDED_REASON: Final = "excluded by settings (discovery.excluded_channels)"
MANUAL_PREFIX: Final = "manual"
TITLES_FOR_PROMPT: Final = 8
CHECK_WORKERS: Final = 6

_log = get_logger("discovery")


@dataclass(frozen=True)
class ChannelInfo:
    """Public channel text and numbers fetched for a permission check or a follower count."""

    name: str
    description: str
    handle: str | None = None
    followers: int | None = None


# ── pure rules ───────────────────────────────────────────────


def is_excluded(platform: str, channel_id: str, handle: str, excluded: Sequence[str]) -> bool:
    """True when the channel is on the owner's exclusion list. An entry is a channel id or a handle
    (case-insensitive, with or without `@`), optionally prefixed with `youtube:` or `twitch:`."""

    def tidy(value: str) -> str:
        return value.strip().casefold().removeprefix("@")

    names = {tidy(channel_id), tidy(handle)} - {""}
    for entry in excluded:
        prefix, _, rest = entry.partition(":")
        if rest and prefix.strip().casefold() in ("youtube", "twitch"):
            if prefix.strip().casefold() == platform and tidy(rest) in names:
                return True
        elif tidy(entry) in names:
            return True
    return False


def needs_check(creator: Creator, now: datetime, recheck_days: int) -> bool:
    """True when the creator was never checked or the cached verdict is older than the re-check interval.
    Blocked creators are never checked: nothing a description says can unblock them."""
    if creator.tier == "blocked":
        return False
    checked = creator.description_checked_at
    return checked is None or now - checked >= timedelta(days=recheck_days)


def clip_allowed(creator: Creator, require_check: bool) -> bool:
    """May this creator get a live slot or a video job? Never when forbidden; with `require_check` also
    not before the first permission check has run."""
    if creator.forbidden:
        return False
    return creator.description_checked_at is not None or not require_check


def decide_tier(current: str, reason: str | None, verdict: PermissionVerdict) -> tuple[str, str | None]:
    """The tier and reason to store after a P6 verdict.

    `blocked` never changes. A creator who forbids clipping is always `restricted`. A tier the owner set
    by hand (reason starting with "manual") is otherwise kept.
    """
    if current == "blocked":
        return current, reason
    if verdict.tier == "restricted":
        return "restricted", verdict.evidence or "channel text forbids clipping"
    if reason is not None and reason.startswith(MANUAL_PREFIX):
        return current, reason
    return verdict.tier, verdict.evidence


# ── creator rows ─────────────────────────────────────────────


def upsert_creators(
    candidates: Sequence[Candidate], excluded: Sequence[str]
) -> dict[tuple[str, str], Creator]:
    """Insert or refresh the creator of every candidate and return them by `(platform, channel_id)`.

    A repeated pass changes nothing but `updated_at`. Channels on the exclusion list become `blocked`.
    """
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate in candidates:
        row = by_key.setdefault(
            candidate.channel_key,
            {
                "platform": candidate.platform,
                "channel_id": candidate.channel_id,
                "handle": candidate.channel_handle or candidate.channel_name or candidate.channel_id,
                "display_name": candidate.channel_name or None,
                "language": None,
                "follower_count": None,
            },
        )
        if row["language"] is None and candidate.source_type == "live":
            row["language"] = candidate.language
        row["language"] = row["language"] or candidate.language
        if row["follower_count"] is None:
            row["follower_count"] = candidate.followers
    if not by_key:
        return {}
    rows = db.fetch_all(
        """
        insert into creators (platform, channel_id, handle, display_name, language, follower_count)
        select platform, channel_id, handle, display_name, language, follower_count
        from jsonb_to_recordset(%s) as x(
          platform text, channel_id text, handle text, display_name text, language text,
          follower_count bigint)
        on conflict (platform, channel_id) do update
          set display_name = coalesce(excluded.display_name, creators.display_name),
              language = coalesce(excluded.language, creators.language),
              follower_count = coalesce(creators.follower_count, excluded.follower_count),
              updated_at = now()
        returning *, (xmax = 0) as inserted
        """,
        (Jsonb(list(by_key.values())),),
    )
    creators = {
        (row["platform"], row["channel_id"]): store.creator_from_row(row, is_new=bool(row["inserted"]))
        for row in rows
    }
    for key, creator in list(creators.items()):
        if creator.tier != "blocked" and is_excluded(
            creator.platform, creator.channel_id, creator.handle, excluded
        ):
            creators[key] = block(creator, EXCLUDED_REASON)
    return creators


def block(creator: Creator, reason: str) -> Creator:
    """Mark a creator as never to be clipped, drop their waiting work and record the event."""
    db.execute(
        "update creators set tier = 'blocked', tier_reason = %s, blocked_at = now(), updated_at = now() "
        "where id = %s",
        (reason, creator.id),
    )
    skipped = store.retire_creator_work(creator.id)
    events.emit(
        "creator.blocked",
        level="warn",
        entity_type="creator",
        entity_id=creator.id,
        message=f"{creator.platform} {creator.handle} blocked: {reason}",
        data={"reason": reason, "videos_skipped": skipped},
    )
    return replace(creator, tier="blocked")


def set_tier(platform: str, channel: str, tier: str, reason: str) -> Creator | None:
    """Owner override from the CLI: set the tier of a known creator found by channel id or handle. The
    stored reason is prefixed with "manual" so permission checks keep it. Returns None when unknown."""
    row = db.fetch_one(
        "select * from creators where platform = %s and (channel_id = %s or lower(handle) = lower(%s))",
        (platform, channel, channel.removeprefix("@")),
    )
    if row is None:
        return None
    creator = store.creator_from_row(row)
    note = f"{MANUAL_PREFIX}: {reason}".strip()
    if tier == "blocked":
        return block(creator, note)
    db.execute(
        "update creators set tier = %s, tier_reason = %s, blocked_at = null, updated_at = now() "
        "where id = %s",
        (tier, note, creator.id),
    )
    if tier == "restricted":
        store.retire_creator_work(creator.id)
    events.emit(
        "creator.tier_changed",
        entity_type="creator",
        entity_id=creator.id,
        message=f"{creator.platform} {creator.handle}: {creator.tier} -> {tier} ({note})",
        data={"old": creator.tier, "new": tier, "reason": note},
    )
    return replace(creator, tier=tier)  # type: ignore[arg-type]


def save_channel_facts(facts: Mapping[str, ChannelInfo], creators: Collection[Creator]) -> None:
    """Store follower counts (and the YouTube @handle) fetched from the platform, in one statement."""
    rows = [
        {"id": creator.id, "followers": info.followers, "handle": info.handle}
        for creator in creators
        if (info := facts.get(creator.channel_id)) is not None and (info.followers is not None or info.handle)
    ]
    if not rows:
        return
    db.execute(
        """
        update creators c
        set follower_count = coalesce(x.followers, c.follower_count),
            handle = coalesce(nullif(x.handle, ''), c.handle),
            updated_at = now()
        from jsonb_to_recordset(%s) as x(id uuid, followers bigint, handle text)
        where c.id = x.id
        """,
        (Jsonb(rows),),
    )


# ── channel text ─────────────────────────────────────────────


def youtube_channel_info(client: YouTubeClient, channel_ids: Sequence[str]) -> dict[str, ChannelInfo]:
    """`channels.list` (snippet + statistics, 1 quota unit per 50 channels) as `ChannelInfo` by channel id.
    A hidden subscriber count is stored as 0 so it is not asked for again."""
    info: dict[str, ChannelInfo] = {}
    for item in client.channels_list(list(channel_ids), parts=("snippet", "statistics")):
        snippet = item.get("snippet") or {}
        statistics = item.get("statistics") or {}
        count = statistics.get("subscriberCount")
        followers = int(count) if isinstance(count, str | int) and str(count).isdigit() else None
        if followers is None and statistics.get("hiddenSubscriberCount"):
            followers = 0
        info[str(item.get("id", ""))] = ChannelInfo(
            name=str(snippet.get("title") or ""),
            description=str(snippet.get("description") or ""),
            handle=str(snippet.get("customUrl") or "") or None,
            followers=followers,
        )
    return info


def twitch_channel_info(client: TwitchClient, user_ids: Sequence[str]) -> dict[str, ChannelInfo]:
    """Twitch user descriptions (`GET /helix/users`) as `ChannelInfo` by user id."""
    return {
        user.id: ChannelInfo(name=user.display_name or user.login, description=user.description or "")
        for user in client.get_users(list(user_ids))
    }


def recent_titles(creator_ids: Collection[str]) -> dict[str, list[str]]:
    """The latest distinct stream and video titles we saw for each creator (context for P6)."""
    if not creator_ids:
        return {}
    rows = db.fetch_all(
        """
        select creator_id::text as creator_id, title from (
          select distinct on (creator_id, title) creator_id, title, checked_at
          from candidates
          where creator_id = any(%s::uuid[]) and title is not null and title <> ''
          order by creator_id, title, checked_at desc
        ) t
        order by creator_id, checked_at desc
        """,
        (list(creator_ids),),
    )
    titles: dict[str, list[str]] = {}
    for row in rows:
        bucket = titles.setdefault(row["creator_id"], [])
        if len(bucket) < TITLES_FOR_PROMPT:
            bucket.append(row["title"])
    return titles


# ── permission checks ────────────────────────────────────────


def apply_verdict(creator: Creator, verdict: PermissionVerdict, now: datetime) -> Creator:
    """Store a P6 verdict on the creator row and return the updated creator. A change of tier writes a
    `creator.tier_changed` event; a creator who became restricted loses their waiting work."""
    row = db.fetch_one("select tier, tier_reason from creators where id = %s", (creator.id,))
    current = str(row["tier"]) if row is not None else creator.tier
    reason = row["tier_reason"] if row is not None else None
    tier, new_reason = decide_tier(current, reason, verdict)
    db.execute(
        "update creators set tier = %s, tier_reason = %s, description_checked_at = %s, updated_at = now() "
        "where id = %s",
        (tier, new_reason, now, creator.id),
    )
    if tier != current:
        skipped = store.retire_creator_work(creator.id) if tier == "restricted" else 0
        events.emit(
            "creator.tier_changed",
            level="warn" if tier == "restricted" else "info",
            entity_type="creator",
            entity_id=creator.id,
            message=f"{creator.platform} {creator.handle}: {current} -> {tier}",
            data={"old": current, "new": tier, "evidence": verdict.evidence, "videos_skipped": skipped},
        )
    return replace(creator, tier=tier, description_checked_at=now)  # type: ignore[arg-type]


def check_creators(
    creators: Sequence[Creator],
    *,
    youtube: YouTubeClient | None,
    twitch: TwitchClient | None,
    anthropic: AnthropicClient | None = None,
    now: datetime,
    known_info: Mapping[tuple[str, str], ChannelInfo] | None = None,
) -> dict[str, Creator]:
    """Run the permission check (P6) for each creator and return the updated creators by id.

    Channel descriptions are fetched in one batch per platform (`known_info` supplies those a pass already
    has). A creator whose description cannot be read or whose check fails (budget, API error) is left
    unchecked and missing from the result: with `discovery.require_permission_check` they are simply not
    picked until a later check succeeds.
    """
    info: dict[tuple[str, str], ChannelInfo] = dict(known_info or {})
    missing_yt = [c.channel_id for c in creators if c.platform == "youtube" and c.key not in info]
    missing_tw = [c.channel_id for c in creators if c.platform == "twitch" and c.key not in info]
    try:
        if missing_yt and youtube is not None and youtube.is_configured():
            fetched = youtube_channel_info(youtube, missing_yt)
            info.update({("youtube", channel_id): value for channel_id, value in fetched.items()})
            save_channel_facts(fetched, [c for c in creators if c.platform == "youtube"])
        if missing_tw and twitch is not None and twitch.is_configured():
            fetched = twitch_channel_info(twitch, missing_tw)
            info.update({("twitch", channel_id): value for channel_id, value in fetched.items()})
    except ClientError as exc:
        _log.warning("permission_channel_fetch_failed", error=str(exc)[:200])
    ready = [creator for creator in creators if creator.key in info]
    if not ready:
        return {}
    titles = recent_titles([creator.id for creator in ready])

    def ask(creator: Creator) -> PermissionVerdict | None:
        channel = info[creator.key]
        try:
            return calls.read_permission(
                channel.description,
                channel.name or creator.display_name or creator.handle,
                titles.get(creator.id, []),
                ref_id=creator.id,
                client=anthropic,
            )
        except Exception as exc:  # budget, API error, refusal, missing key: check again later
            _log.warning(
                "permission_check_failed",
                creator_id=creator.id,
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )
            return None

    with ThreadPoolExecutor(max_workers=CHECK_WORKERS, thread_name_prefix="p6") as pool:
        verdicts = list(pool.map(ask, ready))
    checked: dict[str, Creator] = {}
    with db.transaction():
        for creator, verdict in zip(ready, verdicts, strict=True):
            if verdict is not None:
                checked[creator.id] = apply_verdict(creator, verdict, now)
    return checked


def enqueue_permission_job(creator_id: str) -> int | None:
    """Queue a `creator.permission` job (payload `{"creator_id": ...}`); one per creator at a time."""
    return jobs.enqueue(
        "normal",
        PERMISSION_JOB_TYPE,
        {"creator_id": creator_id},
        priority=PERMISSION_JOB_PRIORITY,
        dedupe_key=f"{PERMISSION_JOB_TYPE}:{creator_id}",
    )


def handle_permission_job(job: jobs.Job) -> None:
    """Worker handler of `creator.permission`: read the channel description and store the tier.

    Raises when the check could not be made, so the job queue retries it with backoff.
    """
    creator_id = str(job.payload.get("creator_id", ""))
    row = store.creators_by_id([creator_id]).get(creator_id) if creator_id else None
    if row is None:
        jobs.fail(job.id, f"creator {creator_id!r} not found", retry=False)
        return
    creator = store.creator_from_row(row)
    if creator.tier == "blocked":
        return
    checked = check_creators([creator], youtube=YouTubeClient(), twitch=TwitchClient(), now=datetime.now(UTC))
    if creator.id not in checked:
        raise RuntimeError(f"permission check for creator {creator.id} could not be completed")


def register_handlers() -> None:
    """Register the discovery job handlers with the queue worker (called by the worker wiring)."""
    from sixstars.services import worker

    if PERMISSION_JOB_TYPE not in worker.HANDLERS:
        worker.register(PERMISSION_JOB_TYPE, handle_permission_job)
