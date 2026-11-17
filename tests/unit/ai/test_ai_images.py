"""Frame preparation for the vision prompts."""

import io

import pytest
from PIL import Image

from sixstars.ai.images import prepare_jpeg


def picture(width: int, height: int, fmt: str = "JPEG") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (40, 90, 160)).save(out, format=fmt)
    return out.getvalue()


def test_large_frame_is_shrunk_to_1280_on_the_long_side() -> None:
    prepared = prepare_jpeg(picture(1920, 1080))
    assert (prepared.width, prepared.height) == (1280, 720)
    with Image.open(io.BytesIO(prepared.data)) as image:
        assert image.format == "JPEG" and image.size == (1280, 720)


def test_portrait_frame_uses_its_height_as_the_long_side() -> None:
    prepared = prepare_jpeg(picture(1080, 1920))
    assert (prepared.width, prepared.height) == (720, 1280)


def test_small_frame_keeps_its_size_and_png_becomes_jpeg() -> None:
    prepared = prepare_jpeg(picture(640, 360, "PNG"))
    assert (prepared.width, prepared.height) == (640, 360)
    with Image.open(io.BytesIO(prepared.data)) as image:
        assert image.format == "JPEG"


def test_bad_bytes_raise_value_error() -> None:
    with pytest.raises(ValueError, match="not a readable image"):
        prepare_jpeg(b"not an image")
