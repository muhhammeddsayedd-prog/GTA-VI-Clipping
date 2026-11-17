"""YouTube live chat over the same web endpoints the youtube.com player uses, shared by the live and replay
adapters. Added in M03 because `chat-downloader` 0.2.8 no longer works against YouTube (see
`development-plan/progress-notes/M03.md`).

How it works (verified 2026-11-17): the watch page embeds `ytInitialData`, whose
`conversationBar.liveChatRenderer` holds a continuation token; `POST /youtubei/v1/live_chat/get_live_chat`
(live) or `get_live_chat_replay` (finished streams) with that token returns a batch of chat actions and the
next token. The first response also lists the "Top chat" and "Live chat" views; we switch to "Live chat",
which shows every message. No API key quota and no login are involved.

The parsing functions are pure and unit-tested on recorded data; the two `fetch_*`/`post_*` coroutines
do the HTTP calls with timeouts.
"""

import html as html_lib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any, Final

import httpx

from sixstars.chat.models import ChatMessage, ChatSourceError, Kind

WATCH_URL: Final = "https://www.youtube.com/watch"
CHAT_API_URL: Final = "https://www.youtube.com/youtubei/v1/live_chat/{endpoint}"
LIVE_ENDPOINT: Final = "get_live_chat"
REPLAY_ENDPOINT: Final = "get_live_chat_replay"
# YouTube serves a page without chat data to browsers it considers too old, so this must look current.
USER_AGENT: Final = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
# Request constant (not a runtime setting: `docs/16-settings-reference.md` has no key for it).
HTTP_TIMEOUT_S: Final = 15.0
# Marks the EU cookie-consent choice as made, so EU servers get the watch page and not the consent wall.
CONSENT_COOKIE: Final = ("SOCS", "CAI")

RENDERER_KINDS: Final[dict[str, Kind]] = {
    "liveChatTextMessageRenderer": "chat",
    "liveChatPaidMessageRenderer": "donation",
    "liveChatPaidStickerRenderer": "donation",
    "liveChatMembershipItemRenderer": "sub",
    "liveChatSponsorshipsGiftPurchaseAnnouncementRenderer": "gift",
    "liveChatSponsorshipsGiftRedemptionAnnouncementRenderer": "gift",
    "liveChatViewerEngagementMessageRenderer": "system",
    "liveChatModeChangeMessageRenderer": "system",
}
# Items that are not messages (a placeholder is a message still being moderated).
SKIPPED_RENDERERS: Final = frozenset({"liveChatPlaceholderItemRenderer"})
SYSTEM_USER: Final = "youtube"

_VIDEO_ID_RE: Final = re.compile(r"^[A-Za-z0-9_-]{11}$")
_URL_ID_RE: Final = re.compile(
    r"(?:[?&]v=|youtu\.be/|/live/|/shorts/|/embed/)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])"
)
_CLIENT_VERSION_RE: Final = re.compile(r'"INNERTUBE_CLIENT_VERSION"\s*:\s*"([^"]+)"')
_API_KEY_RE: Final = re.compile(r'"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"')
_VISITOR_DATA_RE: Final = re.compile(r'"VISITOR_DATA"\s*:\s*"([^"]+)"')


@dataclass(frozen=True, slots=True)
class WatchPage:
    """What the chat adapters need from a watch page.

    `continuation` is None when the page has no chat (a normal upload, chat disabled, or no replay);
    `chat_notice` then holds YouTube's explanation when it gives one. `unplayable_reason` is set when the
    video itself cannot be played (private, removed, sign-in required).
    """

    video_id: str
    client_version: str
    api_key: str | None
    visitor_data: str | None
    continuation: str | None
    is_replay: bool
    is_live: bool
    duration_s: float | None
    title: str | None
    chat_notice: str | None
    unplayable_reason: str | None


@dataclass(frozen=True, slots=True)
class ChatPage:
    """One chat API response: raw actions, the next continuation (None at the end of the chat), the
    server's suggested wait before the next poll, and the token of the "Live chat" (all messages) view
    when the response came from another view."""

    actions: list[dict[str, Any]]
    continuation: str | None
    timeout_ms: int | None
    all_chat_continuation: str | None


def extract_video_id(video: str) -> str:
    """Return the 11-character video id from an id or a YouTube URL. Raises `ValueError` if there is none."""
    cleaned = video.strip()
    if _VIDEO_ID_RE.match(cleaned):
        return cleaned
    match = _URL_ID_RE.search(cleaned)
    if match is None:
        raise ValueError(f"not a YouTube video id or URL: {video!r}")
    return match.group(1)


