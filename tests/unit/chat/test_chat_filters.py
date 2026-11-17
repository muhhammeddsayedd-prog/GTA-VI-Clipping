"""Chat filters: what counts towards activity, bot learning and the per-user cap. No network, no database."""

from pathlib import Path

import pytest

from sixstars.chat import synthetic, twitch_replay, youtube_replay
from sixstars.chat.filters import (
    FilterContext,
    cap_per_user,
    is_counted,
    learn_bots,
    normalise_name,
)
from sixstars.chat.models import ChatMessage, Kind

IGNORED = frozenset({"nightbot", "streamelements", "streamlabs"})
CTX = FilterContext(warmup_s=300.0, ignore_users=IGNORED)


def _msg(
    t: float = 1000.0,
    username: str = "viewer",
    text: str = "LMAO",
    kind: Kind = "chat",
    *,
    is_broadcaster: bool = False,
    is_mod: bool = False,
    user_id: str | None = None,
) -> ChatMessage:
    return ChatMessage(
        platform="twitch",
        t=t,
        user_id=user_id or f"id_{username}",
        username=username,
        text=text,
        kind=kind,
        is_mod=is_mod,
        is_broadcaster=is_broadcaster,
    )


def test_plain_chat_is_counted() -> None:
    assert is_counted(_msg(), CTX)
    assert is_counted(_msg(is_mod=True), CTX)  # moderators are viewers too


@pytest.mark.parametrize("kind", ["notice", "sub", "gift", "raid", "donation", "system"])
def test_only_chat_kind_is_counted(kind: Kind) -> None:
    assert not is_counted(_msg(kind=kind), CTX)


def test_broadcaster_is_not_counted() -> None:
    assert not is_counted(_msg(is_broadcaster=True), CTX)


@pytest.mark.parametrize("username", ["Nightbot", "@Nightbot", "StreamElements", " streamlabs "])
def test_ignored_users_are_not_counted_in_any_spelling(username: str) -> None:
    assert not is_counted(_msg(username=username), CTX)


def test_learned_bots_are_not_counted() -> None:
    ctx = CTX.with_bots({"SpamAccount", "@OtherBot"})
    assert ctx.bot_names == {"spamaccount", "otherbot"}
    assert not is_counted(_msg(username="spamaccount"), ctx)
    assert not is_counted(_msg(username="@OtherBot"), ctx)
    assert is_counted(_msg(username="someone_else"), ctx)
    assert is_counted(_msg(username="spamaccount"), CTX)  # the original context is unchanged


@pytest.mark.parametrize("text", ["", "   ", "\t\n"])
def test_empty_text_is_not_counted(text: str) -> None:
    assert not is_counted(_msg(text=text), CTX)


def test_warmup_in_replay_mode() -> None:
    assert not is_counted(_msg(t=0.0), CTX)
    assert not is_counted(_msg(t=299.9), CTX)
    assert is_counted(_msg(t=300.0), CTX)


def test_warmup_in_live_mode_is_relative_to_the_stream_start() -> None:
    ctx = FilterContext(warmup_s=300.0, ignore_users=IGNORED, stream_start=1_700_000_000.0)
    assert not is_counted(_msg(t=1_700_000_299.0), ctx)
    assert is_counted(_msg(t=1_700_000_300.0), ctx)


def test_normalise_name() -> None:
    assert normalise_name(" @NightBot ") == "nightbot"
    assert normalise_name("Plain_Name") == "plain_name"


# --- bot learning -----------------------------------------------------------------------------------


def test_learn_bots_finds_a_repeating_account() -> None:
    chat = synthetic.merge(synthetic.flat_chat(600, 8), synthetic.bot_flood(0, 600))
    assert learn_bots(chat) == {"dealsbot9000x"}


def test_learn_bots_finds_names_ending_in_bot_even_with_one_message() -> None:
    messages = [
        _msg(username="CoolBot", text="hi"),
        _msg(username="@Helper_BOT", text="yo"),
        _msg(username="robot_fan"),
    ]
    assert learn_bots(messages) == {"coolbot", "helper_bot"}


def test_learn_bots_needs_enough_messages() -> None:
    repeated = [_msg(t=float(i), username="eager", text="same text every time") for i in range(19)]
    assert learn_bots(repeated) == set()
    assert learn_bots([*repeated, _msg(t=20.0, username="eager", text="same text every time")]) == {"eager"}


