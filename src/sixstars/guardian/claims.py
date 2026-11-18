"""Claims and removals check (docs/12 section 1): `POST /v1/guardian/check`.

What can be seen, and with which credential:

- Owner's YouTube OAuth (`videos.list` on our own videos). Sees: a removed video, `uploadStatus=rejected`
  with its reason, country blocks and privacy changes, also on private videos. Does not see: Content ID
  claims that only monetise or track, and copyright strikes as such.
- YouTube API key (public `videos.list`). Sees the same fields, for public videos only. Does not see:
  anything on a private video (it is simply absent), claims and strikes as such.
- Upload-Post post analytics. Sees: "video not found" on TikTok and YouTube; on Instagram only "may have
  been deleted or the token expired". Does not see: why a post is gone, muted audio, or a TikTok or
  Instagram claim that leaves the post up.
- Takedown emails (`takedowns.py`): whatever a platform or a creator writes to the inbox.

There is no OAuth refresh token today (D103), so the API key path runs. The OAuth path is chosen by
`YouTubeClient.has_oauth()` on every pass: it starts working when `YOUTUBE_OAUTH_REFRESH_TOKEN` is set,
with no code change. An API error is never read as "the video is gone".

Reactions: a post that is gone or restricted is a problem. Its creator is blocked, their other posts are
taken down (`enforce.py`), a `takedowns` row is written (source `platform` when the post is gone, `claim`
when it is restricted) and the strike rule is applied (`stop.py`). When most posts of a platform are gone
at once this is an account-level event: emergency stop, and no creator is blamed.
"""

import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from psycopg.types.json import Jsonb

from sixstars import db, events, notify
from sixstars.clients.upload_post import AccountAuthError, PostRejected, RateLimited, UploadPostClient
from sixstars.clients.youtube import YouTubeClient, YouTubeVideoStatus, parse_video_status
from sixstars.guardian import config as guardian_config
from sixstars.guardian import enforce, removal, stop
from sixstars.guardian.config import GuardianConfig
from sixstars.guardian.enforce import CreatorActions
from sixstars.guardian.removal import PostRef, Removal
from sixstars.logging import get_logger

SERVICE = "guardian"
LOCK_NAME = "sixstars.guardian.check"
MAX_PROBLEMS_IN_ANSWER = 50

Kind = Literal["removed_by_platform", "claimed", "blocked"]
Seen = Literal["ok", "gone", "restricted", "ambiguous", "unknown"]


@dataclass(frozen=True, slots=True)
class Observation:
    """What one source said about one post."""

    post: PostRef
    seen: Seen
    kind: Kind | None = None  # set when `seen` is gone or restricted
    detail: str = ""
    source: str = ""


@dataclass(frozen=True, slots=True)
class Finding:
    """A post with a problem the guardian must act on."""

    post: PostRef
    kind: Kind
    detail: str
    exists: bool  # still on the platform, so it has to be taken down

    def as_dict(self) -> dict[str, Any]:
        return {
            "post_id": self.post.id,
            "platform": self.post.platform,
            "account_id": self.post.account_id,
            "creator": self.post.creator_handle,
            "kind": self.kind,
            "detail": self.detail,
            "post_url": self.post.post_url,
        }


@dataclass(slots=True)
class CheckResult:
    status: str = "ok"  # ok | already_running
    checked: int = 0
    problems: list[dict[str, Any]] = field(default_factory=list)
    unverifiable: int = 0
    suspects: int = 0
    removed_by_owner: int = 0
    manual_removals_pending: int = 0
    errors: list[str] = field(default_factory=list)
    mass_event: list[str] = field(default_factory=list)
    emergency_stop: bool = False
    strikes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checked": self.checked,
            "problems": self.problems[:MAX_PROBLEMS_IN_ANSWER],
            "unverifiable": self.unverifiable,
            "suspects": self.suspects,
            "removed_by_owner": self.removed_by_owner,
            "manual_removals_pending": self.manual_removals_pending,
            "errors": self.errors,
            "mass_event": self.mass_event,
            "emergency_stop": self.emergency_stop,
            "strikes": self.strikes,
        }


