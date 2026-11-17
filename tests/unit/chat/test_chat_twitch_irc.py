"""Twitch IRC: the line parser on recorded lines, the message mapping, and the reconnecting reader driven by
a scripted fake connection (no network)."""

import asyncio
from collections import Counter, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from sixstars.chat import twitch_irc
from sixstars.chat.models import ChatConnectionError, ChatMessage

# Lines in the exact format of the examples in Twitch's IRC documentation (not captured: no cheer, gift or
# raid happened during the 150-second capture that produced `twitch_irc_lines.txt`).
BITS_LINE = (
    "@badge-info=;badges=staff/1,bits/1000;bits=100;color=;display-name=ronni;emotes=;"
    "id=b34ccfc7-4977-403a-8a94-33c6bac34fb8;mod=0;room-id=12345678;subscriber=0;tmi-sent-ts=1507246572675;"
    "turbo=1;user-id=12345678;user-type=staff :ronni!ronni@ronni.tmi.twitch.tv PRIVMSG #ronni :cheer100"
)
EMOTE_LINE = (
    "@badge-info=;badges=broadcaster/1;client-nonce=459e3142897c7a22b7d275178f2259e0;color=#0000FF;"
    "display-name=abc;emotes=62835:0-10;first-msg=0;flags=;id=885196de-cb67-427a-baa8-82f9b0fcd05f;mod=0;"
    "room-id=713936733;subscriber=0;tmi-sent-ts=1642696567751;turbo=0;user-id=713936733;user-type= "
    ":abc!abc@abc.tmi.twitch.tv PRIVMSG #xyz :bleedPurple"
)
SUBGIFT_LINE = (
    "@badge-info=;badges=staff/1,premium/1;color=#0000FF;display-name=TWW2;emotes=;"
    "id=e9176cd8-5e22-4684-ad40-ce53c2561c5e;login=tww2;mod=0;msg-id=subgift;msg-param-months=1;"
    "msg-param-recipient-display-name=Mr_Woodchuck;msg-param-recipient-id=55554444;"
    "msg-param-recipient-name=mr_woodchuck;msg-param-sub-plan-name=House\\sof\\sNyoro~n;msg-param-sub-plan=1000;"
    "room-id=19571752;subscriber=0;system-msg=TWW2\\sgifted\\sa\\sTier\\s1\\ssub\\sto\\sMr_Woodchuck!;"
    "tmi-sent-ts=1521159445153;turbo=0;user-id=87654321;user-type=staff :tmi.twitch.tv USERNOTICE #forstycup"
)
MYSTERY_GIFT_LINE = SUBGIFT_LINE.replace("msg-id=subgift", "msg-id=submysterygift")
RAID_LINE = (
    "@badge-info=;badges=turbo/1;color=#9ACD32;display-name=TestChannel;emotes=;"
    "id=3d830f12-795c-447d-af3c-ea05e40fbddb;login=testchannel;mod=0;msg-id=raid;"
    "msg-param-displayName=TestChannel;msg-param-login=testchannel;msg-param-viewerCount=15;room-id=33332222;"
    "subscriber=0;system-msg=15\\sraiders\\sfrom\\sTestChannel\\shave\\sjoined\\n!;tmi-sent-ts=1507246572675;"
    "turbo=1;user-id=123456;user-type= :tmi.twitch.tv USERNOTICE #othertestchannel"
)
ANNOUNCEMENT_LINE = RAID_LINE.replace("msg-id=raid", "msg-id=announcement")
PLAIN_LINE = (
    "@badge-info=;badges=moderator/1;display-name=ModUser;mod=1;tmi-sent-ts=1700000000500;user-id=42 "
    ":moduser!moduser@moduser.tmi.twitch.tv PRIVMSG #chan :hello: world :)"
)


def _lines(fixtures_dir: Path) -> list[str]:
    return (fixtures_dir / "twitch_irc_lines.txt").read_text(encoding="utf-8").splitlines()


def _parse(line: str) -> twitch_irc.IrcLine:
    parsed = twitch_irc.parse_line(line)
    assert parsed is not None
    return parsed


def _message(line: str, now: float = 0.0) -> ChatMessage:
    message = twitch_irc.to_message(_parse(line), now)
    assert message is not None
    return message


# --- parser -----------------------------------------------------------------------------------------


