"""Prepares video frames for the vision prompts: at most 1280 px on the long side, JPEG quality 85
(M05 build step 3). Smaller images cost fewer tokens and are still large enough to see a facecam edge or
a watermark. Implemented in M05.
"""

import io
from dataclasses import dataclass

from PIL import Image

MAX_SIDE_PX = 1280
JPEG_QUALITY = 85


@dataclass(frozen=True, slots=True)
class PreparedImage:
    data: bytes
    width: int
    height: int


def prepare_jpeg(data: bytes, max_side: int = MAX_SIDE_PX, quality: int = JPEG_QUALITY) -> PreparedImage:
    """Decode an image, shrink it so its long side is at most `max_side` and re-encode it as JPEG.

    Raises `ValueError` when the bytes are not an image.
    """
    try:
        with Image.open(io.BytesIO(data)) as opened:
            image = opened.convert("RGB")
    except OSError as exc:
        raise ValueError("frame is not a readable image") from exc
    width, height = image.size
    long_side = max(width, height)
    if long_side > max_side:
        factor = max_side / long_side
        width, height = max(1, round(width * factor)), max(1, round(height * factor))
        image = image.resize((width, height), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=quality)
    return PreparedImage(out.getvalue(), width, height)
