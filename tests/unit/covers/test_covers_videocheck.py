"""C1 acceptance check: the pure judging of OCR'd caption words and frames (docs/29a section 9)."""

from sixstars.covers.videocheck import ClipCheck, FrameCheck, check_times, normalise, token_status

VOCAB = sorted(
    {normalise(w) for w in ["the", "suspect's", "car", "is", "gone", "right", "just", "oh", "god"]}
)


def test_token_status() -> None:
    assert token_status("SUSPECT'S", VOCAB) == "ok"
    assert token_status("GONE!", VOCAB) == "ok"
    assert token_status("G0NE", VOCAB) == "ok"  # OCR slip, fuzzy
    assert token_status("SUSPE", VOCAB) == "cut"
    assert token_status("ST", VOCAB) == "cut"  # the tail of "just"
    assert token_status("F*CK", VOCAB) == "masked"
    assert token_status("X2", VOCAB) == "unknown"
    assert token_status("!!", VOCAB) == "unknown"


def _frame(window: str, share: float | None, *tokens: str) -> FrameCheck:
    return FrameCheck(
        window=window,  # type: ignore[arg-type]
        t_s=1.0,
        face_share=share,
        tokens=tokens,
        statuses=tuple(token_status(t, VOCAB) for t in tokens),
    )


def test_clip_verdicts() -> None:
    good = ClipCheck(
        "c1",
        (
            _frame("open", 0.40, "OH", "GOD"),
            _frame("open", 0.38),
            _frame("open", 0.30, "THE"),
            _frame("peak", 0.41, "THE", "CAR", "IS", "GONE"),
            _frame("peak", 0.36, "GONE"),
        ),
        True,
        12.0,
        12.0,
    )
    assert good.face_ok() and good.caption_ok() and good.passed()
    cut = ClipCheck("c2", (*good.frames, _frame("peak", 0.4, "ST", "RIGHT")), True, 12.0, 12.0)
    assert not cut.caption_ok() and not cut.passed()
    small = ClipCheck("c3", (_frame("open", 0.2, "OH", "GOD"), _frame("open", None, "OH", "GOD")), True, 9, 9)
    assert not small.face_ok()
    no_phrase = ClipCheck("c4", (_frame("open", 0.4, "OH"), _frame("open", 0.4)), True, 9.0, 9.0)
    assert not no_phrase.caption_ok()
    assert not ClipCheck("c5", good.frames, False, 15.0, 12.0).passed()


def test_check_times() -> None:
    assert check_times(10.0, 4.0) == [
        ("open", 0.5),
        ("open", 1.0),
        ("open", 1.5),
        ("peak", 3.5),
        ("peak", 4.0),
        ("peak", 4.5),
    ]
    assert check_times(10.0, None) == [("open", 0.5), ("open", 1.0), ("open", 1.5)]
    assert [t for _w, t in check_times(10.0, 9.8)] == [0.5, 1.0, 1.5, 9.3, 9.8]
