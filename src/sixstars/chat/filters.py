"""Filters that drop chat noise before spike counting: known bots, system notices, subscription and raid
messages. Implemented in M03.

All functions here are pure. `FilterContext.from_settings` is the only place that reads the `settings`
table (`spikes.warmup_s`, `spikes.ignore_users`). Rules: `docs/05-chat-spike-engine.md` section 2.
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from sixstars import settings
from sixstars.chat.models import ChatMessage

# Bot-learning and spam-control numbers from `docs/05-chat-spike-engine.md` section 2. They are defaults of
# function parameters, not runtime settings: `docs/16-settings-reference.md` has no keys for them.
BOT_MIN_MESSAGES: Final = 20
BOT_SAME_PREFIX_RATIO: Final = 0.8
BOT_PREFIX_CHARS: Final = 20
BOT_NAME_SUFFIX: Final = "bot"
MAX_PER_USER_PER_BUCKET: Final = 3


def normalise_name(username: str) -> str:
    """Return a username in the form used for bot lists: trimmed, lowercase, without a leading `@`
    (YouTube shows handles as `@Name`)."""
    return username.strip().lstrip("@").lower()


@dataclass(frozen=True, slots=True)
class FilterContext:
    """What `is_counted` needs to know about one stream.

    `stream_start` is on the same clock as `ChatMessage.t`: the unix time the stream started (live), or 0
    (replay). `ignore_users` and `bot_names` hold names as made by `normalise_name`.
    """

    warmup_s: float
    ignore_users: frozenset[str]
    stream_start: float = 0.0
    bot_names: frozenset[str] = frozenset()

    @classmethod
    def from_settings(cls, stream_start: float = 0.0, bot_names: Iterable[str] = ()) -> "FilterContext":
        """Build a context from the `spikes.warmup_s` and `spikes.ignore_users` settings."""
        return cls(
            warmup_s=settings.get_float("spikes.warmup_s"),
            ignore_users=frozenset(
                normalise_name(str(name)) for name in settings.get_list("spikes.ignore_users")
            ),
            stream_start=stream_start,
            bot_names=frozenset(normalise_name(name) for name in bot_names),
        )

    def with_bots(self, bot_names: Iterable[str]) -> "FilterContext":
        """Return a copy whose `bot_names` also contains `bot_names`."""
        merged = self.bot_names | frozenset(normalise_name(name) for name in bot_names)
        return FilterContext(self.warmup_s, self.ignore_users, self.stream_start, merged)


def is_counted(msg: ChatMessage, ctx: FilterContext) -> bool:
    """True when the message counts towards chat activity.

    Dropped: anything that is not plain chat (subs, gifts, raids, donations, notices), the broadcaster's
    own messages, ignored users and learned bots, empty text, and messages in the warm-up period at the
    start of the stream.
    """
    if msg.kind != "chat" or msg.is_broadcaster:
        return False
    if not msg.text.strip():
        return False
    if msg.t < ctx.stream_start + ctx.warmup_s:
        return False
    name = normalise_name(msg.username)
    return name not in ctx.ignore_users and name not in ctx.bot_names


def learn_bots(
    messages: Iterable[ChatMessage],
    *,
    min_messages: int = BOT_MIN_MESSAGES,
    same_prefix_ratio: float = BOT_SAME_PREFIX_RATIO,
    prefix_chars: int = BOT_PREFIX_CHARS,
) -> set[str]:
    """Return the normalised names of users that behave like bots.

    A user is a bot when the name ends in "bot", or when they posted at least `min_messages` chat messages
    of which at least `same_prefix_ratio` start with the same `prefix_chars` characters. Live sessions call
    this on the first 10 minutes of chat, replays on the whole file; the result goes into
    `live_sessions.bot_names` / `chat_files.bot_names`.
    """
    prefixes: dict[str, Counter[str]] = {}
    bots: set[str] = set()
    for message in messages:
        name = normalise_name(message.username)
        if not name:
            continue
        if name.endswith(BOT_NAME_SUFFIX):
            bots.add(name)
        if message.kind == "chat":
            prefixes.setdefault(name, Counter())[message.text.strip()[:prefix_chars]] += 1
    for name, counts in prefixes.items():
        total = counts.total()
        if total >= min_messages and counts.most_common(1)[0][1] / total >= same_prefix_ratio:
            bots.add(name)
    return bots


def cap_per_user(
    messages_in_bucket: Iterable[ChatMessage], max_per_user: int = MAX_PER_USER_PER_BUCKET
) -> list[ChatMessage]:
    """Keep at most `max_per_user` messages per user (the first ones, in order) from one bucket."""
    kept: list[ChatMessage] = []
    per_user: Counter[str] = Counter()
    for message in messages_in_bucket:
        if per_user[message.user_id] < max_per_user:
            per_user[message.user_id] += 1
            kept.append(message)
    return kept
