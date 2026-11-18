"""Unit tests for the Phase 1 scoring overhaul (docs/CHANGELOG-scoring.md): pure logic only, no network or
database. Every behaviour is checked with its flag on, and the old behaviour with it off."""

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from gate_helpers import make_settings

from sixstars.ai.schemas import SpikeVerdict
from sixstars.ai.spike_confirm import TierDecision, decide_fast, non_content
from sixstars.chat import synthetic
from sixstars.factory.anchor import CutRules, find_anchor, guidance
from sixstars.factory.config import FactoryConfig, with_cut_rules
from sixstars.factory.lines import Word
from sixstars.gate.overhaul import lowered_hourly_score, lowered_lengths
from sixstars.live.session import fast_path_reason
from sixstars.scoring import firsttime
from sixstars.scoring.percentile import Strength, percentile_of
from sixstars.spikes import DetectedSpike, LiveSpikeDetector
from sixstars.spikes.baseline import Baseline, median_baseline, to_json
from sixstars.spikes.buckets import TokenMatcher, is_burst, tokenise
from sixstars.spikes.config import SpikeConfig, apply_scoring_flags

FIRST_TIME_FLAG: dict[str, Any] = {
    "enabled": True,
    "score_points": 1.0,
    "rank_points": 0.5,
    "phrases": ["first time", "first ever", "finally"],
    "event_types": {"car_theft": ["steal", "stole", "carjack"], "death": ["wasted", "i died"]},
}


SEED_SQL = Path(__file__).resolve().parents[3] / "supabase" / "migrations" / "0002_seed_settings.sql"
_ROW_RE = re.compile(r"^\s*\('([\w.]+)', '(.*)'::jsonb, '", re.MULTILINE)


@pytest.fixture
def cfg() -> SpikeConfig:
    text = SEED_SQL.read_text(encoding="utf-8")
    return SpikeConfig.from_mapping(
        {key: json.loads(raw.replace("''", "'")) for key, raw in _ROW_RE.findall(text)}
    )


# ── percentiles ─────────────────────────────────────────────


def test_percentile_counts_ties_half_and_needs_history() -> None:
    assert percentile_of(5.0, []) is None
    assert percentile_of(10.0, [1, 2, 3, 4]) == 100.0
    assert percentile_of(0.0, [1, 2, 3, 4]) == 0.0
    assert percentile_of(2.0, [1, 2, 3, 4]) == 37.5
    assert Strength(97.5, "channel", 40).at_least(97)
    assert not Strength(None, "none", 0).at_least(0)


# ── reactions ───────────────────────────────────────────────


def test_burst_and_custom_emoji_rules_only_when_on() -> None:
    plain = TokenMatcher.from_tokens(["LOL"])
    extended = TokenMatcher.from_tokens(
        ["LOL", "HAHA*", "XD", "???", "pepeLaugh"], burst_min=3, custom_emoji=True
    )
    assert not plain.matches("GIGACHAD GIGACHAD GIGACHAD")
    assert extended.matches("GIGACHAD GIGACHAD GIGACHAD")
    assert not extended.matches("GIGACHAD GIGACHAD")
    assert extended.matches(":face-blue-smiling:") and not plain.matches(":face-blue-smiling:")
    assert extended.matches("HAHAHAHAHA") and extended.matches("xDDD") and extended.matches("wait ????")
    assert extended.matches("pepeLaugh")
    assert is_burst(tokenise("W W W"), 3) and not is_burst(tokenise("W L W"), 3)


def test_scoring_flags_change_the_spike_config_only_when_enabled(cfg: SpikeConfig) -> None:
    assert apply_scoring_flags(cfg, None, None) is cfg
    changed = apply_scoring_flags(
        cfg,
        {"enabled": True, "warmup_s": 120},
        {"enabled": True, "extra_tokens": ["ICANT", "LOL"], "burst_min": 3, "youtube_emoji": True},
        merge={"enabled": True},
    )
    assert changed.warmup_s == 120
    assert "ICANT" in changed.reaction_tokens and changed.reaction_tokens.count("LOL") <= 1
    assert changed.reaction_burst_min == 3 and changed.youtube_emoji_reactions and changed.live_keep_stronger


