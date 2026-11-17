"""Bucketing: tokens, per-user cap, counts per bucket and the optional audio jump."""

import pytest

from sixstars.chat.models import ChatMessage
from sixstars.spikes.buckets import (
    Bucket,
    Counted,
    LoudnessTrack,
    TokenMatcher,
    count_message,
    make_buckets,
    squeeze,
    tokenise,
)
from sixstars.spikes.config import SpikeConfig


def msg(t: float, user: str, text: str = "hello") -> ChatMessage:
    return ChatMessage(platform="twitch", t=t, user_id=f"id_{user}", username=user, text=text, kind="chat")


def counted(t: float, user: str, react: bool = False, clip: bool = False) -> Counted:
    return Counted(t, f"id_{user}", react, clip, msg(t, user))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("no way!!", ["NO", "WAY"]),
        ("LMAOOOOO", ["LMAO"]),
        ("wwww", ["W"]),
        ("clip-it,now", ["CLIP", "IT", "NOW"]),
        ("💀💀💀", []),
        ("  ", []),
        ("what_the 4head", ["WHAT", "THE", "4HEAD"]),
    ],
)
def test_tokenise(text: str, expected: list[str]) -> None:
    assert tokenise(text) == expected


def test_squeeze() -> None:
    assert [squeeze(word) for word in ("POGGERS", "LOL", "NOOOO")] == ["POGERS", "LOL", "NO"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("LMAO", True),
        ("lmaooooo that was wild", True),
        ("lol", True),
        ("W", True),
        ("WWWWWW", True),
        ("what a win", False),  # "W" must be a whole word
        ("NO WAY", True),
        ("no   way!!", True),
        ("there is no way he did that", True),
        ("I know wayne", False),  # multi-word tokens match whole words only
        ("poggers", True),
        ("PogChamp", True),
        ("he is dead 💀", True),
        ("😂😂😂", True),
        ("nice car", False),
        ("clip it", True),
        ("", False),
    ],
)
def test_reaction_tokens_from_the_seed(cfg: SpikeConfig, text: str, expected: bool) -> None:
    assert TokenMatcher.from_tokens(cfg.reaction_tokens).matches(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("CLIP IT", True),
        ("someone clip that", True),
        ("clipped", True),
        ("CLIPPPP", True),
        ("clipit", True),
        ("paperclips are useful", False),
        ("eclipse", False),
    ],
)
def test_clip_tokens_from_the_seed(cfg: SpikeConfig, text: str, expected: bool) -> None:
    assert TokenMatcher.from_tokens(cfg.clip_tokens).matches(text) is expected


def test_a_trailing_star_makes_a_prefix_token() -> None:
    matcher = TokenMatcher.from_tokens(["HAHA*", "LOL"])
    assert matcher.matches("hahahahaha") and matcher.matches("HAHA")
    assert not matcher.matches("aha") and not matcher.matches("lolz")


def test_count_message_sets_both_flags(cfg: SpikeConfig) -> None:
    reactions, clips = (
        TokenMatcher.from_tokens(cfg.reaction_tokens),
        TokenMatcher.from_tokens(cfg.clip_tokens),
    )
    item = count_message(msg(12.0, "a", "LMAO clip it"), reactions, clips)
    assert (item.t, item.user_id, item.react, item.clip) == (12.0, "id_a", True, True)
    plain = count_message(msg(13.0, "b", "where is he going"), reactions, clips)
    assert (plain.react, plain.clip) == (False, False)


def test_make_buckets_counts_per_bucket() -> None:
    items = [
        counted(0.0, "a", react=True),
        counted(3.0, "b"),
        counted(9.99, "a", clip=True),
        counted(10.0, "c", react=True),
        counted(25.0, "c", react=True, clip=True),
        counted(30.0, "late"),  # outside the three buckets
    ]
    buckets = make_buckets(items, 0.0, 10.0, 3, max_per_user=3)
    assert [bucket.t for bucket in buckets] == [0.0, 10.0, 20.0]
    assert [(b.msgs, b.uniq, b.reaction_count, b.clipreq) for b in buckets] == [
        (3, 2, 1, 1),
        (1, 1, 1, 0),
        (1, 1, 1, 1),
    ]
    assert buckets[0].react == pytest.approx(1 / 3) and buckets[1].react == 1.0
    assert buckets[0].value("clipreq") == 1.0 and buckets[0].audio == 0.0


def test_an_empty_bucket_has_zero_reaction_share() -> None:
    assert Bucket(t=0.0).react == 0.0
    assert [b.msgs for b in make_buckets([], 0.0, 10.0, 2, max_per_user=3)] == [0, 0]


def test_each_user_counts_at_most_three_messages_per_bucket() -> None:
    items = [counted(float(i), "spammer", react=True) for i in range(8)] + [counted(8.5, "other")]
    (bucket,) = make_buckets(items, 0.0, 10.0, 1, max_per_user=3)
    assert (bucket.msgs, bucket.uniq, bucket.reaction_count) == (4, 2, 3)
    spread = make_buckets(items, 0.0, 5.0, 2, max_per_user=3)
    assert [b.msgs for b in spread] == [3, 4]  # the cap is per bucket


def test_buckets_can_start_anywhere_for_the_sliding_live_window() -> None:
    items = [counted(1_700_000_000.0 + offset, f"u{offset}") for offset in range(0, 40, 2)]
    buckets = make_buckets(items, 1_700_000_005.0, 10.0, 2, max_per_user=3)
    assert [(b.t, b.msgs) for b in buckets] == [(1_700_000_005.0, 5), (1_700_000_015.0, 5)]


def test_loudness_track_gives_the_jump_against_the_previous_30_seconds() -> None:
    track = LoudnessTrack([(float(t), -30.0) for t in range(70, 100)])
    for t in range(100, 110):
        track.add(float(t), -18.0)
    assert track.jump(100.0, 10.0, 30.0) == pytest.approx(12.0)
    assert track.jump(90.0, 10.0, 30.0) == pytest.approx(0.0)
    assert track.jump(500.0, 10.0, 30.0) == 0.0  # nothing known
    assert track.jump(70.0, 10.0, 30.0) == 0.0  # nothing before it
    track.trim(100.0)
    assert len(track) == 10 and track.jump(100.0, 10.0, 30.0) == 0.0


def test_make_buckets_fills_audio_only_when_loudness_is_given() -> None:
    track = LoudnessTrack([(float(t), -30.0 if t < 10 else -20.0) for t in range(0, 20)])
    with_audio = make_buckets([], 0.0, 10.0, 2, max_per_user=3, loudness=track, audio_ref_s=30.0)
    assert [bucket.audio for bucket in with_audio] == [0.0, pytest.approx(10.0)]
