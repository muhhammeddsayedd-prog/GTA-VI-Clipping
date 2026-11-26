"""M20: the long-form fetch goes through the proxy like the VOD lane (sticky port per attempt, retry on a
refusal, give up at once when the proxy account refused, meter the traffic)."""

from pathlib import Path
from typing import Any

import pytest

from sixstars.longform import build
from sixstars.vod import proxy, sections, ytdlp

SRC = build.Source(url="https://www.youtube.com/watch?v=x", platform="youtube", start_s=100.0, end_s=130.0)
SECTION = sections.section_for(90.0, 140.0)


def _setup(monkeypatch: pytest.MonkeyPatch, outcomes: list[Exception | None]) -> dict[str, Any]:
    seen: dict[str, Any] = {"ports": [], "usage": 0}
    accesses = [
        ytdlp.Access(proxy=f"http://u:p@gw:{port}", sticky_port=port) for port in (10001, 10002, 10003)
    ]
    monkeypatch.setattr(proxy.ProxyConfig, "load", classmethod(lambda cls: object()))
    monkeypatch.setattr(proxy, "job_accesses", lambda *a, **k: accesses)
    monkeypatch.setattr(proxy, "record_usage", lambda *a, **k: seen.__setitem__("usage", seen["usage"] + 1))

    def fake_download(*args: Any, access: ytdlp.Access, **kwargs: Any) -> sections.Downloaded:
        seen["ports"].append(access.sticky_port)
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome
        return sections.Downloaded(Path("x.mp4"), 90.0, 50.0, 1920, 1080)

    monkeypatch.setattr(sections, "download_section", fake_download)
    return seen


def _blocked(detail: str) -> ytdlp.DownloadBlockedError:
    return ytdlp.DownloadBlockedError(ytdlp.Failure("blocked", detail))


class _Vod:
    max_height = 1080
    download_timeout_s = 60.0


def test_refusal_moves_to_the_next_sticky_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen = _setup(monkeypatch, [_blocked("Sign in to confirm you're not a bot"), None])
    got = build.download("item", SRC, SECTION, tmp_path, _Vod())  # type: ignore[arg-type]
    assert got.duration_s == 50.0
    assert seen["ports"] == [10001, 10002]
    assert seen["usage"] == 1


def test_last_refusal_is_raised(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen = _setup(monkeypatch, [_blocked("bot"), _blocked("bot"), _blocked("bot")])
    with pytest.raises(ytdlp.DownloadBlockedError):
        build.download("item", SRC, SECTION, tmp_path, _Vod())  # type: ignore[arg-type]
    assert seen["ports"] == [10001, 10002, 10003]
    assert seen["usage"] == 1


def test_proxy_account_refusal_stops_at_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen = _setup(monkeypatch, [_blocked("Tunnel connection failed: 407 Proxy Authentication Required")])
    with pytest.raises(ytdlp.DownloadBlockedError):
        build.download("item", SRC, SECTION, tmp_path, _Vod())  # type: ignore[arg-type]
    assert seen["ports"] == [10001]
