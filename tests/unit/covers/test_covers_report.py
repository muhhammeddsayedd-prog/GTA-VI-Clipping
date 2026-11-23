"""Cover engine C8: the weekly report numbers and the console grid's preview choice. No database."""

from sixstars.covers import report


def row(status: str, error: str | None = None) -> dict[str, object]:
    return {"status": status, "error": error}


def test_summarise() -> None:
    rows = [
        row("applied"),
        row("passed"),
        row("failed", "ocr, exposure:clipped=0.05"),
        row("failed", "ocr"),
        row("failed"),
        row("fallback_plain"),
        row("fallback_default"),
        row("skipped_deadline"),
    ]
    s = report.summarise(rows)
    assert s["attempts"] == 6 and s["qa_pass_rate"] == 0.5
    assert s["top_fail_reasons"][0] == ("ocr", 2) and ("vision", 1) in s["top_fail_reasons"]
    assert s["covers"] == 4 and s["fallback_rate"] == 0.5 and s["deadline_misses"] == 1
    assert report.summarise([])["qa_pass_rate"] is None


def test_preview_key() -> None:
    assert report.preview_key("covers-work/c/x.jpg", "instagram") == "covers-work/c/x_3x4.jpg"
    assert report.preview_key("covers-work/c/x.jpg", "youtube") == "covers-work/c/x.jpg"
    assert report.preview_key(None, "tiktok") is None