# ── pure rules ───────────────────────────────────────────────


def youtube_observation(post: PostRef, status: YouTubeVideoStatus | None, source: str) -> Observation:
    """Judge one of our YouTube videos from its `videos.list` status (None: the API does not list it)."""
    if status is None:
        return Observation(post, "gone", "removed_by_platform", "the video no longer exists", source)
    upload = (status.upload_status or "").lower()
    if upload == "deleted":
        return Observation(post, "gone", "removed_by_platform", "uploadStatus is deleted", source)
    if upload == "rejected":
        reason = status.rejection_reason or "no reason given"
        return Observation(post, "restricted", "claimed", f"rejected by YouTube: {reason}", source)
    if status.region_blocked:
        countries = ", ".join(status.region_blocked[:8])
        return Observation(post, "restricted", "blocked", f"blocked in {countries}", source)
    if status.region_allowed is not None:
        return Observation(post, "restricted", "blocked", "viewable only in a list of countries", source)
    privacy = (status.privacy_status or "").lower()
    if post.is_public and privacy and privacy != "public":
        return Observation(post, "restricted", "blocked", f"privacy changed to {privacy}", source)
    return Observation(post, "ok", source=source)


def analytics_observation(post: PostRef, body: Mapping[str, Any]) -> Observation:
    """Judge a post from an Upload-Post post-analytics answer.

    Real answers (checked 2026-11-17): `{"success", "post", "platforms": {"<platform>": {"success",
    "platform_post_id", "post_metrics" | "post_metrics_error", ...}}}`. A missing post gives
    `post_metrics_error`: "TikTok video not found (ID: ...)", "YouTube video not found (ID: ...)", or for
    Instagram "Instagram API error (HTTP 400). The post may have been deleted or the token expired.",
    which does not say which of the two it is.
    """
    source = "upload_post"
    platforms = body.get("platforms")
    entry = platforms.get(post.platform) if isinstance(platforms, Mapping) else None
    if not isinstance(entry, Mapping):
        entry = body if "post_metrics" in body or "post_metrics_error" in body else None
    if entry is None:
        return Observation(post, "unknown", detail="no entry for the platform", source=source)
    metrics = entry.get("post_metrics")
    if isinstance(metrics, Mapping) and metrics:
        return Observation(post, "ok", source=source)
    error = entry.get("post_metrics_error") or entry.get("error")
    if not isinstance(error, str) or not error.strip():
        return Observation(post, "unknown", detail="no metrics and no error", source=source)
    text = error.strip()[:300]
    lowered = text.lower()
    if "not found" in lowered or "does not exist" in lowered or "has been removed" in lowered:
        return Observation(post, "gone", "removed_by_platform", text, source)
    if "deleted" in lowered or "removed" in lowered:
        return Observation(post, "ambiguous", detail=text, source=source)
    return Observation(post, "unknown", detail=text, source=source)


def judge(observations: Sequence[Observation]) -> tuple[list[Finding], list[Observation]]:
    """Turn the observations of one pass into findings and suspects.

    An ambiguous answer ("deleted or token expired") becomes a finding only when another post of the same
    platform read fine in this pass (so the token works) and the post was already a suspect on the pass
    before. Otherwise it is a suspect and nothing happens yet.
    """
    healthy = {item.post.platform for item in observations if item.seen == "ok"}
    findings: list[Finding] = []
    suspects: list[Observation] = []
    for item in observations:
        if item.seen == "gone" and item.kind is not None:
            findings.append(Finding(item.post, item.kind, item.detail, exists=False))
        elif item.seen == "restricted" and item.kind is not None:
            findings.append(Finding(item.post, item.kind, item.detail, exists=True))
        elif item.seen == "ambiguous":
            if item.post.platform in healthy and item.post.claim_status == "suspect":
                findings.append(Finding(item.post, "removed_by_platform", item.detail, exists=False))
            else:
                suspects.append(item)
    return findings, suspects


