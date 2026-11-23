"""Reading the burned-in caption of a rendered frame (RapidOCR) and judging its words against the clip's
transcript: a whole word is fine, a piece of one is a cut word (docs/29a: TikTok's `cover_timestamp` and
YouTube's auto-pick should land on a frame with a whole caption phrase)."""

import difflib
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from sixstars.factory import tools
from sixstars.factory.layout import REF_HEIGHT, REF_WIDTH

# Text band on the reference canvas: everything below the top safe zone (captions sit on the seam, in the
# gameplay or, for some styles, over the facecam), and the smallest text height that
# counts as a caption (the credit line and the watermark are smaller).
CAPTION_TOP, CAPTION_BOTTOM = 200, 1760
MIN_CAPTION_TEXT_H = 45
FUZZY_MATCH = 0.8

TokenStatus = Literal["ok", "cut", "unknown", "masked"]

_WORD = re.compile(r"[^a-z0-9*]+")
# Digits OCR reads for letters in heavy caption fonts.
_OCR_SLIPS = str.maketrans({"0": "o", "1": "l", "5": "s"})


def normalise(text: str) -> str:
    """Lower case, letters, digits and the mask star only. Pure."""
    return _WORD.sub("", text.lower())


def token_status(token: str, vocabulary: Sequence[str]) -> TokenStatus:
    """What an OCR'd caption word is: a word of the transcript (`ok`, fuzzy for OCR slips), a piece of one
    (`cut`), a masked profanity (`masked`) or something else (`unknown`: noise, `X2`, an emoji). Pure."""
    word = normalise(token)
    if not word:
        return "unknown"
    if "*" in word:
        return "masked"
    if word in vocabulary or word.translate(_OCR_SLIPS) in vocabulary:
        return "ok"
    if any(difflib.SequenceMatcher(None, word, known).ratio() >= FUZZY_MATCH for known in vocabulary):
        return "ok"
    if len(word) >= 2 and any(
        len(known) >= len(word) + 2 and (known.startswith(word) or known.endswith(word))
        for known in vocabulary
    ):
        return "cut"
    return "unknown"


def caption_verdict(tokens: Sequence[str], vocabulary: Sequence[str]) -> tuple[bool, bool]:
    """(whole phrase: two or more transcript words, cut: a piece of a word) for one frame. Pure."""
    statuses = [token_status(token, vocabulary) for token in tokens]
    return sum(1 for s in statuses if s in ("ok", "masked")) >= 2, "cut" in statuses


def frame_rgb(path: Path, t_s: float, ffmpeg: str = "ffmpeg") -> tuple[int, int, bytes]:
    """One frame of a rendered clip at the 1080x1920 reference size, as raw RGB24."""
    command = [ffmpeg, "-v", "error", "-ss", f"{t_s:.3f}", "-i", str(path), "-frames:v", "1"]
    command += ["-vf", f"scale={REF_WIDTH}:{REF_HEIGHT}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    result = tools.run(command, timeout_s=30, what="cover rendered frame")
    return REF_WIDTH, REF_HEIGHT, result.stdout


def caption_tokens(engine: Any, frame: tuple[int, int, bytes]) -> list[str]:
    """The words of caption-sized text in the caption band of a reference-size RGB frame."""
    import numpy as np

    width, _height, data = frame
    band = data[CAPTION_TOP * width * 3 : CAPTION_BOTTOM * width * 3]
    image = np.frombuffer(band, dtype=np.uint8).reshape(CAPTION_BOTTOM - CAPTION_TOP, width, 3)[:, :, ::-1]
    result = engine(np.ascontiguousarray(image))
    texts = getattr(result, "txts", None) or ()
    boxes = getattr(result, "boxes", None)
    words: list[str] = []
    for n, text in enumerate(texts):
        if boxes is not None and n < len(boxes):
            ys = [float(point[1]) for point in boxes[n]]
            if max(ys) - min(ys) < MIN_CAPTION_TEXT_H:
                continue
        words += str(text).split()
    return words
