"""Takedown emails (docs/12 section 2): `POST /v1/guardian/takedown`, called by n8n for every mail that
arrives in the takedown inbox.

The inbox is a real mailbox: anyone can write to it, so every field is untrusted text.

- The email only ever selects things by matching what we hold: links to posts we published, links to or
  ids of the source videos and channels we clipped, and creator names in our `creators` table. Nothing in
  the email is fetched, followed or executed, and no id from the email is passed to a platform.
- Whether it is a removal request is decided by prompt P7 (which treats the email as data). A request
  that matches nothing of ours changes nothing and goes to the owner.
- One email is processed once (`message_id`); sending it again returns the stored result.

Balance when unsure (P7 and docs/12: missing a real request is worse than a false alarm): an email that
P7 calls a takedown and that matches a creator of ours is acted on in full, even if a human might have
doubted it. An email that P7 calls a takedown but that matches nothing is never acted on automatically; it
is stored as `open` and the owner is alerted. An email that could not be classified (AI down, or more
than `guardian.takedown_max_per_hour` emails) is stored as `open` and alerted, and is classified when n8n
sends it again.
"""

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from psycopg.types.json import Jsonb

from sixstars import db, events, notify
from sixstars.ai.schemas import TakedownVerdict
from sixstars.clients.upload_post import UploadPostClient
from sixstars.clients.youtube import YouTubeClient
from sixstars.guardian import config as guardian_config
from sixstars.guardian import enforce, removal, stop
from sixstars.guardian.config import GuardianConfig
from sixstars.guardian.enforce import CreatorActions
from sixstars.logging import get_logger

SERVICE = "guardian"
MAX_SENDER_CHARS = 320
MAX_SUBJECT_CHARS = 300
MAX_EXCERPT_CHARS = 1000
MAX_MESSAGE_ID_CHARS = 512
MAX_URLS = 200
MAX_HANDLES = 20
MAX_URL_CHARS = 2048

Classifier = Callable[[str, str, str], TakedownVerdict]

_URL_RE = re.compile(r"https?://[^\s<>\"'\]\[)(}{|\\^`]+", re.IGNORECASE)
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{5,64}$")
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_.-]{2,64}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_YOUTUBE_HOSTS = frozenset({"youtube.com", "youtube-nocookie.com", "music.youtube.com"})
_YOUTUBE_PATH_VIDEO = frozenset({"shorts", "embed", "live", "v"})
_YOUTUBE_RESERVED = frozenset(
    {
        "watch",
        "playlist",
        "results",
        "feed",
        "channel",
        "c",
        "user",
        "shorts",
        "embed",
        "live",
        "v",
        "t",
        "hashtag",
    }
)
_TWITCH_RESERVED = frozenset({"videos", "directory", "p", "settings", "subscriptions", "downloads", "jobs"})


@dataclass(frozen=True, slots=True)
class TakedownEmail:
    """The email as n8n sent it, cleaned and cut to size."""

    sender: str
    subject: str
    body: str
    message_id: str
    claimed_received_at: str | None = None  # what the email says; kept for the record, never trusted
    truncated: bool = False
    links: tuple[str, ...] = ()  # links found in the subject and the whole body, also beyond the cut


@dataclass(frozen=True, slots=True)
class Keys:
    """What an email points at, reduced to ids and names that can be compared with our data."""

    video_ids: frozenset[tuple[str, str]] = frozenset()  # (platform, id): youtube, tiktok, instagram, twitch
    channel_ids: frozenset[str] = frozenset()  # YouTube UC... ids
    handles: frozenset[str] = frozenset()  # normalised names from channel links


@dataclass(slots=True)
class Match:
    creator_ids: list[str] = field(default_factory=list)
    post_ids: list[str] = field(default_factory=list)  # our posts the email links to
    how: list[str] = field(default_factory=list)


# ── pure helpers ─────────────────────────────────────────────


def clean_text(value: Any, limit: int) -> str:
    """A string field of the email without control characters, cut to `limit` characters."""
    if not isinstance(value, str):
        return ""
    return _CONTROL_RE.sub(" ", value)[:limit].strip()


def one_line(value: str) -> str:
    return " ".join(value.split())


def fallback_message_id(sender: str, subject: str, body: str) -> str:
    """A stable id for an email that arrived without one, so that it is still processed once."""
    digest = hashlib.sha256("\x1f".join((sender, subject, body)).encode("utf-8", "replace")).hexdigest()
    return f"sha256:{digest}"