def is_youtube_reference(value: str) -> bool:
    """True when `value` is a YouTube URL (used by the CLI to tell a URL from a Twitch login or VOD id)."""
    return "youtube.com/" in value or "youtu.be/" in value


def _dig(node: Any, *path: str | int) -> Any:
    """Follow `path` through nested dicts and lists; None as soon as a step is missing."""
    for step in path:
        if isinstance(step, int):
            if not isinstance(node, list) or not -len(node) <= step < len(node):
                return None
            node = node[step]
        else:
            if not isinstance(node, dict):
                return None
            node = node.get(step)
    return node


def _embedded_json(page: str, name: str, script_id: str) -> dict[str, Any] | None:
    """Find a JSON object embedded in a watch page.

    YouTube serves two forms (both seen on 2026-11-17, apparently an experiment): a
    `<script id="..." type="application/json">` element, and the older `var name = {...};` assignment.
    """
    tag = re.search(rf'<script[^>]*\bid="{re.escape(script_id)}"[^>]*>(.*?)</script>', page, re.DOTALL)
    if tag is not None:
        for candidate in (tag.group(1), html_lib.unescape(tag.group(1))):
            try:
                decoded = json.loads(candidate)
            except ValueError:
                continue
            if isinstance(decoded, dict):
                return decoded
    decoder = json.JSONDecoder()
    for match in re.finditer(
        rf"""(?:var\s+|window\s*\[\s*["']){re.escape(name)}(?:["']\s*\])?\s*=\s*""", page
    ):
        try:
            decoded, _ = decoder.raw_decode(page, match.end())
        except ValueError:
            continue
        if isinstance(decoded, dict):
            return decoded
    return None


def text_of(node: Any) -> str:
    """Flatten a YouTube text node (`simpleText` or `runs`) to plain text.

    Standard emoji become the emoji character and custom channel emoji their `:shortcut:`.
    """
    if not isinstance(node, dict):
        return ""
    simple = node.get("simpleText")
    if isinstance(simple, str):
        return simple
    parts: list[str] = []
    for run in node.get("runs") or []:
        if not isinstance(run, dict):
            continue
        text = run.get("text")
        emoji = run.get("emoji")
        if isinstance(text, str):
            parts.append(text)
        elif isinstance(emoji, dict):
            shortcut = _dig(emoji, "shortcuts", 0)
            emoji_id = emoji.get("emojiId")
            if not emoji.get("isCustomEmoji") and isinstance(emoji_id, str):
                parts.append(emoji_id)
            elif isinstance(shortcut, str):
                parts.append(shortcut)
    return "".join(parts)


def _first_continuation(continuations: Any) -> tuple[str | None, int | None]:
    """Return the first usable continuation token of a `continuations` list and its `timeoutMs`."""
    for entry in continuations if isinstance(continuations, list) else []:
        if not isinstance(entry, dict):
            continue
        for kind, body in entry.items():
            if kind == "playerSeekContinuationData" or not isinstance(body, dict):
                continue
            token = body.get("continuation")
            if isinstance(token, str) and token:
                timeout = body.get("timeoutMs")
                return token, timeout if isinstance(timeout, int) else None
    return None, None


def parse_watch_page(page: str, video_id: str) -> WatchPage:
    """Extract the chat entry point and video facts from watch page HTML.

    Raises `ChatSourceError` when the page is not a watch page at all (consent wall, block page), which
    is different from a watch page without chat (`continuation is None`).
    """
    version = _CLIENT_VERSION_RE.search(page)
    initial = _embedded_json(page, "ytInitialData", "yt-initial-data")
    if version is None or initial is None:
        raise ChatSourceError(f"YouTube did not return a usable watch page for {video_id}")
    api_key = _API_KEY_RE.search(page)
    visitor = _VISITOR_DATA_RE.search(page)
    player = _embedded_json(page, "ytInitialPlayerResponse", "yt-initial-player-response") or {}
    bar = _dig(initial, "contents", "twoColumnWatchNextResults", "conversationBar")
    renderer = _dig(bar, "liveChatRenderer")
    continuation, _ = _first_continuation(_dig(renderer, "continuations"))
    notice = text_of(_dig(bar, "conversationBarRenderer", "availabilityMessage", "messageRenderer", "text"))
    status = _dig(player, "playabilityStatus", "status")
    reason = _dig(player, "playabilityStatus", "reason")
    unplayable = None
    if isinstance(status, str) and status != "OK":
        unplayable = reason if isinstance(reason, str) and reason else status
    length = _dig(player, "videoDetails", "lengthSeconds")
    duration = float(length) if isinstance(length, str) and length.isdigit() and int(length) > 0 else None
    title = _dig(player, "videoDetails", "title")
    return WatchPage(
        video_id=video_id,
        client_version=version.group(1),
        api_key=api_key.group(1) if api_key else None,
        visitor_data=visitor.group(1) if visitor else None,
        continuation=continuation,
        is_replay=bool(_dig(renderer, "isReplay")),
        is_live=bool(_dig(player, "videoDetails", "isLive")),
        duration_s=duration,
        title=title if isinstance(title, str) else None,
        chat_notice=notice or None,
        unplayable_reason=unplayable,
    )