def test_recorded_lines_parse_into_the_expected_commands(fixtures_dir: Path) -> None:
    parsed = [twitch_irc.parse_line(line) for line in _lines(fixtures_dir)]
    assert all(line is not None for line in parsed)
    commands = Counter(line.command for line in parsed if line is not None)
    assert commands["PRIVMSG"] == 93
    assert commands["USERNOTICE"] == 2
    assert commands["CLEARCHAT"] == 2
    assert commands["ROOMSTATE"] == 1
    assert commands["001"] == 1


def test_recorded_lines_give_chat_and_sub_messages_only(fixtures_dir: Path) -> None:
    messages = [twitch_irc.to_message(_parse(line), 0.0) for line in _lines(fixtures_dir)]
    kept = [message for message in messages if message is not None]
    assert Counter(message.kind for message in kept) == {"chat": 93, "sub": 2}
    assert all(message.platform == "twitch" for message in kept)
    assert sum(message.is_mod for message in kept) == 3
    assert sum(message.is_broadcaster for message in kept) == 6
    assert all(message.user_id.isdigit() for message in kept)


def test_first_recorded_privmsg(fixtures_dir: Path) -> None:
    first = next(line for line in _lines(fixtures_dir) if " PRIVMSG " in line)
    assert _message(first) == ChatMessage(
        platform="twitch",
        t=1794877743.548,
        user_id="733578503",
        username="user_6fa75f47",
        text="1",
        kind="chat",
    )


def test_parse_line_splits_tags_prefix_command_and_trailing() -> None:
    parsed = _parse(PLAIN_LINE)
    assert parsed.command == "PRIVMSG"
    assert parsed.prefix == "moduser!moduser@moduser.tmi.twitch.tv"
    assert parsed.params == ("#chan",)
    assert parsed.trailing == "hello: world :)"
    assert parsed.tags["display-name"] == "ModUser"
    assert parsed.tags["badge-info"] == ""


def test_parse_line_without_tags_or_prefix() -> None:
    ping = _parse("PING :tmi.twitch.tv")
    assert (ping.command, ping.trailing, ping.tags, ping.prefix) == ("PING", "tmi.twitch.tv", {}, "")
    reconnect = _parse(":tmi.twitch.tv RECONNECT")
    assert (reconnect.command, reconnect.trailing) == ("RECONNECT", None)


@pytest.mark.parametrize("line", ["", "   ", "\r\n", "@only=tags"])
def test_parse_line_returns_none_for_empty_lines(line: str) -> None:
    assert twitch_irc.parse_line(line) is None


def test_tag_values_are_unescaped() -> None:
    assert twitch_irc.unescape_tag(r"a\sb\:c\\d\r\n") == "a b;c\\d\r\n"
    assert _parse(SUBGIFT_LINE).tags["system-msg"] == "TWW2 gifted a Tier 1 sub to Mr_Woodchuck!"


# --- message mapping --------------------------------------------------------------------------------


def test_privmsg_with_emotes_keeps_the_text() -> None:
    message = _message(EMOTE_LINE)
    assert (message.kind, message.text, message.username) == ("chat", "bleedPurple", "abc")
    assert message.t == 1642696567.751
    assert message.is_broadcaster is True
    assert message.is_mod is False


def test_recorded_emote_only_message(fixtures_dir: Path) -> None:
    line = next(line for line in _lines(fixtures_dir) if "emotes=425618:0-2" in line)
    assert _message(line).text == "LUL"


def test_moderator_flag_from_badge_and_tag() -> None:
    message = _message(PLAIN_LINE)
    assert message.is_mod is True
    assert message.text == "hello: world :)"
    assert message.t == 1700000000.5


def test_bits_message_is_a_donation() -> None:
    message = _message(BITS_LINE)
    assert (message.kind, message.text, message.user_id) == ("donation", "cheer100", "12345678")


def test_recorded_resub_with_and_without_a_user_message(fixtures_dir: Path) -> None:
    notices = [_message(line) for line in _lines(fixtures_dir) if " USERNOTICE " in line]
    assert [notice.kind for notice in notices] == ["sub", "sub"]
    assert notices[0].text == "ive been gone from the stream for a month. wtf is happening"
    assert notices[1].text == "user_f9c8b54a subscribed with Prime. They've subscribed for 42 months!"
    assert notices[1].username == "user_f9c8b54a"


