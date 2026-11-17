"""Live Twitch chat reader over IRC on websockets, yielding normalised `ChatMessage` objects with reconnect
handling. Implemented in M03.

The connection is anonymous (`NICK justinfan<digits>`), so no Twitch API keys are needed. `parse_line` and
`to_message` are pure and unit-tested on recorded lines; `stream` owns the socket, answers `PING`, sends
its own `PING` when the channel is silent, and reconnects with backoff on a drop or a server `RECONNECT`.
"""

import asyncio
import random
import re
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Final, Protocol

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import WebSocketException

from sixstars.chat.models import ChatConnectionError, ChatMessage, Kind
from sixstars.chat.net import backoff_delay
from sixstars.logging import get_logger

TWITCH_IRC_URL: Final = "wss://irc-ws.chat.twitch.tv:443"
ANONYMOUS_PASS: Final = "SCHMOOPIIE"
CAPABILITIES: Final = "twitch.tv/tags twitch.tv/commands"

# Connection constants (not runtime settings: `docs/16-settings-reference.md` has no keys for them).
OPEN_TIMEOUT_S: Final = 10.0
CLOSE_TIMEOUT_S: Final = 5.0
IDLE_PING_S: Final = 60.0  # silence before we send our own PING
PONG_TIMEOUT_S: Final = 15.0  # silence after our PING before the connection counts as dead
MAX_FRAME_BYTES: Final = 1 << 20

USERNOTICE_KINDS: Final[dict[str, Kind]] = {
    "sub": "sub",
    "resub": "sub",
    "subgift": "gift",
    "submysterygift": "gift",
    "giftpaidupgrade": "gift",
    "anongiftpaidupgrade": "gift",
    "anonsubgift": "gift",
    "anonsubmysterygift": "gift",
    "raid": "raid",
}

_TAG_ESCAPES: Final = {"\\s": " ", "\\:": ";", "\\\\": "\\", "\\r": "\r", "\\n": "\n"}
_TAG_ESCAPE_RE: Final = re.compile(r"\\[s:\\rn]")
_LOGIN_RE: Final = re.compile(r"^[a-z0-9_]{1,25}$")
_URL_PREFIX_RE: Final = re.compile(r"^(https?://)?(www\.)?twitch\.tv/")
_ACTION_PREFIX: Final = "\x01ACTION "


@dataclass(frozen=True, slots=True)
class IrcLine:
    """One parsed IRC line: IRCv3 tags, prefix, upper-case command, middle parameters and trailing text."""

    tags: dict[str, str]
    prefix: str
    command: str
    params: tuple[str, ...]
    trailing: str | None


class IrcConnection(Protocol):
    """The part of a websocket connection the reader uses (lets tests pass a scripted fake)."""

    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...


Connector = Callable[[], AbstractAsyncContextManager[IrcConnection]]


class _DroppedError(Exception):
    """The current connection must be replaced (server RECONNECT or no answer to our PING)."""


def unescape_tag(value: str) -> str:
    """Undo IRCv3 tag-value escaping (`\\s` is a space, `\\:` a semicolon, `\\\\` a backslash)."""
    return _TAG_ESCAPE_RE.sub(lambda match: _TAG_ESCAPES[match.group(0)], value)


def parse_line(line: str) -> IrcLine | None:
    """Parse one raw IRC line (without the line ending). Returns None for an empty or malformed line."""
    rest = line.rstrip("\r\n")
    tags: dict[str, str] = {}
    if rest.startswith("@"):
        raw_tags, _, rest = rest[1:].partition(" ")
        for item in raw_tags.split(";"):
            key, _, value = item.partition("=")
            if key:
                tags[key] = unescape_tag(value)
    rest = rest.lstrip(" ")
    prefix = ""
    if rest.startswith(":"):
        prefix, _, rest = rest[1:].partition(" ")
    head, separator, trailing = rest.partition(" :")
    parts = head.split()
    if not parts:
        return None
    return IrcLine(
        tags=tags,
        prefix=prefix,
        command=parts[0].upper(),
        params=tuple(parts[1:]),
        trailing=trailing if separator else None,
    )


def _badge_names(tags: dict[str, str]) -> set[str]:
    return {badge.partition("/")[0] for badge in tags.get("badges", "").split(",") if badge}


def _sent_time(tags: dict[str, str], now: float) -> float:
    raw = tags.get("tmi-sent-ts", "")
    return int(raw) / 1000 if raw.isdigit() else now


def _bits(tags: dict[str, str]) -> int:
    raw = tags.get("bits", "")
    return int(raw) if raw.isdigit() else 0


def to_message(irc: IrcLine, now: float) -> ChatMessage | None:
    """Convert a parsed line to a `ChatMessage`. Lines that are not chat or user notices give None.

    `now` (unix seconds) is used only when the line has no `tmi-sent-ts` tag.
    """
    if irc.command not in ("PRIVMSG", "USERNOTICE"):
        return None
    tags = irc.tags
    # PRIVMSG carries the login in the prefix (`nick!nick@nick.tmi.twitch.tv`), USERNOTICE in a tag.
    login = tags.get("login", "") if irc.command == "USERNOTICE" else irc.prefix.partition("!")[0]
    username = tags.get("display-name") or login
    user_id = tags.get("user-id") or login.lower()
    if not user_id:
        return None
    text = irc.trailing or ""
    kind: Kind
    if irc.command == "PRIVMSG":
        if text.startswith(_ACTION_PREFIX):
            text = text[len(_ACTION_PREFIX) :].rstrip("\x01")
        kind = "donation" if _bits(tags) > 0 else "chat"
    else:
        kind = USERNOTICE_KINDS.get(tags.get("msg-id", ""), "notice")
        text = text or tags.get("system-msg", "")
    badges = _badge_names(tags)
    return ChatMessage(
        platform="twitch",
        t=_sent_time(tags, now),
        user_id=user_id,
        username=username,
        text=text,
        kind=kind,
        is_mod="moderator" in badges or tags.get("mod") == "1",
        is_broadcaster="broadcaster" in badges,
    )