def build_email(
    sender: Any, subject: Any, body: Any, message_id: Any, received_at: Any, body_max_chars: int
) -> TakedownEmail:
    """Normalise the request fields. Oversized text is cut, never rejected: a refused request would lose
    the email, because the Gmail trigger does not deliver it twice."""
    raw_body = body if isinstance(body, str) else ""
    clean_body = clean_text(raw_body, max(1, body_max_chars))
    clean_sender = one_line(clean_text(sender, MAX_SENDER_CHARS))
    clean_subject = one_line(clean_text(subject, MAX_SUBJECT_CHARS))
    given_id = one_line(clean_text(message_id, MAX_MESSAGE_ID_CHARS + 1))
    if not given_id or len(given_id) > MAX_MESSAGE_ID_CHARS:
        given_id = fallback_message_id(clean_sender, clean_subject, clean_body)
    claimed = one_line(clean_text(received_at, 64)) or None
    return TakedownEmail(
        sender=clean_sender,
        subject=clean_subject,
        body=clean_body,
        message_id=given_id,
        claimed_received_at=claimed,
        truncated=len(raw_body) > max(1, body_max_chars),
        links=tuple(extract_urls(clean_subject + " " + _CONTROL_RE.sub(" ", raw_body))),
    )


def extract_urls(text: str, limit: int = MAX_URLS) -> list[str]:
    """The http(s) links in a text, in order, without trailing punctuation. Nothing is fetched."""
    urls: list[str] = []
    for found in _URL_RE.finditer(text):
        url = found.group(0).rstrip(".,;:!?*>")
        if url and len(url) <= MAX_URL_CHARS and url not in urls:
            urls.append(url)
            if len(urls) >= limit:
                break
    return urls


def normalise_handle(value: str) -> str:
    """Lower case, letters and digits only: `@Some_Streamer` and `some streamer` compare equal."""
    return "".join(char for char in value.lower() if char.isalnum())


def _host(url: str) -> tuple[str, list[str], dict[str, list[str]]] | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    for prefix in ("www.", "m.", "mobile."):
        host = host.removeprefix(prefix)
    segments = [segment for segment in parts.path.split("/") if segment]
    return host, segments, parse_qs(parts.query)


def url_keys(url: str) -> list[tuple[str, str]]:
    """What a link points at, as `(kind, value)` pairs: `video:<platform>` with the platform's id,
    `channel` with a YouTube channel id, or `handle` with a normalised name. An unknown site, a short
    link or anything malformed gives nothing (short links are never resolved)."""
    parsed = _host(url)
    if parsed is None:
        return []
    host, segments, query = parsed
    keys: list[tuple[str, str]] = []

    def video(platform: str, value: str | None) -> None:
        if value and _ID_RE.match(value):
            keys.append((f"video:{platform}", value))

    def handle(value: str) -> None:
        name = value.lstrip("@")
        if _HANDLE_RE.match(name) and normalise_handle(name):
            keys.append(("handle", normalise_handle(name)))

    if host == "youtu.be" and segments:
        video("youtube", segments[0])
    elif host in _YOUTUBE_HOSTS:
        if segments and segments[0] == "watch":
            video("youtube", next(iter(query.get("v") or []), None))
        elif len(segments) >= 2 and segments[0] in _YOUTUBE_PATH_VIDEO:
            video("youtube", segments[1])
        elif len(segments) >= 2 and segments[0] == "channel":
            if _ID_RE.match(segments[1]):
                keys.append(("channel", segments[1]))
        elif len(segments) >= 2 and segments[0] in ("c", "user"):
            handle(segments[1])
        elif segments and segments[0].startswith("@"):
            handle(segments[0])
            if len(segments) >= 3 and segments[1] in _YOUTUBE_PATH_VIDEO:
                video("youtube", segments[2])
        elif len(segments) == 1 and segments[0] not in _YOUTUBE_RESERVED:
            handle(segments[0])
    elif host == "tiktok.com":
        if len(segments) >= 3 and segments[0].startswith("@") and segments[1] in ("video", "photo"):
            video("tiktok", segments[2])
            handle(segments[0])
        elif segments and segments[0].startswith("@"):
            handle(segments[0])
    elif host == "instagram.com":
        for marker in ("reel", "reels", "p", "tv"):
            if marker in segments[:-1]:
                video("instagram", segments[segments.index(marker) + 1])
                break
        else:
            if len(segments) == 1:
                handle(segments[0])
    elif host in ("twitch.tv", "clips.twitch.tv"):
        if len(segments) >= 2 and segments[0] == "videos":
            video("twitch", segments[1])
        elif segments and segments[0] not in _TWITCH_RESERVED and host == "twitch.tv":
            handle(segments[0])
            if len(segments) >= 3 and segments[1] in ("v", "video"):
                video("twitch", segments[2])
    return keys


