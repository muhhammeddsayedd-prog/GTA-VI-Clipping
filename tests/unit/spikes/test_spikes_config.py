"""`SpikeConfig`: every spike threshold comes from settings, with type checks."""

import re
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from sixstars import settings
from sixstars.spikes import config as spike_config
from sixstars.spikes.config import SETTING_KEYS, SpikeConfig, SpikeConfigError

REPO = Path(__file__).resolve().parents[3]
# Settings read outside `SpikeConfig`: the Claude confirmation step (M05) and the replay end guard, which
# the VOD lane (M08) reads and passes to `scan_replay(end_guard_s=...)`.
CONFIRMATION_KEYS = {"spikes.s_min_confidence", "spikes.ab_min_confidence", "spikes.end_guard_s"}


def test_seeded_values_are_read_as_documented(cfg: SpikeConfig) -> None:
    assert (cfg.bucket_s, cfg.live_step_s, cfg.baseline_s, cfg.warmup_s) == (10.0, 5.0, 600.0, 300.0)
    assert (cfg.z_s, cfg.z_a, cfg.z_b, cfg.end_z) == (4.0, 3.0, 2.5, 1.5)
    assert (cfg.s_unique_multiple, cfg.min_messages, cfg.merge_gap_s) == (3.0, 15.0, 60.0)
    assert (cfg.lead_s, cfg.tail_s, cfg.min_len_s, cfg.max_len_s, cfg.context_pad_s) == (20, 5, 15, 60, 15)
    assert cfg.weights == {"msgs": 0.4, "uniq": 0.25, "react": 0.2, "clipreq": 0.1, "audio": 0.05}
    assert cfg.min_std == {"msgs": 2.0, "uniq": 1.5, "react": 0.05, "clipreq": 0.5, "audio": 1.5}
    assert "KEKW" in cfg.reaction_tokens and "clip it" in cfg.clip_tokens
    assert "nightbot" in cfg.ignore_users
    assert cfg.chat_offset_s == {"twitch": 0.0, "youtube": 0.0}
    assert (cfg.baseline_exclude_s, cfg.min_baseline_buckets, cfg.z_min, cfg.z_max) == (30.0, 12, -3.0, 10.0)


def test_every_documented_spike_setting_is_read_from_settings(seed_values: dict[str, Any]) -> None:
    documented = set(
        re.findall(r"\| `(spikes\.[a-z_]+)` \|", (REPO / "docs/16-settings-reference.md").read_text("utf-8"))
    )
    assert documented - CONFIRMATION_KEYS == {key for key in SETTING_KEYS if key.startswith("spikes.")}
    assert set(SETTING_KEYS) <= set(seed_values)


def test_config_is_frozen(cfg: SpikeConfig) -> None:
    with pytest.raises(FrozenInstanceError):
        cfg.z_b = 1.0  # type: ignore[misc]


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("spikes.z_b", "2.5"),
        ("spikes.z_b", True),
        ("spikes.bucket_s", 0),
        ("spikes.weights", [0.4]),
        ("spikes.weights", {"msgs": "a lot"}),
        ("spikes.reaction_tokens", "LOL"),
        ("live.chat_offset_s", 3),
    ],
)
def test_wrong_types_are_rejected(seed_values: dict[str, Any], key: str, bad: Any) -> None:
    with pytest.raises(SpikeConfigError, match=re.escape(key.split(".")[0])):
        SpikeConfig.from_mapping({**seed_values, key: bad})


def test_a_missing_key_is_rejected(seed_values: dict[str, Any]) -> None:
    values = {key: value for key, value in seed_values.items() if key != "spikes.lead_s"}
    with pytest.raises(SpikeConfigError, match="spikes.lead_s"):
        SpikeConfig.from_mapping(values)


def test_from_settings_reads_each_key_once_and_applies_overrides(
    seed_values: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []

    def fake_get(key: str) -> Any:
        asked.append(key)
        return seed_values[key]

    monkeypatch.setattr(settings, "get", fake_get)
    cfg = spike_config.from_settings({"spikes.lead_s": 25})
    assert sorted(asked) == sorted(SETTING_KEYS)
    assert cfg.lead_s == 25.0 and cfg.tail_s == 5.0


def test_filter_context_and_offsets(cfg: SpikeConfig) -> None:
    ctx = cfg.filter_context(1000.0, ["@SpamGuy"])
    assert (ctx.warmup_s, ctx.stream_start, ctx.bot_names) == (300.0, 1000.0, frozenset({"spamguy"}))
    assert ctx.ignore_users == cfg.ignore_users
    assert cfg.offset_for("twitch") == 0.0 and cfg.offset_for("kick") == 0.0