def test_learn_bots_needs_a_dominant_prefix() -> None:
    def messages(same: int) -> list[ChatMessage]:
        repeated = [
            _msg(username="chatty", text="Follow the channel and join now " + str(i)) for i in range(same)
        ]
        varied = [
            _msg(username="chatty", text=f"{i} something different each time") for i in range(20 - same)
        ]
        return repeated + varied

    assert learn_bots(messages(16)) == {"chatty"}  # 80% share a 20-character prefix
    assert learn_bots(messages(15)) == set()  # 75% is under the threshold


def test_learn_bots_ignores_notices_and_respects_parameters() -> None:
    subs = [_msg(username="gifter", text="gifted a sub", kind="gift") for _ in range(30)]
    assert learn_bots(subs) == set()
    short = [_msg(username="emote_fan", text="KEKW") for _ in range(5)]
    assert learn_bots(short, min_messages=5) == {"emote_fan"}
    assert learn_bots(short, min_messages=5, same_prefix_ratio=1.0, prefix_chars=2) == {"emote_fan"}


def test_an_emote_spammer_is_learned_as_a_bot_only_past_the_message_floor() -> None:
    # A viewer repeating one emote 20 times in the learning window looks like a bot by this rule; the
    # per-bucket cap (3) is what protects real reactions, so this is accepted behaviour (docs/05 section 2).
    spam = [_msg(t=float(i), username="hype_viewer", text="KEKW") for i in range(20)]
    assert learn_bots(spam) == {"hype_viewer"}


def test_learn_bots_on_real_samples(fixtures_dir: Path) -> None:
    twitch = twitch_replay.parse_file(fixtures_dir / "twitch_replay_sample.json").messages
    assert learn_bots(twitch) == set()  # the known bot there (StreamElements) is on the ignore list instead
    youtube = youtube_replay.parse_actions_file(fixtures_dir / "youtube_live_sample.jsonl")
    assert "nightbot" in learn_bots(youtube)


def test_filters_on_the_real_twitch_sample(fixtures_dir: Path) -> None:
    messages = twitch_replay.parse_file(fixtures_dir / "twitch_replay_sample.json").messages
    counted = [message for message in messages if is_counted(message, CTX)]
    dropped = [message for message in messages if not is_counted(message, CTX)]
    # Dropped: one message in the warm-up, one cheer, and the StreamElements messages.
    assert len(messages) == 200
    assert {message.kind for message in counted} == {"chat"}
    assert all(normalise_name(message.username) != "streamelements" for message in counted)
    assert any(message.kind == "donation" for message in dropped)
    assert any(message.t < 300 for message in dropped)
    assert len(counted) + len(dropped) == 200 and len(counted) >= 190


def test_filters_on_the_real_youtube_sample(fixtures_dir: Path) -> None:
    messages = youtube_replay.parse_actions_file(fixtures_dir / "youtube_replay_sample.jsonl")
    counted = [message for message in messages if is_counted(message, CTX)]
    assert {message.kind for message in counted} == {"chat"}
    assert all(message.t >= 300 for message in counted)
    assert not any(normalise_name(message.username) in IGNORED for message in counted)
    assert 0 < len(counted) < 171


# --- per-user cap -----------------------------------------------------------------------------------


def test_cap_per_user_keeps_the_first_three_per_user_in_order() -> None:
    bucket = [
        _msg(1.0, "a", "1"),
        _msg(2.0, "b", "1"),
        _msg(3.0, "a", "2"),
        _msg(4.0, "a", "3"),
        _msg(5.0, "a", "4"),
        _msg(6.0, "b", "2"),
        _msg(7.0, "a", "5"),
    ]
    kept = cap_per_user(bucket)
    assert [(message.username, message.text) for message in kept] == [
        ("a", "1"),
        ("b", "1"),
        ("a", "2"),
        ("a", "3"),
        ("b", "2"),
    ]


def test_cap_per_user_uses_the_user_id_and_a_custom_limit() -> None:
    bucket = [
        _msg(1.0, "Name", user_id="7"),
        _msg(2.0, "NewName", user_id="7"),
        _msg(3.0, "other", user_id="8"),
    ]
    assert len(cap_per_user(bucket, 1)) == 2
    assert cap_per_user([], 3) == []
    assert cap_per_user(iter(bucket), 0) == []


def test_three_user_flood_is_capped() -> None:
    flood = synthetic.burst(1000.0, 120, 3)
    assert len(cap_per_user(flood)) == 9