def collect_keys(urls: Sequence[str]) -> Keys:
    videos: set[tuple[str, str]] = set()
    channels: set[str] = set()
    handles: set[str] = set()
    for url in urls:
        for kind, value in url_keys(url):
            if kind == "channel":
                channels.add(value)
            elif kind == "handle":
                handles.add(value)
            else:
                videos.add((kind.removeprefix("video:"), value))
    return Keys(frozenset(videos), frozenset(channels), frozenset(handles))


def post_key(platform: str, platform_post_id: str | None, post_url: str | None) -> set[tuple[str, str]]:
    """The ids under which a link in an email can name one of our posts."""
    keys: set[tuple[str, str]] = set()
    if platform_post_id:
        keys.add((platform, platform_post_id))
    if post_url:
        for kind, value in url_keys(post_url):
            if kind.startswith("video:"):
                keys.add((kind.removeprefix("video:"), value))
    return keys


def match_rows(
    keys: Keys,
    names: Sequence[str],
    posts: Sequence[Mapping[str, Any]],
    sources: Sequence[Mapping[str, Any]],
    creators: Sequence[Mapping[str, Any]],
    own_handles: Sequence[str],
    min_handle_chars: int,
) -> Match:
    """Pure matching of an email against our rows.

    - `posts`: our real posts (`id`, `platform`, `platform_post_id`, `post_url`, `creator_id`): exact id.
    - `sources`: the videos and streams we clipped (`platform`, `source_id`, `creator_id`): exact id.
    - `creators`: `id`, `platform`, `channel_id`, `handle`, `display_name`: exact channel id, or the same
      name after `normalise_handle` (so case, "@", spaces and punctuation do not matter).
    Names shorter than `min_handle_chars`, and our own account names, never match.
    """
    match = Match()
    creator_ids: dict[str, None] = {}

    def add(creator_id: Any, how: str) -> None:
        key = str(creator_id)
        if key not in creator_ids:
            creator_ids[key] = None
            match.how.append(how)

    for post in posts:
        if (
            post_key(str(post["platform"]), post.get("platform_post_id"), post.get("post_url"))
            & keys.video_ids
        ):
            match.post_ids.append(str(post["id"]))
            add(post["creator_id"], f"link to our {post['platform']} post")
    for source in sources:
        if (str(source["platform"]), str(source["source_id"])) in keys.video_ids:
            add(source["creator_id"], f"link to the {source['platform']} video we clipped")
    own = {normalise_handle(handle) for handle in own_handles}
    wanted = {normalise_handle(name) for name in names[:MAX_HANDLES]} | set(keys.handles)
    wanted = {name for name in wanted if len(name) >= max(1, min_handle_chars) and name not in own}
    for creator in creators:
        if str(creator["channel_id"]) in keys.channel_ids:
            add(creator["id"], "link to the creator's channel")
            continue
        candidates = {normalise_handle(str(creator["handle"] or ""))}
        if creator.get("display_name"):
            candidates.add(normalise_handle(str(creator["display_name"])))
        candidates.discard("")
        if candidates & wanted:
            add(creator["id"], f"creator name {creator['handle']}")
    match.creator_ids = list(creator_ids)
    return match


# ── database ─────────────────────────────────────────────────


def find_match(keys: Keys, names: Sequence[str], cfg: GuardianConfig) -> Match:
    """Match the email against everything we posted, clipped or know."""
    posts = db.fetch_all(
        """
        select p.id::text as id, p.platform, p.platform_post_id, p.post_url, c.creator_id::text as creator_id
        from posts p join clips c on c.id = p.clip_id
        where not p.dry_run and p.status in ('published', 'remove_failed', 'removed')
        """
    )
    source_ids = [value for _, value in keys.video_ids]
    sources = db.fetch_all(
        """
        select source_platform as platform, source_id, creator_id::text as creator_id
        from clips where source_id = any(%(ids)s)
        union
        select platform, source_id, creator_id::text as creator_id from videos where source_id = any(%(ids)s)
        """,
        {"ids": source_ids},
    )
    creators = db.fetch_all("select id::text as id, platform, channel_id, handle, display_name from creators")
    own = [str(row["handle"]) for row in db.fetch_all("select handle from platform_accounts")]
    return match_rows(keys, names, posts, sources, creators, own, cfg.handle_match_min_chars)


def existing(message_id: str) -> dict[str, Any] | None:
    return db.fetch_one(
        "select id::text as id, status, is_takedown, actions from takedowns where message_id = %s",
        (message_id,),
    )