# ── stored baselines and the 2-minute warm-up ───────────────


def test_median_baseline_takes_each_feature_median_and_floors_std() -> None:
    stored = [
        to_json(Baseline(60, {"msgs": 10, "uniq": 8}, {"msgs": 3, "uniq": 0.1}), 0),
        to_json(Baseline(60, {"msgs": 20, "uniq": 9}, {"msgs": 5, "uniq": 0.1}), 0),
        to_json(Baseline(60, {"msgs": 30, "uniq": 10}, {"msgs": 7, "uniq": 0.1}), 0),
        "garbage",
    ]
    prior = median_baseline(stored, {"uniq": 1.5})
    assert prior is not None and prior.n == 3
    assert prior.mean["msgs"] == 20 and prior.std["msgs"] == 5 and prior.std["uniq"] == 1.5
    assert median_baseline([], {}) is None


def _run_live(detector: LiveSpikeDetector, chat: list[Any], until: float, step: float) -> list[DetectedSpike]:
    spikes: list[DetectedSpike] = []
    index, now = 0, 0.0
    while now <= until:
        while index < len(chat) and chat[index].t < now:
            detector.feed(chat[index])
            index += 1
        spikes += detector.tick(now)
        now += step
    return spikes


def test_a_countdown_spike_two_minutes_in_is_found_with_a_stored_baseline(cfg: SpikeConfig) -> None:
    short = replace(cfg, warmup_s=120.0)
    chat = synthetic.merge(synthetic.flat_chat(600, 5), synthetic.burst(160.0, 150, 100))
    without = _run_live(
        LiveSpikeDetector("twitch", 0.0, config=short, listen_start=0.0), chat, 400, cfg.live_step_s
    )
    assert without == []  # 40 s of own history: no baseline yet
    prior = Baseline(60, {"msgs": 5, "uniq": 4, "react": 0.05, "clipreq": 0, "audio": 0}, dict(cfg.min_std))
    detector = LiveSpikeDetector("twitch", 0.0, config=short, listen_start=0.0, prior=prior)
    found = _run_live(detector, chat, 400, cfg.live_step_s)
    assert len(found) == 1 and abs(found[0].peak_t - 160.0) <= cfg.bucket_s
    assert detector.prior_used > 0


# ── merging keeps the stronger live spike ───────────────────


def test_a_stronger_live_spike_inside_the_gap_supersedes_the_first(cfg: SpikeConfig) -> None:
    chat = synthetic.merge(
        synthetic.flat_chat(1500, 5),
        synthetic.burst(1200.0, 16, 10, seed=1, user_prefix="a"),
        synthetic.burst(1240.0, 300, 220, seed=2, user_prefix="b"),
    )
    old = _run_live(
        LiveSpikeDetector("twitch", 0.0, config=cfg, listen_start=0.0), chat, 1400, cfg.live_step_s
    )
    assert len(old) == 1 and old[0].supersedes_peak_t is None
    keep = replace(cfg, live_keep_stronger=True)
    new = _run_live(
        LiveSpikeDetector("twitch", 0.0, config=keep, listen_start=0.0), chat, 1400, cfg.live_step_s
    )
    assert len(new) == 2
    assert new[1].score_z > new[0].score_z and new[1].supersedes_peak_t == new[0].peak_t


# ── fast path ───────────────────────────────────────────────


def _verdict(**kwargs: Any) -> SpikeVerdict:
    values: dict[str, Any] = {
        "is_moment": False,
        "confidence": 0.4,
        "moment_type": "other",
        "one_line": "chat reacts, unclear what happened",
        "noise_reason": "chat conversation",
    }
    return SpikeVerdict(**{**values, **kwargs})


def test_fast_path_overrides_a_chat_only_reject_but_not_a_giveaway() -> None:
    rejected = TierDecision("rejected", "rejected", 0.4, "chat conversation")
    fast = decide_fast(_verdict(), rejected, ["giveaway", "raid"], "p98 of channel")
    assert fast.status == "confirmed" and fast.tier == "S" and "fast path" in fast.reason
    giveaway = _verdict(noise_reason="giveaway or command spam")
    assert non_content(giveaway, ["giveaway"]) == "giveaway"
    assert decide_fast(giveaway, rejected, ["giveaway"], "p98") is rejected