def mass_platforms(
    observations: Sequence[Observation], findings: Sequence[Finding], minimum: int, ratio: float
) -> list[str]:
    """Platforms where so many posts are gone in one pass that the account itself is in trouble (or the
    platform's answer is wrong): at least `minimum` posts gone and at least `ratio` of those read."""
    read: dict[str, int] = {}
    gone: dict[str, int] = {}
    for item in observations:
        if item.post.status == "published" and item.seen not in ("unknown",):
            read[item.post.platform] = read.get(item.post.platform, 0) + 1
    for finding in findings:
        if finding.post.status == "published" and not finding.exists:
            gone[finding.post.platform] = gone.get(finding.post.platform, 0) + 1
    return sorted(
        platform
        for platform, count in gone.items()
        if count >= max(1, minimum) and count >= ratio * read.get(platform, count)
    )


def in_check_window(published_at: datetime, now: datetime, lookback_h: float, min_age_s: float) -> bool:
    """A post is checked from `min_age_s` after publishing until it is `lookback_h` old."""
    age = now - published_at
    return timedelta(seconds=min_age_s) <= age <= timedelta(hours=lookback_h)


# ── reading the platforms ────────────────────────────────────


def posts_to_check(now: datetime, cfg: GuardianConfig) -> list[PostRef]:
    """Published posts inside the window (least recently checked first) and every post that waits for a
    manual removal."""
    rows = db.fetch_all(
        removal.POST_SQL
        + """
        where not p.dry_run and (
          p.status = 'remove_failed'
          or (p.status = 'published' and p.published_at <= %(newest)s and p.published_at >= %(oldest)s)
        )
        order by (p.status = 'remove_failed') desc, p.claim_checked_at nulls first, p.published_at
        limit %(limit)s
        """,
        {
            "newest": now - timedelta(seconds=cfg.check_min_age_s),
            "oldest": now - timedelta(hours=cfg.check_lookback_h),
            "limit": max(1, cfg.check_max_posts),
        },
    )
    return [removal.post_from_row(row) for row in rows]


def observe_youtube(
    posts: Sequence[PostRef], youtube: YouTubeClient, cfg: GuardianConfig, errors: list[str]
) -> tuple[list[Observation], list[PostRef]]:
    """Read our YouTube videos. Returns the observations and the posts this source could not judge
    (private videos without the OAuth token)."""
    log = get_logger(SERVICE)
    observations: list[Observation] = []
    with_id = [(post, removal.youtube_video_id(post)) for post in posts]
    todo = [(post, video_id) for post, video_id in with_id if video_id]
    observations.extend(
        Observation(post, "unknown", detail="no video id stored", source="none")
        for post, video_id in with_id
        if not video_id
    )
    if todo and youtube.has_oauth():
        try:
            # One OAuth videos.list per 50 videos instead of one per post.
            statuses = youtube.get_statuses([video_id for _, video_id in todo])
            for post, video_id in todo:
                observations.append(youtube_observation(post, statuses.get(video_id), "youtube_oauth"))
            return observations, []
        except Exception as exc:  # a revoked token must not stop the check: use the API key instead
            log.warning("youtube_oauth_check_failed", error=type(exc).__name__)
            errors.append(f"youtube oauth: {type(exc).__name__}")
            done = {item.post.id for item in observations}
            todo = [(post, video_id) for post, video_id in todo if post.id not in done]
    public = [(post, video_id) for post, video_id in todo if post.is_public]
    private = [post for post, _ in todo if not post.is_public]
    if public:
        if not youtube.is_configured():
            return observations, [*private, *(post for post, _ in public)]
        try:
            items = youtube.videos_list([video_id for _, video_id in public], ("status", "contentDetails"))
        except Exception as exc:
            log.error("youtube_check_failed", error=type(exc).__name__)
            errors.append(f"youtube api: {type(exc).__name__}")
            return observations, private
        by_id = {str(item.get("id", "")): parse_video_status(item) for item in items}
        for post, video_id in public:
            observations.append(youtube_observation(post, by_id.get(video_id), "youtube_api_key"))
    return observations, private