def _claim_row(email: TakedownEmail) -> str | None:
    """Insert the row for this email. None when another request already holds the message id."""
    row = db.fetch_one(
        """
        insert into takedowns (source, sender, subject, body_excerpt, message_id, status, actions)
        values ('email', %s, %s, %s, %s, 'open', %s)
        on conflict (message_id) where message_id is not null do nothing
        returning id::text as id
        """,
        (
            email.sender,
            email.subject,
            email.body[:MAX_EXCERPT_CHARS],
            email.message_id,
            Jsonb({"state": "processing", "claimed_received_at": email.claimed_received_at}),
        ),
    )
    return None if row is None else str(row["id"])


def _finish(
    takedown_id: str,
    status: str,
    is_takedown: bool | None,
    actions: Mapping[str, Any],
    creator_id: str | None = None,
    post_ids: Sequence[str] = (),
) -> None:
    db.execute(
        """
        update takedowns
        set status = %s, is_takedown = %s, actions = %s, creator_id = %s, matched_post_ids = %s::uuid[]
        where id = %s
        """,
        (status, is_takedown, Jsonb(dict(actions)), creator_id, list(post_ids), takedown_id),
    )


def _classified_last_hour() -> int:
    row = db.fetch_one(
        """
        select count(*) as n from takedowns
        where source = 'email' and is_takedown is not null and received_at > now() - interval '1 hour'
        """
    )
    return int(row["n"]) if row else 0


def _answer(
    takedown_id: str, status: str, is_takedown: bool | None, actions: Mapping[str, Any], duplicate: bool
) -> dict[str, Any]:
    return {
        "takedown_id": takedown_id,
        "status": status,
        "is_takedown": is_takedown,
        "duplicate": duplicate,
        "matched": actions.get("matched", {"creators": [], "posts": 0}),
        "actions": actions.get("items", []),
        "strike": bool(actions.get("strike", False)),
        "emergency_stop": stop.is_stopped(),
    }


# ── the flow ─────────────────────────────────────────────────


def handle_email(
    email: TakedownEmail,
    classify: Classifier,
    upload_post: UploadPostClient,
    youtube: YouTubeClient,
    now: datetime,
) -> dict[str, Any]:
    """Process one email from the takedown inbox. Returns the JSON answer of the route.

    When something fails halfway, the row is left open and unclassified, so the retry that n8n sends is
    processed again from the start instead of being answered as a duplicate. Every step is safe to
    repeat: a removed post is not removed twice and a creator's claim counter moves once per email.
    """
    claimed: list[str] = []
    try:
        return _handle(email, classify, upload_post, youtube, now, claimed)
    except Exception as exc:
        if claimed:
            why = f"processing failed: {type(exc).__name__}"
            db.execute(
                "update takedowns set status = 'open', is_takedown = null, actions = %s where id = %s",
                (Jsonb({"state": "unclassified", "why": why}), claimed[0]),
            )
        raise