@pytest.mark.parametrize(
    "line,kind",
    [
        (SUBGIFT_LINE, "gift"),
        (MYSTERY_GIFT_LINE, "gift"),
        (SUBGIFT_LINE.replace("msg-id=subgift", "msg-id=giftpaidupgrade"), "gift"),
        (SUBGIFT_LINE.replace("msg-id=subgift", "msg-id=anongiftpaidupgrade"), "gift"),
        (SUBGIFT_LINE.replace("msg-id=subgift", "msg-id=sub"), "sub"),
        (RAID_LINE, "raid"),
        (ANNOUNCEMENT_LINE, "notice"),
    ],
)
def test_usernotice_kinds(line: str, kind: str) -> None:
    assert _message(line).kind == kind


def test_raid_notice_fields() -> None:
    message = _message(RAID_LINE)
    assert (message.user_id, message.username) == ("123456", "TestChannel")
    assert message.text == "15 raiders from TestChannel have joined\n!"
    assert message.t == 1507246572.675


def test_action_message_is_unwrapped(fixtures_dir: Path) -> None:
    line = next(line for line in _lines(fixtures_dir) if "\x01ACTION" in line and "Rolls" in line)
    assert _message(line).text == "@user_a21756b5 Rolls 35 (1-99)"


def test_missing_timestamp_uses_now_and_missing_ids_use_the_login() -> None:
    message = _message(":SomeOne!someone@someone.tmi.twitch.tv PRIVMSG #chan :hi", now=123.0)
    assert (message.t, message.user_id, message.username) == (123.0, "someone", "SomeOne")


@pytest.mark.parametrize(
    "line",
    [
        "PING :tmi.twitch.tv",
        ":tmi.twitch.tv 001 justinfan12345 :Welcome, GLHF!",
        "@ban-duration=15;room-id=1;target-user-id=2;tmi-sent-ts=3 :tmi.twitch.tv CLEARCHAT #chan :someone",
        "@emote-only=0;room-id=1 :tmi.twitch.tv ROOMSTATE #chan",
        ":justinfan12345!justinfan12345@justinfan12345.tmi.twitch.tv JOIN #chan",
        "@msg-id=msg_channel_suspended :tmi.twitch.tv NOTICE #chan :This channel does not exist.",
    ],
)
def test_other_commands_are_not_messages(line: str) -> None:
    assert twitch_irc.to_message(_parse(line), 0.0) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Buddha", "buddha"),
        ("#some_channel", "some_channel"),
        ("https://www.twitch.tv/Some_Channel/", "some_channel"),
        ("twitch.tv/abc", "abc"),
    ],
)
def test_normalise_login(raw: str, expected: str) -> None:
    assert twitch_irc.normalise_login(raw) == expected


@pytest.mark.parametrize("raw", ["", "has space", "way_too_long_for_a_twitch_login_name", "bad!char"])
def test_normalise_login_rejects_invalid(raw: str) -> None:
    with pytest.raises(ValueError):
        twitch_irc.normalise_login(raw)


def test_handshake_lines_and_nick() -> None:
    nick = twitch_irc.anonymous_nick()
    assert nick.startswith("justinfan") and len(nick) == len("justinfan") + 5 and nick[9:].isdigit()
    assert twitch_irc.handshake_lines("chan", "justinfan12345") == [
        "PASS SCHMOOPIIE",
        "NICK justinfan12345",
        "CAP REQ :twitch.tv/tags twitch.tv/commands",
        "JOIN #chan",
    ]


# --- reader -----------------------------------------------------------------------------------------

HANG = object()


class FakeConnection:
    """Returns scripted frames; `HANG` blocks forever; running out of frames closes the connection."""

    def __init__(self, frames: list[object]) -> None:
        self.frames = deque(frames)
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str | bytes:
        if not self.frames:
            raise ConnectionError("connection closed")
        frame = self.frames.popleft()
        if frame is HANG:
            await asyncio.Event().wait()
        if isinstance(frame, BaseException):
            raise frame
        assert isinstance(frame, str | bytes)
        return frame


def _connector(connections: list[object]) -> twitch_irc.Connector:
    """Hand out the scripted connections in order; an exception entry fails that connection attempt."""
    queue = deque(connections)

    @asynccontextmanager
    async def open_connection() -> AsyncIterator[twitch_irc.IrcConnection]:
        if not queue:
            raise ConnectionRefusedError("no more scripted connections")
        connection = queue.popleft()
        if isinstance(connection, BaseException):
            raise connection
        assert isinstance(connection, FakeConnection)
        yield connection

    return open_connection


class SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


async def _collect(stream: AsyncIterator[ChatMessage]) -> tuple[list[ChatMessage], BaseException | None]:
    messages: list[ChatMessage] = []
    try:
        async for message in stream:
            messages.append(message)
    except ChatConnectionError as exc:
        return messages, exc
    return messages, None