def _analytics(post: PostRef, upload_post: UploadPostClient) -> Mapping[str, Any] | None:
    """Post analytics by request id, else by the platform's own id; None when neither is known to
    Upload-Post."""
    if post.upload_request_id:
        try:
            return upload_post.get_post_analytics(post.upload_request_id, post.platform)
        except PostRejected:
            pass  # HTTP 404 "Post not found.": Upload-Post does not know this request id
    if post.platform_post_id:
        try:
            return upload_post.get_post_analytics_by_platform_id(
                post.upload_post_user, post.platform, post.platform_post_id
            )
        except PostRejected:
            return None
    return None


def observe_upload_post(
    posts: Sequence[PostRef],
    upload_post: UploadPostClient,
    errors: list[str],
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
) -> list[Observation]:
    """Read posts through Upload-Post until the time budget is used, a rate limit is hit or the account
    is not authorised. Posts that were not reached are simply not observed in this pass."""
    log = get_logger(SERVICE)
    observations: list[Observation] = []
    for post in posts:
        if clock() >= deadline:
            errors.append("time budget used; the remaining posts wait for the next pass")
            break
        try:
            body = _analytics(post, upload_post)
        except RateLimited:
            errors.append("upload-post: rate limited")
            break
        except AccountAuthError:
            errors.append("upload-post: not authorised")
            break
        except Exception as exc:
            log.warning("post_check_failed", post_id=post.id, error=type(exc).__name__)
            observations.append(Observation(post, "unknown", detail=type(exc).__name__, source="upload_post"))
            continue
        if body is None:
            observations.append(
                Observation(post, "unknown", detail="unknown to Upload-Post", source="upload_post")
            )
        else:
            observations.append(analytics_observation(post, body))
    return observations


def observe(
    posts: Sequence[PostRef],
    upload_post: UploadPostClient,
    youtube: YouTubeClient,
    cfg: GuardianConfig,
    errors: list[str],
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[Observation], int]:
    """All observations of one pass, and how many posts no source could judge."""
    deadline = clock() + cfg.check_budget_s
    youtube_posts = [post for post in posts if post.platform == "youtube"]
    others = [post for post in posts if post.platform != "youtube"]
    observations, leftover = observe_youtube(youtube_posts, youtube, cfg, errors)
    unverifiable = 0
    via_upload_post: list[PostRef] = []
    for post in [*leftover, *others]:
        if post.is_public or cfg.check_private_posts or post.status == "remove_failed":
            via_upload_post.append(post)
        else:
            unverifiable += 1
    if via_upload_post and not upload_post.is_configured():
        errors.append("upload-post: no API key")
        unverifiable += len(via_upload_post)
        via_upload_post = []
    observations.extend(observe_upload_post(via_upload_post, upload_post, errors, deadline, clock))
    return observations, unverifiable


# ── acting ───────────────────────────────────────────────────


def _mark_checked(observations: Sequence[Observation], suspects: Sequence[Observation]) -> None:
    suspect_ids = [item.post.id for item in suspects if item.post.status == "published"]
    ok_ids = [item.post.id for item in observations if item.seen == "ok" and item.post.status == "published"]
    seen_ids = [item.post.id for item in observations]
    if seen_ids:
        db.execute("update posts set claim_checked_at = now() where id = any(%s::uuid[])", (seen_ids,))
    if ok_ids:
        db.execute(
            "update posts set claim_status = 'ok' where id = any(%s::uuid[]) and status = 'published'",
            (ok_ids,),
        )
    if suspect_ids:
        db.execute(
            "update posts set claim_status = 'suspect' where id = any(%s::uuid[]) and status = 'published'",
            (suspect_ids,),
        )


