"""Pure rules of the residential proxy (vod/proxy.py) and how yt-dlp is called through it. No network."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sixstars import config
from sixstars.vod import proxy, sections, ytdlp
from sixstars.vod.ytdlp import Access, TrafficMeter

BASE = "http://user:s3cret@gw.dataimpulse.com:10000"


def cfg(**changes: Any) -> proxy.ProxyConfig:
    values: dict[str, Any] = {
        "max_attempts": 3,
        "cookies_mode": "fallback",
        "daily_budget_usd": 3.0,
        "usd_per_gb": 1.0,
        "overhead_mb_per_call": 2.0,
        "cycle_gb": 5.0,
        "cycle_started_at": proxy.datetime(2026, 11, 18, tzinfo=proxy.UTC),
        "low_traffic_share": 0.8,
        "health_interval_s": 900.0,
    }
    return proxy.ProxyConfig(**{**values, **changes})


def sequence(*values: float) -> Any:
    items = list(values)

    def rand() -> float:
        return items.pop(0) if len(items) > 1 else items[0]

    return rand


# ── ports ────────────────────────────────────────────────────


def test_with_port_replaces_only_the_port() -> None:
    assert proxy.with_port(BASE, 14321) == "http://user:s3cret@gw.dataimpulse.com:14321"
    assert proxy.with_port("http://gw.example.com", 10001) == "http://gw.example.com:10001"
    assert proxy.with_port("http://u:p@[2001:db8::1]:823", 10002) == "http://u:p@[2001:db8::1]:10002"


def test_port_range_needs_both_ends_in_order() -> None:
    assert proxy.port_range(10000, 20000) == (10000, 20000)
    assert proxy.port_range(None, 20000) is None
    assert proxy.port_range(20000, 10000) is None
    assert proxy.port_range(0, 10) is None


def test_pick_port_stays_in_range_and_avoids_used_ports() -> None:
    assert proxy.pick_port(10000, 20000, lambda: 0.0) == 10000
    assert proxy.pick_port(10000, 20000, lambda: 0.999999) == 20000
    assert proxy.pick_port(10000, 20000, lambda: 1.0) == 20000  # never past the end
    assert proxy.pick_port(10000, 20000, sequence(0.0, 0.0, 0.5), used={10000}) == 15000
    # A range of one port that is used: it is still the answer (better than none).
    assert proxy.pick_port(10000, 10000, lambda: 0.3, used={10000}) == 10000
    # Every draw hits a used port: the first free one is taken.
    assert proxy.pick_port(10000, 10002, lambda: 0.0, used={10000}) == 10001


# ── cookies order ────────────────────────────────────────────


def test_cookies_come_after_the_proxy_alone_by_default() -> None:
    assert proxy.cookies_plan(3, "fallback", True) == [False, False, False, True]
    assert proxy.cookies_plan(3, "fallback", False) == [False, False, False]
    assert proxy.cookies_plan(3, "first", True) == [True, True, True]
    assert proxy.cookies_plan(3, "never", True) == [False, False, False]
    assert proxy.cookies_plan(0, "fallback", False) == [False]


# ── traffic, cost, budget ────────────────────────────────────


def test_traffic_adds_the_overhead_of_each_call() -> None:
    assert proxy.traffic_bytes(40_000_000, 3, 2.0) == 46_000_000
    assert proxy.traffic_bytes(-5, 0, 2.0) == 0


def test_cost_is_gigabytes_times_the_price() -> None:
    assert proxy.cost_usd(1_500_000_000, 1.0) == 1.5
    assert proxy.cost_usd(250_000_000, 1.0) == 0.25
    assert proxy.gigabytes(500_000_000) == 0.5


def test_budget_and_low_traffic_rules() -> None:
    assert proxy.budget_reached(3.0, 3.0) and not proxy.budget_reached(2.99, 3.0)
    assert not proxy.budget_reached(100.0, 0)  # 0 = no limit
    assert proxy.low_traffic(4.0, 5.0, 0.8) and not proxy.low_traffic(3.99, 5.0, 0.8)
    assert not proxy.low_traffic(9.0, 0, 0.8)


def test_only_account_refusals_stop_the_retries() -> None:
    assert proxy.is_account_failure("proxy refused: 407 Proxy Authentication Required")
    assert proxy.is_account_failure("Tunnel connection failed: 402 Payment Required")
    assert not proxy.is_account_failure("Unable to connect to proxy: timed out")
    assert not proxy.is_account_failure("ERROR: Sign in to confirm you're not a bot")


def test_provider_name_for_messages() -> None:
    assert proxy.provider_name(BASE) == "DataImpulse"
    assert proxy.provider_name("http://u:p@brd.superproxy.io:33335") == "The proxy"
    assert proxy.provider_name(None) == "The proxy"


# ── the accesses of one job ──────────────────────────────────


@pytest.fixture
def sticky_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setenv("PROXY_STICKY_PORT_MIN", "10000")
    monkeypatch.setenv("PROXY_STICKY_PORT_MAX", "20000")
    monkeypatch.setenv("POT_PROVIDER_URL", "http://bgutil:4416")
    config.env.cache_clear()
    monkeypatch.setattr(proxy, "access_for", lambda platform: Access(proxy=BASE, cookies_file=cookies))
    yield cookies
    config.env.cache_clear()


def test_each_attempt_has_its_own_port_and_shares_the_meter(sticky_env: Path) -> None:
    meter = TrafficMeter()
    accesses = proxy.job_accesses("youtube", sequence(0.1, 0.2, 0.3, 0.4), cfg(), meter)
    ports = [access.sticky_port for access in accesses]
    assert ports == [11000, 12000, 13000, 14000]
    assert [access.proxy for access in accesses] == [
        f"http://user:s3cret@gw.dataimpulse.com:{p}" for p in ports
    ]
    assert [access.cookies_file is not None for access in accesses] == [False, False, False, True]
    assert all(access.meter is meter and access.pot_url == "http://bgutil:4416" for access in accesses)
    # The configuration text the blocked back-off compares does not change between attempts.
    assert {access.described for access in accesses} == {"PROXY_URL set, YOUTUBE_COOKIES_FILE set"}


def test_twitch_and_a_direct_connection_get_one_plain_attempt(
    sticky_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert proxy.job_accesses("twitch", lambda: 0.5, cfg(), TrafficMeter()) == [
        Access(proxy=BASE, cookies_file=sticky_env)
    ]
    monkeypatch.setattr(proxy, "access_for", lambda platform: Access())
    assert proxy.job_accesses("youtube", lambda: 0.5, cfg(), TrafficMeter()) == [Access()]


def test_without_a_port_range_the_url_is_used_as_it_is(
    sticky_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PROXY_STICKY_PORT_MIN")
    config.env.cache_clear()
    accesses = proxy.job_accesses("youtube", lambda: 0.5, cfg(cookies_mode="never"), TrafficMeter())
    assert [access.proxy for access in accesses] == [BASE] * 3
    assert all(access.sticky_port is None for access in accesses)


# ── yt-dlp through the proxy ─────────────────────────────────


def test_the_po_token_provider_is_used_only_with_the_proxy() -> None:
    with_proxy = ytdlp.base_command(Access(proxy=BASE, pot_url="http://bgutil:4416"))
    assert (
        with_proxy[with_proxy.index("--extractor-args") + 1]
        == "youtubepot-bgutilhttp:base_url=http://bgutil:4416"
    )
    assert "--extractor-args" not in ytdlp.base_command(Access(pot_url="http://bgutil:4416"))


def test_calls_are_counted_only_through_the_proxy() -> None:
    meter = TrafficMeter()
    ytdlp.run(
        ["x"], access=Access(proxy=BASE, meter=meter), timeout_s=1, what="t", runner=lambda c, t: (0, "", "")
    )
    ytdlp.run(["x"], access=Access(meter=meter), timeout_s=1, what="t", runner=lambda c, t: (0, "", ""))
    assert meter.calls == 1


def test_downloads_through_the_proxy_never_go_above_the_cap() -> None:
    strict = sections.format_selector(1080, strict_cap=True)
    assert all("[height<=1080]" in option for option in strict.split("/"))
    assert sections.format_selector(1080).endswith("/bv*+ba/b")  # direct: a taller source beats no clip
