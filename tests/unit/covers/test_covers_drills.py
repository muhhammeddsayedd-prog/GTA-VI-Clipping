"""Cover engine fault drills (docs/29a T): whatever breaks in the cover engine, the post goes out without a
custom cover and one alert is raised. The staging drills (a real Playwright crash, a missing OCR model,
a vision timeout, Storage down, Upload-Post 5xx, YouTube 403) run after the deploy; these prove the code
paths."""

from types import SimpleNamespace
from typing import Any

import pytest

from sixstars.covers import deliver, engine, miner


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(deliver, "alert_once", lambda kind, title, body: sent.append((kind, title)))
    return sent


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("database down"),
        OSError("storage unreachable"),
        TimeoutError("vision model timed out"),
    ],
)
def test_cover_fields_never_stop_a_post(
    monkeypatch: pytest.MonkeyPatch, alerts: list[tuple[str, str]], failure: Exception
) -> None:
    def boom(cls: Any) -> Any:
        raise failure

    monkeypatch.setattr(deliver.CoverConfig, "load", classmethod(boom))
    assert deliver.cover_fields("clip", "instagram_main", "instagram", "hourly", "post") == {}
    assert alerts == [("fields", "Cover fields failed")]


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("Playwright crashed"),
        FileNotFoundError("OCR model missing"),
        TimeoutError("vision model timed out"),
    ],
)
def test_a_failed_build_alerts_and_does_not_fail_the_job(
    monkeypatch: pytest.MonkeyPatch, alerts: list[tuple[str, str]], failure: Exception
) -> None:
    monkeypatch.setattr(
        miner.CoverConfig,
        "load",
        classmethod(lambda cls: miner.CoverConfig.from_values({"cover.enabled": True})),
    )
    monkeypatch.setattr(miner, "mine", lambda clip_id: None)

    def build(clip_id: str, **_: Any) -> dict[str, str]:
        raise failure

    monkeypatch.setattr(engine, "build", build)
    job: Any = SimpleNamespace(payload={"clip_id": "c1"})
    miner.handle_mine(job)
    assert alerts == [("build", "Cover engine: build failed")]


def test_capture_never_fails_a_render(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    def boom(cls: Any) -> Any:
        raise RuntimeError("scoring_config unreachable")

    monkeypatch.setattr(miner.CoverConfig, "load", classmethod(boom))
    queued = miner.capture(
        "c1",
        source=tmp_path / "x.mp4",
        window=miner.ClipWindow(0, 10),
        duration_s=10.0,
        has_audio=True,
        source_w=1920,
        source_h=1080,
        facecam=None,
        gameplay=None,
        peak_s=None,
        moment_type=None,
        lane="spike",
        rendered_path=None,
    )
    assert queued is False