def _record_finding(finding: Finding) -> None:
    """Write the finding to the post. A post that is gone needs no removal."""
    post = finding.post
    db.execute("update posts set claim_status = %s where id = %s", (finding.kind, post.id))
    events.emit(
        "post.claimed",
        level="critical",
        entity_type="post",
        entity_id=post.id,
        message=f"{post.platform} post problem ({finding.kind}): {finding.detail}"[:500],
        data={**finding.as_dict(), "clip_id": post.clip_id, "exists": finding.exists},
        service=SERVICE,
    )
    if not finding.exists:
        removal.mark_removed(post, f"{finding.kind}: {finding.detail}", "platform")


def _insert_incident(source: str, creator_id: str | None, findings: Sequence[Finding], subject: str) -> str:
    row = db.fetch_one(
        """
        insert into takedowns
          (source, subject, body_excerpt, is_takedown, creator_id, matched_post_ids, status)
        values (%s, %s, %s, true, %s, %s::uuid[], 'open')
        returning id::text as id
        """,
        (
            source,
            subject[:300],
            "; ".join(f"{item.post.platform}: {item.detail}" for item in findings)[:1000],
            creator_id,
            [item.post.id for item in findings],
        ),
    )
    assert row is not None
    return str(row["id"])


def _close_incident(incident_id: str, status: str, actions: Mapping[str, Any]) -> None:
    db.execute(
        "update takedowns set status = %s, actions = %s where id = %s",
        (status, Jsonb(dict(actions)), incident_id),
    )


def act_on_creator_findings(
    findings: Sequence[Finding], upload_post: UploadPostClient, youtube: YouTubeClient
) -> CreatorActions | None:
    """One incident: the findings of one creator. Block, remove, record, alert."""
    first = findings[0].post
    gone = any(not item.exists for item in findings)
    source = "platform" if gone else "claim"
    kinds = ", ".join(sorted({item.kind for item in findings}))
    subject = f"claims check: {len(findings)} post(s) of {first.creator_handle} ({kinds})"
    incident_id = _insert_incident(source, first.creator_id, findings, subject)
    for finding in findings:
        _record_finding(finding)
    reason = f"claim or removal on our post ({kinds})"
    actions = enforce.act_on_creator(first.creator_id, reason, upload_post, youtube, incident_id=incident_id)
    removals: list[Removal] = actions.removals if actions else []
    manual = [item for item in removals if not item.done]
    _close_incident(
        incident_id,
        "partial" if manual else "actioned",
        {
            "strike": True,
            "findings": [item.as_dict() for item in findings],
            "items": actions.as_actions() if actions else [],
        },
    )
    lines = [f"{item.post.platform} {item.post.post_url or item.post.id}: {item.detail}" for item in findings]
    body = "\n".join(
        [
            *lines[:10],
            *(actions.summary_lines() if actions else []),
            "This counts as one strike.",
        ]
    )
    notify.send("critical", f"Claim or removal: {first.creator_handle}", body)
    for title, text in removal.manual_removal_messages(manual, f"claim or removal on {first.creator_handle}"):
        notify.send("critical", title, text)
    return actions


def _resolve_owner_removals(observations: Sequence[Observation]) -> int:
    """A post that waited for a manual removal and is now gone: the owner deleted it."""
    done = 0
    healthy = {item.post.platform for item in observations if item.seen == "ok"}
    for item in observations:
        if item.post.status != "remove_failed":
            continue
        # Instagram never says "not found"; its ambiguous answer counts while its token demonstrably works.
        if item.seen == "gone" or (item.seen == "ambiguous" and item.post.platform in healthy):
            done += int(removal.mark_removed(item.post, "removed by hand", "manual"))
            enforce.settle_clips(item.post.creator_id)
    return done


