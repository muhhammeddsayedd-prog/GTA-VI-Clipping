"""The VOD lane through the residential proxy (vod/proxy.py), against Postgres with the fakes of
test_vod_process: one sticky port per job, a new port per retry, cookies last, traffic and cost in
`api_usage`, the daily proxy budget, an account refusal, and the guardian's proxy checks."""

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
import test_vod_process
from test_vod_process import BOT_CHECK, NOW, Lane, queued, video_row

from sixstars import config, db, settings, usage
from sixstars.clients.fakes import FakeProxyProbe
from sixstars.clients.proxy_probe import ProbeResult
from sixstars.factory.pipeline import next_budget_reset
from sixstars.guardian import health as guardian
from sixstars.guardian import incidents
from sixstars.vod import proxy

lane = test_vod_process.lane  # the fixture, shared with test_vod_process

DATAIMPULSE = "http://user:s3cret@gw.dataimpulse.com:10000"
PROXY_407 = "ERROR: Unable to download webpage: Tunnel connection failed: 407 Proxy Authentication Required"


@pytest.fixture
def sticky(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("PROXY_URL", DATAIMPULSE)
    monkeypatch.setenv("PROXY_STICKY_PORT_MIN", "10000")
    monkeypatch.setenv("PROXY_STICKY_PORT_MAX", "20000")
    monkeypatch.setenv("POT_PROVIDER_URL", "http://bgutil:4416")
    monkeypatch.delenv("YOUTUBE_COOKIES_FILE", raising=False)
    config.env.cache_clear()
    yield
    config.env.cache_clear()


def with_cookies(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setenv("YOUTUBE_COOKIES_FILE", str(cookies))
    config.env.cache_clear()


def option(command: list[str], flag: str) -> str | None:
    return command[command.index(flag) + 1] if flag in command else None


def proxied(lane: Lane) -> list[list[str]]:
    return [command for command in lane.ytdlp.commands if "--proxy" in command]


def port_of(command: list[str]) -> int:
    url = option(command, "--proxy")
    assert url is not None and url.startswith("http://user:s3cret@gw.dataimpulse.com:")
    return int(url.rsplit(":", 1)[1])


def attempts() -> list[dict[str, object]]:
    rows = db.fetch_all("select data from events where type = 'vod.proxy_attempt' order by id")
    return [dict(row["data"]) for row in rows]


def usage_rows() -> list[dict[str, object]]:
    return db.fetch_all("select * from api_usage where provider = 'DataImpulse' order by id")


def test_one_job_uses_one_sticky_port_for_every_request(lane: Lane, sticky: None) -> None:
    video_id = lane.video()
    lane.enqueue(video_id)

    outcome = lane.run_next()

    assert outcome.status == "done" and outcome.downloaded == 2
    commands = proxied(lane)
    assert len(commands) == len(lane.ytdlp.commands) >= 2  # captions and windows, all through the proxy
    assert len({port_of(command) for command in commands}) == 1
    assert 10000 <= port_of(commands[0]) <= 20000
    assert all(
        option(c, "--extractor-args") == "youtubepot-bgutilhttp:base_url=http://bgutil:4416" for c in commands
    )
    assert all("[height<=1080]" in str(option(c, "-f")) for c in commands if "-f" in c)
    assert not any("/bv*+ba/b" in str(option(c, "-f")) for c in commands)  # no taller fallback
    assert [(a["attempt"], a["ok"], a["port"]) for a in attempts()] == [(1, True, port_of(commands[0]))]
    # Traffic: the window files, the chat replay (downloaded through the job's session) and any caption
    # file, plus 2 MB of overhead per call.
    [row] = usage_rows()
    assert row["operation"] == "youtube_download" and row["ref_id"] == video_id
    overhead = len(commands) * 2_000_000
    assert overhead / 1e9 < float(row["units"]) < (overhead + 1_000_000) / 1e9
    assert float(row["cost_usd"]) == pytest.approx(float(row["units"]) * 1.0, abs=1e-6)


def test_a_bot_check_is_tried_again_on_a_new_port(lane: Lane, sticky: None) -> None:
    settings.set("vod.confirm_with_captions", False)
    settings.set("vod.download_workers", 1)  # one window at a time: the first one meets the bot check
    video_id = lane.video()
    lane.ytdlp.section_results = [(1, "", BOT_CHECK)]
    lane.enqueue(video_id)

    outcome = lane.run_next()

    assert outcome.status == "done" and outcome.downloaded == 2
    tries = attempts()
    assert [(a["attempt"], a["ok"]) for a in tries] == [(1, False), (2, True)]
    assert tries[0]["port"] != tries[1]["port"]
    assert video_row(video_id)["status"] == "done"
    assert len(usage_rows()) == 1  # one row per job, whatever the attempts


def test_after_the_proxy_alone_cookies_are_tried_once_then_the_video_waits(
    lane: Lane, sticky: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with_cookies(monkeypatch, tmp_path)
    settings.set("vod.confirm_with_captions", False)
    settings.set("vod.download_workers", 1)
    video_id = lane.video()
    lane.ytdlp.section_results = [(1, "", BOT_CHECK)] * 20
    lane.enqueue(video_id)

    outcome = lane.run_next()

    assert outcome.status == "deferred" and outcome.kind == "download_blocked"
    assert outcome.run_after == NOW + timedelta(seconds=1800)  # then the 30-min retry, as before
    tries = attempts()
    assert [a["cookies"] for a in tries] == [False, False, False, True]
    assert len({a["port"] for a in tries}) == 4
    commands = proxied(lane)
    with_cookie = {port_of(c) for c in commands if "--cookies" in c}
    assert with_cookie == {tries[3]["port"]}  # only the last attempt sends cookies
    video = video_row(video_id)
    assert video["status"] == "new" and str(video["skip_reason"]).startswith("download_blocked")
    assert len(queued("vod.process")) == 1  # waiting, not failed


def test_one_video_refused_on_every_port_does_not_hold_the_others(lane: Lane, sticky: None) -> None:
    """Each job has its own exit IPs: the platform is not blocked because one video was refused."""
    settings.set("vod.confirm_with_captions", False)
    settings.set("vod.download_workers", 1)
    stubborn = lane.video("vodtest0001")
    lane.ytdlp.section_results = [(1, "", BOT_CHECK)] * 20
    lane.enqueue(stubborn)
    assert lane.run_next().kind == "download_blocked"
    no_platform_block = db.fetch_all("select 1 from events where type = 'vod.download_blocked'")
    assert no_platform_block == []

    lane.ytdlp.section_results = []  # the next video is served
    other = lane.video("vodtest0002")
    lane.enqueue(other)
    assert lane.run_next().status == "done"  # not deferred by a platform-wide back-off
    assert video_row(stubborn)["status"] == "new"  # the refused one still waits its 30 min


def test_cookies_order_is_a_setting(
    lane: Lane, sticky: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with_cookies(monkeypatch, tmp_path)
    settings.set("proxy.cookies_mode", "never")
    settings.set("vod.confirm_with_captions", False)
    settings.set("vod.download_workers", 1)
    lane.ytdlp.section_results = [(1, "", BOT_CHECK)] * 20
    lane.enqueue(lane.video())

    assert lane.run_next().kind == "download_blocked"
    assert [a["cookies"] for a in attempts()] == [False, False, False]


def test_a_refusal_by_the_proxy_account_is_not_retried_and_names_the_provider(
    lane: Lane, sticky: None
) -> None:
    settings.set("vod.confirm_with_captions", False)
    lane.ytdlp.section_results = [(1, "", PROXY_407)] * 20
    lane.enqueue(lane.video())

    outcome = lane.run_next()

    assert outcome.kind == "download_blocked"
    assert len(attempts()) == 1
    [alert] = db.fetch_all("select title from notify_outbox")
    assert alert["title"] == (
        "DataImpulse proxy failing: 407 Proxy Authentication Required — top up or check credentials"
    )


def test_the_daily_proxy_budget_pauses_youtube_downloads_until_tomorrow(lane: Lane, sticky: None) -> None:
    usage.record("DataImpulse", "youtube_download", units=3.0, cost_usd=3.0)
    video_id = lane.video()
    lane.enqueue(video_id)

    outcome = lane.run_next()

    assert outcome.status == "deferred" and outcome.kind == "proxy_budget"
    assert outcome.run_after == next_budget_reset(NOW)
    assert lane.ytdlp.commands == []  # nothing downloaded
    assert video_row(video_id)["status"] == "new"
    settings.set("proxy.daily_budget_usd", 10)
    db.execute("update jobs set run_after = now() where status = 'queued'")
    assert lane.run_next().status == "done"


def test_without_the_proxy_the_lane_is_unchanged(lane: Lane) -> None:
    lane.enqueue(lane.video())
    assert lane.run_next().status == "done"
    assert proxied(lane) == [] and attempts() == [] and usage_rows() == []


# ── the guardian ─────────────────────────────────────────────


def test_guardian_reports_a_failing_proxy_and_low_traffic(sticky: None) -> None:
    open_now = incidents.open_keys()
    failing = FakeProxyProbe(ProbeResult(False, "402 Payment Required", account_failure=True))
    usage.record("DataImpulse", "youtube_download", units=4.2, cost_usd=4.2)

    result = guardian.check_proxy(NOW, open_now, probe=failing, rand=lambda: 0.5)

    assert result == {"ok": False, "reason": "402 Payment Required", "cycle_used_gb": 4.2}
    assert 10000 <= int(failing.calls_to("probe")[0]["proxy_url"].rsplit(":", 1)[1]) <= 20000
    titles = {
        (row["severity"], row["title"]) for row in db.fetch_all("select severity, title from notify_outbox")
    }
    assert (
        "critical",
        "DataImpulse proxy failing: 402 Payment Required — top up or check credentials",
    ) in titles
    assert ("warn", "DataImpulse traffic running low: top up") in titles
    assert proxy.health_state() == {
        "ok": False,
        "reason": "402 Payment Required",
        "checked_at": NOW.isoformat(),
    }

    # Within proxy.health_interval_s the stored answer is reused: no second probe.
    working = FakeProxyProbe()
    guardian.check_proxy(NOW + timedelta(seconds=60), open_now, probe=working)
    assert working.calls_to("probe") == []
    guardian.check_proxy(NOW + timedelta(seconds=901), open_now, probe=working)
    assert len(working.calls_to("probe")) == 1 and proxy.health_state()["ok"] is True  # type: ignore[index]


def test_guardian_skips_the_proxy_check_without_a_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROXY_URL", raising=False)
    config.env.cache_clear()
    probe = FakeProxyProbe()
    assert guardian.check_proxy(NOW, set(), probe=probe) is None and probe.calls == []