def _handle(
    email: TakedownEmail,
    classify: Classifier,
    upload_post: UploadPostClient,
    youtube: YouTubeClient,
    now: datetime,
    claimed: list[str],
) -> dict[str, Any]:
    log = get_logger(SERVICE)
    cfg = guardian_config.load()
    takedown_id = _claim_row(email)
    if takedown_id is None:
        row = existing(email.message_id)
        assert row is not None
        stored = row["actions"] if isinstance(row["actions"], dict) else {}
        if not (
            row["status"] == "open" and row["is_takedown"] is None and stored.get("state") == "unclassified"
        ):
            log.info("takedown_duplicate", takedown_id=row["id"])
            return _answer(row["id"], row["status"], row["is_takedown"], stored, duplicate=True)
        takedown_id = str(row["id"])  # stored without a verdict earlier: classify it now
    claimed.append(takedown_id)

    base: dict[str, Any] = {"claimed_received_at": email.claimed_received_at, "truncated": email.truncated}
    if _classified_last_hour() >= cfg.takedown_max_per_hour:
        actions = {**base, "state": "unclassified", "why": "hourly limit of classified emails reached"}
        _finish(takedown_id, "open", None, actions)
        notify.send(
            "warn",
            "Takedown inbox is flooded",
            f"More than {cfg.takedown_max_per_hour} emails were classified in the last hour. New emails are "
            "stored unread (status open) until the flood ends. Please look at the inbox.",
        )
        return _answer(takedown_id, "open", None, actions, duplicate=False)

    try:
        verdict = classify(email.sender, email.subject, email.body)
    except Exception as exc:
        log.error("takedown_classify_failed", takedown_id=takedown_id, error=type(exc).__name__)
        actions = {**base, "state": "unclassified", "why": f"classification failed: {type(exc).__name__}"}
        _finish(takedown_id, "open", None, actions)
        events.emit(
            "takedown.unclassified",
            level="error",
            entity_type="takedown",
            entity_id=takedown_id,
            message="takedown email could not be classified",
            data={"error": type(exc).__name__},
            service=SERVICE,
        )
        notify.send(
            "critical",
            "Takedown email needs you: it could not be read automatically",
            f"From: {email.sender or '(unknown)'}\nSubject: {email.subject or '(none)'}\n"
            "The AI classification failed, so nothing was done. Please read this email in the takedown "
            "inbox. It is classified automatically if n8n sends it again.",
        )
        return _answer(takedown_id, "open", None, actions, duplicate=False)

    summary = one_line(verdict.summary)[:400]
    if not verdict.is_takedown:
        actions = {**base, "state": "done", "kind": verdict.kind, "summary": summary}
        _finish(takedown_id, "ignored", False, actions)
        events.emit(
            "takedown.ignored",
            entity_type="takedown",
            entity_id=takedown_id,
            message="email is not a takedown request",
            data={"kind": verdict.kind},
            service=SERVICE,
        )
        return _answer(takedown_id, "ignored", False, actions, duplicate=False)

    urls = list(email.links)
    for url in verdict.urls:  # links the model copied out of the email; only ever compared, never opened
        if url.lower().startswith(("http://", "https://")) and len(url) <= MAX_URL_CHARS and url not in urls:
            urls.append(url)
    match = find_match(collect_keys(urls[:MAX_URLS]), verdict.creator_handles, cfg)
    base = {**base, "kind": verdict.kind, "summary": summary}

    if not match.creator_ids:
        actions = {**base, "state": "done", "matched": {"creators": [], "posts": 0}, "strike": False}
        _finish(takedown_id, "open", True, actions)
        events.emit(
            "takedown.unmatched",
            level="warn",
            entity_type="takedown",
            entity_id=takedown_id,
            message="takedown request matches no post or creator of ours",
            data={"kind": verdict.kind, "urls": len(urls), "names": len(verdict.creator_handles)},
            service=SERVICE,
        )
        notify.send(
            "critical",
            "Takedown email needs you: nothing of ours matched",
            f"From: {email.sender or '(unknown)'}\nSubject: {email.subject or '(none)'}\n"
            f"Summary: {summary or '(none)'}\n"
            "It reads like a removal request, but no post, source video or creator of ours is named in "
            "it, so nothing was removed and nobody was blocked. Please read it in the takedown inbox.",
        )
        return _answer(takedown_id, "open", True, actions, duplicate=False)

    reason = f"takedown request ({verdict.kind}) by email"
    done: list[CreatorActions] = []
    for creator_id in match.creator_ids:
        acted = enforce.act_on_creator(creator_id, reason, upload_post, youtube, incident_id=takedown_id)
        if acted is not None:
            done.append(acted)
    removals = [item for acted in done for item in acted.removals]
    manual = [item for item in removals if not item.done]
    # A strike is a takedown that hit something we had really published.
    strike = bool(removals) or bool(match.post_ids)
    actions = {
        **base,
        "state": "done",
        "strike": strike,
        "matched": {
            "creators": [acted.handle for acted in done],
            "posts": len(match.post_ids),
            "how": match.how,
        },
        "items": [item for acted in done for item in acted.as_actions()],
    }
    status = "partial" if manual else "actioned"
    _finish(takedown_id, status, True, actions, match.creator_ids[0], match.post_ids)
    events.emit(
        "takedown.actioned",
        level="critical",
        entity_type="takedown",
        entity_id=takedown_id,
        message=f"takedown request actioned for {len(done)} creator(s)",
        data={"kind": verdict.kind, "status": status, "strike": strike, "creators": match.creator_ids},
        service=SERVICE,
    )
    lines = [
        f"From: {email.sender or '(unknown)'}",
        f"Subject: {email.subject or '(none)'}",
        f"Summary: {summary or '(none)'}",
        f"Matched by: {'; '.join(match.how[:5])}",
    ]
    for acted in done:
        lines.extend(acted.summary_lines())
    lines.append("This counts as one strike." if strike else "No strike: nothing of theirs was public.")
    notify.send(
        "critical", f"Takedown actioned: {', '.join(acted.handle for acted in done)[:80]}", "\n".join(lines)
    )
    for title, text in removal.manual_removal_messages(manual, f"takedown request from {email.sender}"):
        notify.send("critical", title, text)
    stop.evaluate(now, cfg.strike_limit, cfg.strike_window_d)
    return _answer(takedown_id, status, True, actions, duplicate=False)
