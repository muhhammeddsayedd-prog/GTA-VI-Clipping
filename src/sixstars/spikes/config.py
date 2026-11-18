"""The spike engine's parameters as one frozen, typed object. `SpikeConfig.from_settings` is the only place
in the engine that reads the `settings` table; everything else takes a `SpikeConfig`. Implemented in M04.

Every number that `docs/16-settings-reference.md` lists under `spikes.*` (plus `live.chat_offset_s`) comes
from the table. The numbers that `docs/05-chat-spike-engine.md` fixes without a settings key are the module
constants below; they are fields of the config too, so the pure functions never use a literal.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Final

from sixstars import settings
from sixstars.chat.filters import MAX_PER_USER_PER_BUCKET, FilterContext, normalise_name
from sixstars.scoring import config as scoring_config

FEATURES: Final[tuple[str, ...]] = ("msgs", "uniq", "react", "clipreq", "audio")

# Fixed by docs/05 (no settings key exists for them).
BASELINE_EXCLUDE_S: Final = 30.0  # the newest part of the trailing window is left out of the baseline
MIN_BASELINE_BUCKETS: Final = 12  # fewer baseline buckets than this: no detection
Z_MIN: Final = -3.0  # each feature z-score is clamped to [Z_MIN, Z_MAX]
Z_MAX: Final = 10.0
REPLAY_MEDIAN_MULTIPLE: Final = 2.0  # replay candidates need msgs >= this x the whole-file median
CHAT_SAMPLE_SIZE: Final = 40  # messages around the peak kept for Claude
AUDIO_REF_S: Final = 30.0  # the audio jump compares a bucket with the loudness of this many seconds before
BOT_LEARN_S: Final = 600.0  # live mode learns bots from the first 10 minutes of chat

SETTING_KEYS: Final[tuple[str, ...]] = (
    "spikes.bucket_s",
    "spikes.live_step_s",
    "spikes.baseline_s",
    "spikes.warmup_s",
    "spikes.min_std",
    "spikes.weights",
    "spikes.z_s",
    "spikes.z_a",
    "spikes.z_b",
    "spikes.s_unique_multiple",
    "spikes.min_messages",
    "spikes.merge_gap_s",
    "spikes.lead_s",
    "spikes.end_z",
    "spikes.tail_s",
    "spikes.min_len_s",
    "spikes.max_len_s",
    "spikes.context_pad_s",
    "spikes.reaction_tokens",
    "spikes.clip_tokens",
    "spikes.ignore_users",
    "live.chat_offset_s",
)


class SpikeConfigError(ValueError):
    """A spike setting is missing, has the wrong JSON type or an impossible value."""


def _number(values: Mapping[str, Any], key: str) -> float:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SpikeConfigError(f"setting {key} must be a number, got {value!r}")
    return float(value)


def _positive(values: Mapping[str, Any], key: str) -> float:
    value = _number(values, key)
    if value <= 0:
        raise SpikeConfigError(f"setting {key} must be greater than 0, got {value!r}")
    return value


def _number_map(values: Mapping[str, Any], key: str) -> dict[str, float]:
    value = values.get(key)
    if not isinstance(value, dict):
        raise SpikeConfigError(f"setting {key} must be an object, got {value!r}")
    numbers: dict[str, float] = {}
    for name, item in value.items():
        if isinstance(item, bool) or not isinstance(item, int | float):
            raise SpikeConfigError(f"setting {key}.{name} must be a number, got {item!r}")
        numbers[str(name)] = float(item)
    return numbers


def _strings(values: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = values.get(key)
    if not isinstance(value, list):
        raise SpikeConfigError(f"setting {key} must be a list, got {value!r}")
    return tuple(str(item) for item in value)


@dataclass(frozen=True, slots=True)
class SpikeConfig:
    """Everything the engine needs to know, read once per scan (replay) or per tick (live)."""

    bucket_s: float
    live_step_s: float
    baseline_s: float
    warmup_s: float
    min_std: Mapping[str, float]
    weights: Mapping[str, float]
    z_s: float
    z_a: float
    z_b: float
    s_unique_multiple: float
    min_messages: float
    merge_gap_s: float
    lead_s: float
    end_z: float
    tail_s: float
    min_len_s: float
    max_len_s: float
    context_pad_s: float
    reaction_tokens: tuple[str, ...]
    clip_tokens: tuple[str, ...]
    ignore_users: frozenset[str]
    chat_offset_s: Mapping[str, float]
    baseline_exclude_s: float = BASELINE_EXCLUDE_S
    min_baseline_buckets: int = MIN_BASELINE_BUCKETS
    z_min: float = Z_MIN
    z_max: float = Z_MAX
    max_per_user: int = MAX_PER_USER_PER_BUCKET
    replay_median_multiple: float = REPLAY_MEDIAN_MULTIPLE
    chat_sample_size: int = CHAT_SAMPLE_SIZE
    audio_ref_s: float = AUDIO_REF_S
    bot_learn_s: float = BOT_LEARN_S
    reaction_burst_min: int = (
        0  # scoring_config reactions.extended: a token repeated this often is a reaction
    )
    youtube_emoji_reactions: bool = False  # scoring_config reactions.extended: ":name:" counts as a reaction
    live_keep_stronger: bool = False  # scoring_config merge.keep_stronger

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "SpikeConfig":
        """Build a config from a `{setting key: JSON value}` mapping holding every key in `SETTING_KEYS`.
        Raises `SpikeConfigError` when a key is missing or has the wrong type."""
        return cls(
            bucket_s=_positive(values, "spikes.bucket_s"),
            live_step_s=_positive(values, "spikes.live_step_s"),
            baseline_s=_positive(values, "spikes.baseline_s"),
            warmup_s=_number(values, "spikes.warmup_s"),
            min_std=_number_map(values, "spikes.min_std"),
            weights=_number_map(values, "spikes.weights"),
            z_s=_number(values, "spikes.z_s"),
            z_a=_number(values, "spikes.z_a"),
            z_b=_number(values, "spikes.z_b"),
            s_unique_multiple=_number(values, "spikes.s_unique_multiple"),
            min_messages=_number(values, "spikes.min_messages"),
            merge_gap_s=_number(values, "spikes.merge_gap_s"),
            lead_s=_number(values, "spikes.lead_s"),
            end_z=_number(values, "spikes.end_z"),
            tail_s=_number(values, "spikes.tail_s"),
            min_len_s=_positive(values, "spikes.min_len_s"),
            max_len_s=_positive(values, "spikes.max_len_s"),
            context_pad_s=_number(values, "spikes.context_pad_s"),
            reaction_tokens=_strings(values, "spikes.reaction_tokens"),
            clip_tokens=_strings(values, "spikes.clip_tokens"),
            ignore_users=frozenset(normalise_name(name) for name in _strings(values, "spikes.ignore_users")),
            chat_offset_s=_number_map(values, "live.chat_offset_s"),
        )

    @classmethod
    def from_settings(cls, overrides: Mapping[str, Any] | None = None) -> "SpikeConfig":
        """Read every spike setting from the `settings` table (30-second cache). `overrides` replaces
        single keys without touching the table; the CLI uses it to try a tuning."""
        values: dict[str, Any] = {key: settings.get(key) for key in SETTING_KEYS}
        values.update(overrides or {})
        return apply_scoring_flags(
            cls.from_mapping(values),
            scoring_config.flag("warmup.v2"),
            scoring_config.flag("reactions.extended"),
            merge=scoring_config.flag("merge.keep_stronger"),
        )

    def filter_context(self, stream_start: float = 0.0, bot_names: Iterable[str] = ()) -> FilterContext:
        """The M03 message filter for one stream, built without another settings read."""
        return FilterContext(
            warmup_s=self.warmup_s,
            ignore_users=self.ignore_users,
            stream_start=stream_start,
            bot_names=frozenset(normalise_name(name) for name in bot_names),
        )

    def offset_for(self, platform: str) -> float:
        """`live.chat_offset_s` for a platform (0 when the platform has no entry)."""
        return float(self.chat_offset_s.get(platform, 0.0))


def apply_scoring_flags(
    cfg: SpikeConfig,
    warmup: Mapping[str, Any] | None,
    reactions: Mapping[str, Any] | None,
    *,
    merge: Mapping[str, Any] | None = None,
) -> SpikeConfig:
    """The config with the enabled scoring flags applied (docs/CHANGELOG-scoring.md). Pure.

    - `warmup.v2`: `warmup_s` replaces `spikes.warmup_s`.
    - `reactions.extended`: `extra_tokens` are added to the reaction tokens, `burst_min` and `youtube_emoji`
      switch on the burst and custom-emoji reaction rules.
    - `merge.keep_stronger`: live mode clips a stronger spike inside the merge gap too (`supersedes_peak_t`).
    """
    changes: dict[str, Any] = {}
    if warmup is not None:
        changes["warmup_s"] = max(0.0, scoring_config.number(warmup, "warmup_s", cfg.warmup_s))
    if reactions is not None:
        extra = [token for token in scoring_config.strings(reactions, "extra_tokens") if token.strip()]
        changes["reaction_tokens"] = tuple(dict.fromkeys([*cfg.reaction_tokens, *extra]))
        changes["reaction_burst_min"] = int(scoring_config.number(reactions, "burst_min", 0.0))
        changes["youtube_emoji_reactions"] = reactions.get("youtube_emoji") is True
    if merge is not None:
        changes["live_keep_stronger"] = True
    return replace(cfg, **changes) if changes else cfg


def from_settings(overrides: Mapping[str, Any] | None = None) -> SpikeConfig:
    """Module-level form of `SpikeConfig.from_settings`."""
    return SpikeConfig.from_settings(overrides)