def test_fast_path_reason_needs_the_flag_and_a_strong_spike() -> None:
    spike = DetectedSpike(
        mode="live", peak_t=0, first_t=0, score_z=5.0, parts={}, msgs=50, uniq=40, reaction_count=30,
        clip_requests=0, tier="A", window_start=0, window_end=30, spike_floor=0, pad_start=0, pad_end=30,
        chat_sample=[],
    )  # fmt: skip
    flag = {"enabled": True, "percentile": 97, "include_s_tier": True}
    assert fast_path_reason(spike, Strength(99.0, "channel", 50), None) is None
    assert fast_path_reason(spike, Strength(99.0, "channel", 50), flag) == "p99 of channel"
    assert fast_path_reason(spike, Strength(50.0, "channel", 50), flag) is None
    assert fast_path_reason(replace(spike, tier="S"), Strength(None, "none", 0), flag) == "provisional tier S"


# ── first-time boost ────────────────────────────────────────


def test_first_time_phrase_and_first_event_give_points() -> None:
    by_phrase = firsttime.decide(
        FIRST_TIME_FLAG, transcript_text="okay", chat_texts=["FIRST TIME EVER LUL"], topic="", seen_events=[]
    )
    assert (
        by_phrase.phrases == ("first time",)
        and by_phrase.score_points == 1.0
        and by_phrase.rank_points == 0.5
    )
    first_car = firsttime.decide(
        FIRST_TIME_FLAG, transcript_text="bro I stole it", chat_texts=[], topic="", seen_events=["death"]
    )
    assert first_car.first_events == ("car_theft",) and first_car.applies
    seen = firsttime.decide(
        FIRST_TIME_FLAG,
        transcript_text="",
        chat_texts=[],
        topic="streamer-steal-car-cops",
        seen_events=["car_theft"],
    )
    assert seen.event_types == ("car_theft",) and not seen.applies and seen.score_points == 0.0
    assert not firsttime.find_phrases(["the firsts timer"], ["first time"])


# ── adaptive cut ────────────────────────────────────────────


def test_anchor_lands_on_the_loud_second_near_the_spike() -> None:
    loudness = [(t / 10, -30.0) for t in range(600)] + [(t / 10, -8.0) for t in range(330, 345)]
    words = [Word("go", float(t), float(t) + 0.3) for t in range(20, 50)]
    chat = [36.0 + i * 0.1 for i in range(40)]
    found = find_anchor(60.0, 35.0, loudness=loudness, chat_times=chat, words=words)
    assert found is not None and 33.0 <= found.t_s <= 35.0
    assert find_anchor(60.0, 35.0, loudness=[], chat_times=[], words=[]) is None


def test_cut_rules_and_guidance() -> None:
    rules = CutRules.from_flag({"min_s": 6, "max_s": 25, "target_min_s": 12, "target_max_s": 18})
    assert (rules.min_s, rules.max_s, rules.lead_s, rules.tail_s) == (6, 25, (1.0, 3.0), (1.0, 3.0))
    text = guidance(rules, None)
    assert "12-18 s" in text and "6 s" in text and "25 s" in text and "loudest" not in text
    cfg = FactoryConfig.offline("hourly")
    assert with_cut_rules(cfg, None) is cfg
    short = with_cut_rules(cfg, {"enabled": True, "min_s": 6, "max_s": 25})
    assert (short.min_len_s, short.max_len_s) == (6, 25) and short.cut_rules is not None


def test_gate_lowering_never_raises_a_limit() -> None:
    settings = make_settings()
    lower = lowered_lengths(settings, 6)
    assert lower.hourly.min_len_s == 6 and lower.express.min_len_s == 6
    assert lower.hourly.max_len_s == settings.hourly.max_len_s
    assert lowered_lengths(settings, 1000).hourly.min_len_s == settings.hourly.min_len_s
    assert lowered_hourly_score(settings, 5).hourly.min_score == min(5, settings.hourly.min_score)
    assert lowered_hourly_score(settings, 5).express.min_score == settings.express.min_score