def parse_chat_response(data: Mapping[str, Any]) -> ChatPage | None:
    """Convert a chat API response. Returns None when it carries no chat (the chat has ended or is gone)."""
    chat = _dig(dict(data), "continuationContents", "liveChatContinuation")
    if not isinstance(chat, dict):
        return None
    raw_actions = chat.get("actions")
    actions = (
        [action for action in raw_actions if isinstance(action, dict)]
        if isinstance(raw_actions, list)
        else []
    )
    continuation, timeout_ms = _first_continuation(chat.get("continuations"))
    views = _dig(
        chat, "header", "liveChatHeaderRenderer", "viewSelector", "sortFilterSubMenuRenderer", "subMenuItems"
    )
    all_chat = None
    # The view list is [Top chat, Live chat]; the second one shows every message.
    if isinstance(views, list) and len(views) >= 2 and not _dig(views, 1, "selected"):
        token, _ = _first_continuation([_dig(views, 1, "continuation")])
        all_chat = token
    return ChatPage(
        actions=actions, continuation=continuation, timeout_ms=timeout_ms, all_chat_continuation=all_chat
    )


def _item_text(kind: Kind, renderer: dict[str, Any]) -> str:
    message = text_of(renderer.get("message"))
    if kind == "chat":
        return message
    extra = (
        text_of(renderer.get("purchaseAmountText"))
        or text_of(renderer.get("headerSubtext"))
        or text_of(renderer.get("headerPrimaryText"))
        or text_of(_dig(renderer, "header", "liveChatSponsorshipsHeaderRenderer", "primaryText"))
        or text_of(renderer.get("text"))
    )
    return " ".join(part for part in (extra, message) if part)


def _badge_types(renderer: dict[str, Any]) -> set[str]:
    badges = renderer.get("authorBadges")
    types: set[str] = set()
    for badge in badges if isinstance(badges, list) else []:
        icon_type = _dig(badge, "liveChatAuthorBadgeRenderer", "icon", "iconType")
        if isinstance(icon_type, str):
            types.add(icon_type)
    return types


def item_to_message(item: Mapping[str, Any], offset_s: float | None) -> tuple[str, ChatMessage] | None:
    """Convert one chat item (`{"<type>Renderer": {...}}`) to `(message id, ChatMessage)`.

    `offset_s` is the replay position in seconds; None means live, where `t` is the item's own timestamp
    in unix seconds. Items that are not messages, or have no usable time, give None.
    """
    for name, renderer in item.items():
        if name in SKIPPED_RENDERERS or not name.endswith("Renderer") or not isinstance(renderer, dict):
            continue
        kind = RENDERER_KINDS.get(name, "notice")
        if offset_s is not None:
            t = offset_s
        else:
            stamp = renderer.get("timestampUsec")
            if not isinstance(stamp, str) or not stamp.isdigit():
                return None
            t = int(stamp) / 1_000_000
        # Gift purchases keep the author inside a header renderer.
        author_node = renderer
        header = _dig(renderer, "header", "liveChatSponsorshipsHeaderRenderer")
        if "authorName" not in renderer and isinstance(header, dict):
            author_node = header
        username = text_of(author_node.get("authorName"))
        channel_id = renderer.get("authorExternalChannelId")
        user_id = channel_id if isinstance(channel_id, str) and channel_id else username.lower()
        if not user_id:
            user_id = username = SYSTEM_USER
        badges = _badge_types(author_node)
        message = ChatMessage(
            platform="youtube",
            t=t,
            user_id=user_id,
            username=username or user_id,
            text=_item_text(kind, renderer),
            kind=kind,
            is_mod="MODERATOR" in badges,
            is_broadcaster="OWNER" in badges,
        )
        message_id = renderer.get("id")
        return (message_id if isinstance(message_id, str) else f"{user_id}:{t}:{name}"), message
    return None


