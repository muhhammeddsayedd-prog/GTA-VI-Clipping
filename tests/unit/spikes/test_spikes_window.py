"""Clip window maths, merging and the live cool-down."""

from dataclasses import replace

import pytest

from sixstars.spikes.config import SpikeConfig
from sixstars.spikes.window import clip_window, in_cooldown, merge_by_peak


def test_window_starts_lead_s_before_the_first_spike_bucket(cfg: SpikeConfig) -> None:
    window = clip_window(990.0, 1010.0, cfg)  # first bucket 990, peak bucket 1000, score fell at 1010
    assert (window.start, window.end) == (970.0, 1015.0)  # 990 - 20, 1010 + 5
    assert window.spike_floor == 990.0
    assert (window.pad_start, window.pad_end) == (955.0, 1030.0)


@pytest.mark.parametrize(
    ("first_t", "end_t", "expected"),
    [
        (990.0, 1000.0, (970.0, 1005.0)),  # 35 s: inside the limits
        (990.0, 960.0, (970.0, 985.0)),  # shorter than min_len_s 15: stretched to 15
        (990.0, 1100.0, (970.0, 1030.0)),  # longer than max_len_s 60: keeps the start
        (990.0, None, (970.0, 1030.0)),  # cut at the maximum length
        (990.0, 1025.0, (970.0, 1030.0)),  # exactly 60 s
    ],
)
def test_window_length_is_clamped(
    cfg: SpikeConfig, first_t: float, end_t: float | None, expected: tuple[float, float]
) -> None:
    window = clip_window(first_t, end_t, cfg)
    assert (window.start, window.end) == expected
    assert cfg.min_len_s <= window.end - window.start <= cfg.max_len_s
    assert window.start <= window.spike_floor == first_t


def test_lead_and_chat_offset_come_from_the_config(cfg: SpikeConfig) -> None:
    assert clip_window(990.0, 1010.0, replace(cfg, lead_s=30.0)).start == 960.0
    shifted = clip_window(990.0, 1010.0, cfg, chat_offset_s=4.0)
    assert (shifted.start, shifted.end, shifted.spike_floor) == (966.0, 1015.0, 990.0)


def test_window_never_starts_before_the_stream(cfg: SpikeConfig) -> None:
    window = clip_window(10.0, 30.0, cfg, earliest=0.0)
    assert (window.start, window.end, window.pad_start) == (0.0, 35.0, 0.0)
    live = clip_window(1_700_000_010.0, 1_700_000_030.0, cfg, earliest=1_700_000_000.0)
    assert live.start == 1_700_000_000.0


def merged(
    spikes: list[tuple[float, float]], gap: float = 60.0
) -> list[tuple[tuple[float, float], list[tuple[float, float]]]]:
    return merge_by_peak(spikes, gap, peak_t=lambda spike: spike[0], score=lambda spike: spike[1])


@pytest.mark.parametrize(
    ("spikes", "expected"),
    [
        ([], []),
        ([(100.0, 5.0)], [((100.0, 5.0), [])]),
        ([(100.0, 5.0), (140.0, 3.0)], [((100.0, 5.0), [(140.0, 3.0)])]),  # 40 s apart: the higher stays
        ([(100.0, 3.0), (140.0, 5.0)], [((140.0, 5.0), [(100.0, 3.0)])]),
        ([(100.0, 5.0), (160.0, 3.0)], [((100.0, 5.0), []), ((160.0, 3.0), [])]),  # exactly the gap: apart
        ([(140.0, 5.0), (100.0, 3.0)], [((140.0, 5.0), [(100.0, 3.0)])]),  # input order does not matter
        (
            [(100.0, 3.0), (140.0, 5.0), (180.0, 4.0), (400.0, 2.6)],
            [((140.0, 5.0), [(100.0, 3.0), (180.0, 4.0)]), ((400.0, 2.6), [])],
        ),
    ],
)
def test_merge_keeps_the_higher_of_close_peaks(
    spikes: list[tuple[float, float]],
    expected: list[tuple[tuple[float, float], list[tuple[float, float]]]],
) -> None:
    assert merged(spikes) == expected


def test_cooldown_after_an_emitted_live_spike() -> None:
    assert not in_cooldown(1000.0, None, 60.0)
    assert in_cooldown(1040.0, 1000.0, 60.0)
    assert not in_cooldown(1060.0, 1000.0, 60.0)
