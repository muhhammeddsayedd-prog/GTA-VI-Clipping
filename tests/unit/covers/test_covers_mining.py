"""Cover engine C3 (docs/29a stages 1-2): peak, frame times, scoring maths and caption judging. No network,
no MediaPipe."""

import pytest

from sixstars.covers import miner, ocr, scorer
from sixstars.covers.peak import choose_peak
from sixstars.factory.layout import Box

WEIGHTS = {"expression": 0.35, "face_area": 0.20, "sharpness": 0.15, "exposure": 0.10, "gameplay": 0.20}


# ── peak ─────────────────────────────────────────────────────


def test_peak_prefers_the_anchor_then_the_payoff_then_chat() -> None:
    chat = [9.0, 9.2, 9.4, 9.6]  # busiest second 9 -> 6.5 after the 3 s lag
    assert choose_peak(20.0, anchor_s=4.0, payoff_s=8.0, chat_times=chat) == (4.0, "anchor")
    assert choose_peak(20.0, anchor_s=25.0, payoff_s=8.0, chat_times=chat) == (8.0, "payoff")
    assert choose_peak(20.0, anchor_s=None, payoff_s=-1.0, chat_times=chat) == (6.5, "chat")
    assert choose_peak(20.0, chat_times=[9.0, 9.5]) == (None, "")


# ── frame times ──────────────────────────────────────────────


def test_sample_times_are_dense_around_the_peak() -> None:
    times = miner.sample_times_ms(10.0, 5.0)
    window = [t for t in times if 3000 <= t < 7000]
    assert window == list(range(3000, 7000, 250))
    assert [t for t in times if t < 3000] == [500, 1500, 2500]
    assert all(0 <= t < 10000 for t in times) and times == sorted(set(times))
    assert miner.sample_times_ms(3.0, None) == [500, 1500, 2500]
    assert miner.frame_index(2750) == 11


def test_manifest_round_trip() -> None:
    manifest = miner.Manifest(
        "c1",
        1.5,
        20.0,
        1920,
        1080,
        Box(1440, 720, 480, 360),
        None,
        7.2,
        "funny",
        "spike",
        "/data/ready/c1.mp4",
    )
    assert miner.Manifest.from_json(manifest.to_json()) == manifest


# ── scorer ───────────────────────────────────────────────────


def test_expression_and_eyes() -> None:
    assert scorer.expression_score({"jawOpen": 0.7}) == pytest.approx(0.7)
    assert scorer.expression_score({"jawOpen": 0.6, "browInnerUp": 0.5}) == pytest.approx(0.75)
    assert scorer.expression_score({"mouthSmileLeft": 1.0, "mouthSmileRight": 1.0, "jawOpen": 0.9}) == 1.0
    assert scorer.eyes_open_score({"eyeBlinkLeft": 0.8, "eyeBlinkRight": 0.6}) == pytest.approx(0.3)


def test_exposure_and_brightness() -> None:
    assert scorer.exposure_score(120, 0.0) == 1.0
    assert scorer.exposure_score(60, 0.0) == pytest.approx(0.5)
    assert scorer.exposure_score(220, 0.0) == pytest.approx(0.5)
    assert scorer.exposure_score(120, 0.05) == pytest.approx(0.5)
    assert scorer.exposure_score(10, 0.0) == 0.0
    assert scorer.brightness_factor(80) == 1.0 and scorer.brightness_factor(40) == 0.5
    assert scorer.brightness_factor(10) == 0.0


def test_percentile_ranks() -> None:
    assert scorer.percentile_ranks([3.0, 1.0, 2.0]) == [1.0, 0.0, 0.5]
    assert scorer.percentile_ranks([5.0]) == [1.0]


def _face(
    expression: float = 0.5, eyes: float = 0.9, sharp: float = 100.0, **kw: object
) -> scorer.FaceFeatures:
    values: dict[str, object] = {
        "found": True,
        "area": 0.4,
        "eyes_open": eyes,
        "expression": expression,
        "sharpness": sharp,
        "luma": 120.0,
        "clipped": 0.0,
        "box": (10, 10, 50, 60),
    }
    values.update(kw)
    return scorer.FaceFeatures(**values)  # type: ignore[arg-type]


GAME = scorer.GameFeatures(detail=0.1, luma=100.0, change=0.05)


def test_score_frames_rejects_and_ranks() -> None:
    items = [
        (0, _face(expression=0.9, sharp=200.0), GAME),
        (250, _face(expression=0.2, sharp=150.0), GAME),
        (500, _face(eyes=0.2, sharp=180.0), GAME),
        (750, scorer.FaceFeatures(found=False), GAME),
        (1000, _face(sharp=1.0), GAME),
        (1250, _face(edge_cut=True, sharp=170.0), GAME),
    ]
    scored = {frame.t_ms: frame for frame in scorer.score_frames(items, WEIGHTS)}
    assert scored[500].rejected == "eyes_closed"
    assert scored[750].rejected == "no_face"
    assert scored[1000].rejected == "blur"
    assert scored[1250].rejected == "face_cut"
    assert scored[0].rejected is None and scored[0].score > scored[250].score
    top = scorer.top_frames(list(scored.values()), 5, min_gap_ms=200)
    assert [frame.t_ms for frame in top] == [0, 250]


def test_top_frames_keep_a_gap() -> None:
    items = [(t, _face(expression=1.0 - t / 10000, sharp=100.0 + t), GAME) for t in (0, 250, 500, 750, 1000)]
    top = scorer.top_frames(scorer.score_frames(items, WEIGHTS), 5, min_gap_ms=500)
    gaps = [abs(a.t_ms - b.t_ms) for a in top for b in top if a is not b]
    assert all(gap >= 500 for gap in gaps)


def test_propose_layout() -> None:
    assert scorer.propose_layout(0.2, 0.4, "funny") == "face_led"
    assert scorer.propose_layout(0.5, 0.4, "emotional") == "face_led"
    assert scorer.propose_layout(0.8, 0.2, "chaos") == "moment_led"
    assert scorer.propose_layout(0.8, 0.4, "chaos") == "split"
    assert scorer.propose_layout(0.5, 0.4, None) == "split"


def test_rendered_score() -> None:
    assert scorer.rendered_score(0.5, phrase=True, cut=False) == pytest.approx(0.8)
    assert scorer.rendered_score(0.5, phrase=False, cut=True) == pytest.approx(0.0)


# ── caption words ────────────────────────────────────────────

VOCAB = sorted(
    {ocr.normalise(w) for w in ["the", "suspect's", "car", "is", "gone", "right", "just", "oh", "god"]}
)


def test_token_status() -> None:
    assert ocr.token_status("SUSPECT'S", VOCAB) == "ok"
    assert ocr.token_status("GONE!", VOCAB) == "ok"
    assert ocr.token_status("G0NE", VOCAB) == "ok"  # OCR slip
    assert ocr.token_status("SUSPE", VOCAB) == "cut"
    assert ocr.token_status("ST", VOCAB) == "cut"  # the tail of "just"
    assert ocr.token_status("F*CK", VOCAB) == "masked"
    assert ocr.token_status("X2", VOCAB) == "unknown"


def test_caption_verdict() -> None:
    assert ocr.caption_verdict(["THE", "CAR", "IS", "GONE"], VOCAB) == (True, False)
    assert ocr.caption_verdict(["ST", "RIGHT"], VOCAB) == (False, True)
    assert ocr.caption_verdict(["OH"], VOCAB) == (False, False)
