"""Unit tests for JSON structured logging."""

import json
from datetime import UTC, datetime

import pytest

from sixstars.logging import bind_context, clear_context, configure_logging, get_logger


def _lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]


def test_log_line_is_json_with_required_keys(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    get_logger("worker-express").info("job_claimed", attempt=1)
    (line,) = _lines(capsys)
    assert line["event"] == "job_claimed"
    assert line["level"] == "info"
    assert line["env"] == "dev"
    assert line["service"] == "worker-express"
    assert line["attempt"] == 1
    ts = datetime.fromisoformat(str(line["ts"]))
    assert ts.utcoffset() == UTC.utcoffset(None)


def test_service_defaults_to_service_name(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SERVICE_NAME", "scheduler")
    configure_logging("INFO")
    get_logger().info("tick")
    (line,) = _lines(capsys)
    assert line["service"] == "scheduler"


def test_bind_context_adds_ids_and_clear_removes_them(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    log = get_logger("api")
    bind_context(clip_id="c1", spike_id="s1", job_id=7)
    try:
        log.info("with_ids")
    finally:
        clear_context()
    log.info("without_ids")
    first, second = _lines(capsys)
    assert (first["clip_id"], first["spike_id"], first["job_id"]) == ("c1", "s1", 7)
    assert "clip_id" not in second


def test_level_filtering(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("WARNING")
    log = get_logger("api")
    log.info("hidden")
    log.warning("shown")
    (line,) = _lines(capsys)
    assert line["event"] == "shown"