def _pending_manual() -> int:
    row = db.fetch_one("select count(*) as n from posts where status = 'remove_failed' and not dry_run")
    return int(row["n"]) if row else 0


@contextmanager
def single_pass(name: str) -> Iterator[bool]:
    """Hold a session advisory lock for the block; yields False when another pass holds it."""
    with db.pool().connection() as conn:
        row = conn.execute("select pg_try_advisory_lock(hashtextextended(%s::text, 0))", (name,)).fetchone()
        locked = bool(row and row[0])
        try:
            yield locked
        finally:
            if locked:
                conn.execute("select pg_advisory_unlock(hashtextextended(%s::text, 0))", (name,))


def run_check(
    upload_post: UploadPostClient,
    youtube: YouTubeClient,
    now: datetime,
    clock: Callable[[], float] = time.monotonic,
) -> CheckResult:
    """One pass of the claims and removals check."""
    with single_pass(LOCK_NAME) as locked:
        if not locked:
            return CheckResult(status="already_running")
        return _run_check(upload_post, youtube, now, clock)


def _run_check(
    upload_post: UploadPostClient, youtube: YouTubeClient, now: datetime, clock: Callable[[], float]
) -> CheckResult:
    log = get_logger(SERVICE)
    cfg = guardian_config.load()
    result = CheckResult()
    posts = posts_to_check(now, cfg)
    observations, result.unverifiable = observe(posts, upload_post, youtube, cfg, result.errors, clock)
    result.checked = sum(1 for item in observations if item.seen != "unknown")
    result.removed_by_owner = _resolve_owner_removals(observations)
    live = [item for item in observations if item.post.status == "published"]
    findings, suspects = judge(live)
    result.suspects = len(suspects)
    _mark_checked(observations, suspects)
    result.problems = [finding.as_dict() for finding in findings]

    mass = mass_platforms(live, findings, cfg.mass_missing_min, cfg.mass_missing_ratio)
    if mass:
        result.mass_event = mass
        affected = [finding for finding in findings if finding.post.platform in mass]
        findings = [finding for finding in findings if finding.post.platform not in mass]
        for finding in affected:
            _record_finding(finding)
        incident_id = _insert_incident(
            "platform",
            None,
            affected,
            f"account-level removal on {', '.join(mass)}: {len(affected)} posts gone",
        )
        _close_incident(
            incident_id,
            "actioned",
            {"strike": True, "mass": mass, "findings": [item.as_dict() for item in affected]},
        )
        stop.trigger(
            f"{len(affected)} posts vanished from {', '.join(mass)} in one check: the account may be "
            "banned or restricted. No creator was blocked for it.",
            {"rule": "mass_removal", "platforms": mass, "posts": [item.post.id for item in affected]},
        )

    by_creator: dict[str, list[Finding]] = {}
    for finding in findings:
        by_creator.setdefault(finding.post.creator_id, []).append(finding)
    for creator_findings in by_creator.values():
        act_on_creator_findings(creator_findings, upload_post, youtube)

    swept = [item for item in enforce.sweep_blocked(upload_post, youtube) if not item.done]
    for title, text in removal.manual_removal_messages(swept, "a post of a blocked creator went public"):
        notify.send("critical", title, text)

    decision = stop.evaluate(now, cfg.strike_limit, cfg.strike_window_d)
    result.strikes = decision.strikes
    result.emergency_stop = stop.is_stopped()
    result.manual_removals_pending = _pending_manual()
    log.info(
        "guardian_check_done",
        checked=result.checked,
        problems=len(result.problems),
        unverifiable=result.unverifiable,
        suspects=result.suspects,
        errors=len(result.errors),
        emergency_stop=result.emergency_stop,
    )
    return result