def normalise_login(login: str) -> str:
    """Return a Twitch login in channel form (lowercase, no `#`, no URL). Raises `ValueError` if invalid."""
    cleaned = login.strip().lower().removeprefix("#")
    cleaned = _URL_PREFIX_RE.sub("", cleaned).strip("/")
    if not _LOGIN_RE.match(cleaned):
        raise ValueError(f"not a Twitch login: {login!r}")
    return cleaned


def anonymous_nick() -> str:
    """Return a fresh anonymous nickname: `justinfan` plus five random digits."""
    return f"justinfan{random.randint(10000, 99999)}"


def handshake_lines(channel: str, nick: str) -> list[str]:
    """The lines sent after connecting, in order: PASS, NICK, CAP REQ, JOIN."""
    return [f"PASS {ANONYMOUS_PASS}", f"NICK {nick}", f"CAP REQ :{CAPABILITIES}", f"JOIN #{channel}"]


def default_connector(proxy: str | None = None) -> Connector:
    """Return a connector that opens the Twitch IRC websocket, through `proxy` when one is given."""

    @asynccontextmanager
    async def open_connection() -> AsyncIterator[IrcConnection]:
        # Keepalive is done at IRC level (PING/PONG), so websocket-level pings are off.
        async with ws_connect(
            TWITCH_IRC_URL,
            open_timeout=OPEN_TIMEOUT_S,
            close_timeout=CLOSE_TIMEOUT_S,
            ping_interval=None,
            max_size=MAX_FRAME_BYTES,
            proxy=proxy,
        ) as connection:
            yield connection

    return open_connection


async def _read_connection(
    connection: IrcConnection,
    channel: str,
    clock: Callable[[], float],
    idle_ping_s: float,
    pong_timeout_s: float,
) -> AsyncIterator[ChatMessage]:
    """Join `channel` on an open connection and yield its messages until the connection must be replaced."""
    log = get_logger("chat")
    for line in handshake_lines(channel, anonymous_nick()):
        await connection.send(line)
    ping_pending = False
    while True:
        try:
            frame = await asyncio.wait_for(
                connection.recv(), timeout=pong_timeout_s if ping_pending else idle_ping_s
            )
        except TimeoutError:
            if ping_pending:
                raise _DroppedError("no answer to PING") from None
            await connection.send("PING :sixstars")
            ping_pending = True
            continue
        ping_pending = False
        text = frame.decode("utf-8", "replace") if isinstance(frame, bytes) else frame
        for raw in text.split("\r\n"):
            irc = parse_line(raw)
            if irc is None:
                continue
            if irc.command == "PING":
                await connection.send(f"PONG :{irc.trailing or 'tmi.twitch.tv'}")
            elif irc.command == "RECONNECT":
                raise _DroppedError("server sent RECONNECT")
            elif irc.command == "NOTICE":
                log.info(
                    "twitch_irc_notice", channel=channel, msg_id=irc.tags.get("msg-id"), text=irc.trailing
                )
            else:
                message = to_message(irc, clock())
                if message is not None:
                    yield message


async def stream(
    login: str,
    *,
    proxy: str | None = None,
    connector: Connector | None = None,
    max_reconnects: int | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.time,
    idle_ping_s: float = IDLE_PING_S,
    pong_timeout_s: float = PONG_TIMEOUT_S,
) -> AsyncGenerator[ChatMessage, None]:
    """Yield the live chat of Twitch channel `login` as `ChatMessage` objects, for as long as it is read.

    `t` is `tmi-sent-ts` in unix seconds. The iterator never ends on its own: an offline channel is simply
    silent, so the caller stops reading (closing the iterator closes the socket). On a drop or a server
    `RECONNECT` it reconnects after 1, 2, 5, 10, then 30 seconds; the schedule restarts once a connection
    has delivered a message. With `max_reconnects` set, that many consecutive failed attempts raise
    `ChatConnectionError`; None means retry forever.
    """
    log = get_logger("chat")
    channel = normalise_login(login)
    open_connection = connector or default_connector(proxy)
    failures = 0
    while True:
        reason = "connection closed"
        try:
            async with open_connection() as connection:
                log.info("twitch_irc_connected", channel=channel)
                async for message in _read_connection(
                    connection, channel, clock, idle_ping_s, pong_timeout_s
                ):
                    failures = 0
                    yield message
        except _DroppedError as exc:
            reason = str(exc)
        except (OSError, WebSocketException) as exc:
            reason = f"{type(exc).__name__}: {exc}"
        if max_reconnects is not None and failures >= max_reconnects:
            log.error("twitch_irc_gave_up", channel=channel, reason=reason, failures=failures)
            raise ChatConnectionError(f"Twitch chat for {channel} lost after {failures} reconnects: {reason}")
        delay = backoff_delay(failures)
        failures += 1
        log.warning(
            "twitch_irc_reconnecting", channel=channel, reason=reason, delay_s=delay, attempt=failures
        )
        await sleep(delay)