def action_to_messages(action: Mapping[str, Any]) -> list[tuple[str, ChatMessage]]:
    """Convert one raw chat action to `(message id, ChatMessage)` pairs (usually zero or one).

    A live action is `{"addChatItemAction": {"item": ...}}`. A replay action wraps live actions in
    `{"replayChatItemAction": {"actions": [...], "videoOffsetTimeMsec": "<ms>"}}`, and its messages get
    `t` = that offset in seconds. Ticker, banner, deletion and other actions are ignored.
    """
    replay = action.get("replayChatItemAction")
    if isinstance(replay, dict):
        raw_offset = replay.get("videoOffsetTimeMsec")
        try:
            offset_s: float | None = int(str(raw_offset)) / 1000
        except ValueError:
            return []
        inner = replay.get("actions")
        actions = [entry for entry in inner if isinstance(entry, dict)] if isinstance(inner, list) else []
    else:
        offset_s = None
        actions = [dict(action)]
    messages: list[tuple[str, ChatMessage]] = []
    for entry in actions:
        item = _dig(entry, "addChatItemAction", "item")
        if isinstance(item, dict):
            converted = item_to_message(item, offset_s)
            if converted is not None:
                messages.append(converted)
    return messages


def new_client(
    proxy: str | None = None,
    cookies_file: Path | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Return the HTTP client for chat calls: browser-like headers, the consent cookie, a timeout on every
    request, and optionally a proxy and a Netscape-format cookies file. `transport` is for tests."""
    cookies = httpx.Cookies()
    cookies.set(CONSENT_COOKIE[0], CONSENT_COOKIE[1], domain=".youtube.com")
    if cookies_file is not None:
        jar = MozillaCookieJar(str(cookies_file))
        jar.load(ignore_discard=True, ignore_expires=True)
        for cookie in jar:
            cookies.jar.set_cookie(cookie)
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        cookies=cookies,
        proxy=proxy,
        timeout=httpx.Timeout(HTTP_TIMEOUT_S),
        follow_redirects=True,
        transport=transport,
    )


async def fetch_watch_page(http: httpx.AsyncClient, video_id: str) -> WatchPage:
    """Download and parse the watch page of `video_id`. Raises `ChatSourceError` on any failure."""
    try:
        response = await http.get(WATCH_URL, params={"v": video_id, "hl": "en"})
    except httpx.HTTPError as exc:
        raise ChatSourceError(f"YouTube watch page request failed: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise ChatSourceError(f"YouTube watch page returned HTTP {response.status_code}")
    if response.url.host.startswith("consent."):
        raise ChatSourceError("YouTube redirected to the cookie consent page")
    return parse_watch_page(response.text, video_id)


async def post_chat(
    http: httpx.AsyncClient, page: WatchPage, endpoint: str, continuation: str
) -> ChatPage | None:
    """Call a chat endpoint with a continuation token. None means the chat has no more data.

    Raises `ChatSourceError` on a network failure, a non-200 status or a body that is not JSON.
    """
    client: dict[str, Any] = {
        "clientName": "WEB",
        "clientVersion": page.client_version,
        "hl": "en",
        "gl": "US",
    }
    if page.visitor_data:
        client["visitorData"] = page.visitor_data
    params = {"prettyPrint": "false"}
    if page.api_key:
        params["key"] = page.api_key
    try:
        response = await http.post(
            CHAT_API_URL.format(endpoint=endpoint),
            params=params,
            json={"context": {"client": client}, "continuation": continuation},
        )
    except httpx.HTTPError as exc:
        raise ChatSourceError(f"YouTube chat request failed: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise ChatSourceError(f"YouTube chat endpoint {endpoint} returned HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise ChatSourceError(f"YouTube chat endpoint {endpoint} returned a body that is not JSON") from exc
    if not isinstance(data, dict):
        raise ChatSourceError(f"YouTube chat endpoint {endpoint} returned unexpected JSON")
    return parse_chat_response(data)
