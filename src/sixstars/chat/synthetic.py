"""Synthetic chat generators for tests of the filters and the spike engine: flat chat, a burst, a bot
flood, a sub train and a raid. Added in M03 (the milestone file names `tests/fixtures/synthetic_chat.py`;
the generators live in the package so every test directory and the CLI can import them).

Every generator is deterministic: the same arguments and `seed` give the same messages. Times are on
whatever clock the caller chooses (seconds from 0 for a replay, unix seconds for live). Results are sorted
by time; combine several with `merge`.
"""

import random
from collections.abc import Iterable, Sequence
from typing import Final

from sixstars.chat.models import ChatMessage, Kind, Platform, sort_messages

CALM_TEXTS: Final[tuple[str, ...]] = (
    "hello chat",
    "what server is this",
    "good stream today",
    "where is he going",
    "that car is nice",
    "anyone know the song",
    "first time here",
    "he needs a new plan",
)
REACTION_TEXTS: Final[tuple[str, ...]] = ("LMAO", "NO WAY", "OMG", "KEKW", "WTF", "CLIP IT", "💀💀💀", "LOL")


def _message(platform: Platform, t: float, user: str, text: str, kind: Kind = "chat") -> ChatMessage:
    return ChatMessage(platform=platform, t=t, user_id=f"id_{user}", username=user, text=text, kind=kind)


def flat_chat(
    duration_s: float,
    msgs_per_10s: float,
    *,
    users: int = 50,
    start: float = 0.0,
    seed: int = 1,
    platform: Platform = "twitch",
    texts: Sequence[str] = CALM_TEXTS,
) -> list[ChatMessage]:
    """Steady chat: `msgs_per_10s` messages in every 10-second span of `[start, start + duration_s)`,
    at random moments inside the span, written by random members of a pool of `users` viewers."""
    rng = random.Random(seed)
    messages: list[ChatMessage] = []
    spans = int(duration_s // 10)
    carry = 0.0
    for span in range(spans):
        carry += msgs_per_10s
        count = int(carry)
        carry -= count
        for _ in range(count):
            t = start + span * 10 + rng.random() * 10
            messages.append(_message(platform, t, f"viewer{rng.randrange(users)}", rng.choice(texts)))
    return sort_messages(messages)


def burst(
    at: float,
    messages: int,
    users: int,
    *,
    spread_s: float = 8.0,
    seed: int = 2,
    platform: Platform = "twitch",
    texts: Sequence[str] = REACTION_TEXTS,
    user_prefix: str = "hype",
) -> list[ChatMessage]:
    """A reaction burst: `messages` messages from `users` distinct users inside `[at, at + spread_s)`.

    Users take turns, so every user posts at least once when `messages >= users`.
    """
    rng = random.Random(seed)
    return sort_messages(
        _message(platform, at + rng.random() * spread_s, f"{user_prefix}{index % users}", rng.choice(texts))
        for index in range(messages)
    )


def bot_flood(
    start: float,
    duration_s: float,
    *,
    every_s: float = 2.0,
    name: str = "dealsbot9000x",
    text: str = "Follow the channel and join the giveaway at example dot com !giveaway",
    platform: Platform = "twitch",
) -> list[ChatMessage]:
    """One account repeating the same message every `every_s` seconds for `duration_s` seconds.

    The default name does not end in "bot", so only the repeated-text rule can catch it.
    """
    count = int(duration_s // every_s)
    return [
        _message(platform, start + index * every_s, name, f"{text} #{index % 3}") for index in range(count)
    ]


def sub_train(
    at: float,
    count: int,
    *,
    spread_s: float = 10.0,
    seed: int = 3,
    platform: Platform = "twitch",
    gift_every: int = 4,
) -> list[ChatMessage]:
    """`count` subscription notices inside `[at, at + spread_s)`; every `gift_every`-th one is a gift."""
    rng = random.Random(seed)
    messages: list[ChatMessage] = []
    for index in range(count):
        kind: Kind = "gift" if gift_every > 0 and index % gift_every == 0 else "sub"
        text = "gifted a Tier 1 sub" if kind == "gift" else "subscribed at Tier 1"
        messages.append(_message(platform, at + rng.random() * spread_s, f"subber{index}", text, kind))
    return sort_messages(messages)


def raid(
    at: float,
    viewers: int,
    *,
    raider: str = "bigraider",
    greeting: str = "RAID RAID RAID",
    spread_s: float = 15.0,
    seed: int = 4,
    platform: Platform = "twitch",
) -> list[ChatMessage]:
    """A raid: one `raid` notice at `at`, then one greeting from each of `viewers` arriving users inside
    `[at, at + spread_s)`. The greetings are plain chat, as they are on the real platforms."""
    rng = random.Random(seed)
    messages = [_message(platform, at, raider, f"{viewers} raiders from {raider} have joined!", "raid")]
    messages += [
        _message(platform, at + rng.random() * spread_s, f"raider{index}", greeting)
        for index in range(viewers)
    ]
    return sort_messages(messages)


def merge(*groups: Iterable[ChatMessage]) -> list[ChatMessage]:
    """Combine several generated lists into one list sorted by time."""
    return sort_messages(message for group in groups for message in group)