async def test_stream_joins_answers_ping_and_yields_messages() -> None:
    connection = FakeConnection(
        [
            ":tmi.twitch.tv 001 justinfan1 :Welcome, GLHF!\r\n:tmi.twitch.tv CAP * ACK :twitch.tv/tags\r\n",
            f"{PLAIN_LINE}\r\nPING :tmi.twitch.tv\r\n{BITS_LINE}\r\n",
            (RAID_LINE + "\r\n").encode(),
        ]
    )
    sleep = SleepRecorder()
    stream = twitch_irc.stream("#Chan", connector=_connector([connection]), max_reconnects=0, sleep=sleep)
    messages, error = await _collect(stream)
    assert [message.kind for message in messages] == ["chat", "donation", "raid"]
    assert connection.sent[0] == "PASS SCHMOOPIIE"
    assert connection.sent[1].startswith("NICK justinfan")
    assert connection.sent[2:4] == ["CAP REQ :twitch.tv/tags twitch.tv/commands", "JOIN #chan"]
    assert connection.sent[4] == "PONG :tmi.twitch.tv"
    assert isinstance(error, ChatConnectionError)
    assert sleep.delays == []


async def test_stream_reconnects_after_server_reconnect_and_drop() -> None:
    first = FakeConnection([PLAIN_LINE, ":tmi.twitch.tv RECONNECT", BITS_LINE])
    second = FakeConnection([RAID_LINE])
    sleep = SleepRecorder()
    stream = twitch_irc.stream("chan", connector=_connector([first, second]), max_reconnects=1, sleep=sleep)
    messages, error = await _collect(stream)
    # The line after RECONNECT on the first connection is never read; the second connection takes over.
    assert [message.kind for message in messages] == ["chat", "raid"]
    assert second.sent[3] == "JOIN #chan"
    # Each connection delivered a message, so both reconnects use the first backoff step; the third
    # attempt finds no connection and is the one allowed failure.
    assert sleep.delays == [1.0, 1.0]
    assert isinstance(error, ChatConnectionError)


async def test_stream_backoff_schedule_and_give_up() -> None:
    failures: list[object] = [OSError("refused") for _ in range(8)]
    sleep = SleepRecorder()
    stream = twitch_irc.stream("chan", connector=_connector(failures), max_reconnects=7, sleep=sleep)
    messages, error = await _collect(stream)
    assert messages == []
    assert sleep.delays == [1.0, 2.0, 5.0, 10.0, 30.0, 30.0, 30.0]
    assert isinstance(error, ChatConnectionError)
    assert "7 reconnects" in str(error)


async def test_stream_backoff_restarts_after_a_delivered_message() -> None:
    connections: list[object] = [
        OSError("refused"),
        OSError("refused"),
        FakeConnection([PLAIN_LINE]),
        OSError("refused"),
    ]
    sleep = SleepRecorder()
    stream = twitch_irc.stream("chan", connector=_connector(connections), max_reconnects=3, sleep=sleep)
    messages, error = await _collect(stream)
    assert len(messages) == 1
    assert sleep.delays == [1.0, 2.0, 1.0, 2.0, 5.0]
    assert isinstance(error, ChatConnectionError)


async def test_stream_pings_a_silent_connection_and_replaces_it_when_unanswered() -> None:
    silent = FakeConnection([PLAIN_LINE, HANG, HANG])
    second = FakeConnection([BITS_LINE])
    sleep = SleepRecorder()
    stream = twitch_irc.stream(
        "chan",
        connector=_connector([silent, second]),
        max_reconnects=1,
        sleep=sleep,
        idle_ping_s=0.01,
        pong_timeout_s=0.01,
    )
    messages, _ = await _collect(stream)
    assert [message.kind for message in messages] == ["chat", "donation"]
    assert silent.sent[-1] == "PING :sixstars"
    assert sleep.delays == [1.0, 1.0]


async def test_stream_can_be_closed_by_the_consumer() -> None:
    connection = FakeConnection([PLAIN_LINE, BITS_LINE, HANG])
    stream = twitch_irc.stream("chan", connector=_connector([connection]))
    seen = []
    async for message in stream:
        seen.append(message)
        if len(seen) == 2:
            break
    await stream.aclose()
    assert [message.kind for message in seen] == ["chat", "donation"]


async def test_stream_rejects_a_bad_login_before_connecting() -> None:
    with pytest.raises(ValueError):
        async for _ in twitch_irc.stream("not a login"):
            pass
