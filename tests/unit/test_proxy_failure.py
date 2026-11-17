"""A proxy that itself refuses (no balance, suspended, wrong credentials, provider policy, unreachable) is
one failure class shared by every lane: recognised in one place, handled as "blocked for now", reported
with what the proxy answered, and never with the credentials of the proxy URL."""

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from sixstars.chat import net, youtube_replay
from sixstars.chat import youtube_innertube as innertube
from sixstars.chat.models import ChatSourceError
from sixstars.discovery import doctor
from sixstars.live import health as live_health
from sixstars.live import recorder
from sixstars.live.session import after_failure, looks_blocked
from sixstars.vod import ytdlp
from sixstars.vod.ytdlp import Access, DownloadBlockedError, ProxyRefusedError

PROXY = "http://brd-customer-hl_1-zone-res:s3cr3tpass@brd.superproxy.io:33335"
PREFIX = "ERROR: [youtube] dQw4w9WgXcQ: Unable to download API page: ('Unable to connect to proxy', "
E407 = PREFIX + "OSError('Tunnel connection failed: 407 Proxy Authentication Required'))"
E407_SUSPENDED = PREFIX + "OSError('Tunnel connection failed: 407 Account is suspended'))"
E402 = PREFIX + "OSError('Tunnel connection failed: 402 Payment Required'))"
E403 = PREFIX + "OSError('Tunnel connection failed: 403 Forbidden'))"  # Bright Data, policy_20050
E_REFUSED = PREFIX + "NewConnectionError('<HTTPSConnection object>: [Errno 111] Connection refused'))"
E_TIMEOUT = PREFIX + "ConnectTimeoutError('Connection to brd.superproxy.io timed out. (connect timeout=30)'))"
E_DNS = (
    PREFIX
    + "NameResolutionError('Failed to resolve brd.superproxy.io ([Errno -2] Name or service not known)'))"
)
BOT_CHECK = (
    "ERROR: [youtube] dQw4w9WgXcQ: Sign in to confirm you're not a bot. Use --cookies-from-browser or "
    "--cookies for the authentication."
)

# (error text, status the proxy answered with, summary) for everything that is a proxy failure.
PROXY_CASES: list[tuple[str, int | None, str]] = [
    (E407, 407, "407 Proxy Authentication Required"),
    (E407_SUSPENDED, 407, "407 Account is suspended"),
    (E402, 402, "402 Payment Required"),
    (E403, 403, "403 Forbidden"),
    (E407.upper(), 407, "407 PROXY AUTHENTICATION REQUIRED"),
    (E402.lower(), 402, "402 payment required"),
    (
        "ERROR: Unable to download webpage: Tunnel connection failed: 429 Too Many Requests",
        429,
        "429 Too Many Requests",
    ),
    ("ERROR: unable to connect to proxy: Tunnel connection failed: 502", 502, "502 Bad Gateway"),
    (E_REFUSED, None, "no answer (connection refused)"),
    (E_TIMEOUT, None, "no answer (timed out)"),
    (E_DNS, None, "no answer (name resolution failed)"),
    ("ERROR: Unable to connect to proxy", None, "no answer (no status code)"),
    ("curl: (56) CONNECT tunnel failed, response 407", 407, "407 Proxy Authentication Required"),
    ("curl: (56) Received HTTP code 402 from proxy after CONNECT", 402, "402 Payment Required"),
    ("[https @ 0x1] HTTP error 407 Proxy Authentication Required", 407, "407 Proxy Authentication Required"),
    ("YouTube watch page request failed: ProxyError: 403 Forbidden", 403, "403 Forbidden"),
    ("proxy refused: 402 Payment Required", 402, "402 Payment Required"),  # our own stored reason
]
# (error text, kind of the VOD classifier) for everything that is not: nothing here may change.
OTHER_CASES: list[tuple[str, str]] = [
    (BOT_CHECK, "blocked"),
    ("ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies", "blocked"),
    ("ERROR: [youtube] abc: Unable to download webpage: HTTP Error 429: Too Many Requests", "blocked"),
    ("ERROR: [youtube] abc: This content isn't available, try again later.", "blocked"),
    ("ERROR: [youtube] abc: The page needs to be reloaded.", "blocked"),
    (
        "ERROR: [youtube] abc: Private video. Sign in if you've been granted access to this video",
        "unavailable",
    ),
    ("ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader", "unavailable"),
    ("ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate", "unavailable"),
    ("ERROR: [twitch:vod] 123: Video 123 does not exist", "unavailable"),
    ("[https @ 000001] HTTP error 403 Forbidden\nERROR: ffmpeg exited with code 1", "forbidden"),
    ("ERROR: unable to download video data: HTTP Error 403: Forbidden", "forbidden"),
    ("ERROR: Unable to download webpage: Connection reset", "error"),
    ("ERROR: [youtube] abc: Unable to download webpage: The read operation timed out", "error"),
    ("ERROR: unable to download video data: Connection refused", "error"),
    ("ERROR: ffmpeg exited with code 1", "error"),
    ("", "error"),
]


# ── the shared classifier ────────────────────────────────────


@pytest.mark.parametrize(("text", "status", "summary"), PROXY_CASES)
def test_a_proxy_failure_is_recognised_with_what_the_proxy_answered(
    text: str, status: int | None, summary: str
) -> None:
    refusal = net.proxy_refusal(text)
    assert refusal is not None
    assert (refusal.status, refusal.summary) == (status, summary)
    assert refusal.described == f"proxy refused: {summary}"


@pytest.mark.parametrize("text", [text for text, _ in OTHER_CASES])
def test_anything_else_is_not_a_proxy_failure(text: str) -> None:
    assert net.proxy_refusal(text) is None


@pytest.mark.parametrize("text", [text for text, _, _ in PROXY_CASES])
def test_the_vod_classifier_gives_proxy_failures_their_own_kind(text: str) -> None:
    failure = ytdlp.classify_failure(1, text)
    assert failure.kind == "proxy"
    error = ytdlp.error_for(failure)
    # Its own class for the message, and a blocked download for every caller that parks the video.
    assert isinstance(error, ProxyRefusedError) and isinstance(error, DownloadBlockedError)
    assert net.proxy_refusal(error.detail) == net.proxy_refusal(text)


@pytest.mark.parametrize(("text", "kind"), OTHER_CASES)
def test_the_vod_classifier_keeps_every_other_kind(text: str, kind: str) -> None:
    assert ytdlp.classify_failure(1, text).kind == kind
    assert type(ytdlp.error_for(ytdlp.classify_failure(1, text))) is not ProxyRefusedError


def test_the_last_error_decides_between_the_proxy_and_the_platform() -> None:
    # yt-dlp retried past a proxy hiccup and then met the bot check, or a video that is gone.
    hiccup = (
        "WARNING: [youtube] Unable to connect to proxy; Tunnel connection failed: 502 Bad Gateway. Retrying"
    )
    assert ytdlp.classify_failure(1, f"{hiccup}\n{BOT_CHECK}").kind == "blocked"
    assert ytdlp.classify_failure(1, f"{hiccup}\nERROR: [youtube] abc: Private video").kind == "unavailable"
    # The proxy's answer is the last error: it is the proxy, whatever came before.
    assert ytdlp.classify_failure(1, f"{BOT_CHECK}\n{E407}").kind == "proxy"
    # The tunnel's 403 is the proxy's, not the media server's, also when ffmpeg has the last word.
    behind = ytdlp.classify_failure(1, f"{E403}\nERROR: ffmpeg exited with code 1")
    assert behind.kind == "proxy" and "403 Forbidden" in behind.detail
    assert ytdlp.classify_failure(ytdlp.EXIT_TIMEOUT, E_TIMEOUT).kind == "proxy"


# ── credentials never leave ──────────────────────────────────


def test_credentials_of_the_proxy_url_are_removed() -> None:
    text = f"ERROR: could not use {PROXY} (ProxyError); also tried {PROXY}/ and socks5://bob:pw@10.0.0.1:1080"
    cleaned = net.redact_proxy(text, PROXY)
    assert "s3cr3tpass" not in cleaned and "brd-customer" not in cleaned and "bob:pw" not in cleaned
    assert cleaned.count("<proxy>") == 2 and "socks5://<redacted>@10.0.0.1:1080" in cleaned
    # Without knowing the configured URL, the user and password of any URL still go; the host stays.
    blind = net.redact_proxy(f"Unable to connect to proxy {PROXY}")
    assert blind == "Unable to connect to proxy http://<redacted>@brd.superproxy.io:33335"
    # A user without a password, and text without a URL.
    assert (
        net.redact_proxy("via https://token@proxy.example:8000/x")
        == "via https://<redacted>@proxy.example:8000/x"
    )
    plain = "ERROR: [youtube] abc: Video unavailable; mail me@example.com, see https://example.com/a?b=c"
    assert net.redact_proxy(plain, None) == plain


def test_a_yt_dlp_run_through_a_refusing_proxy_raises_the_proxy_error_without_credentials() -> None:
    def refused(command: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
        assert command[command.index("--proxy") + 1] == PROXY
        return 1, "", f"[debug] proxy map: {{'all': '{PROXY}'}}\n{E407} via {PROXY}"

    with pytest.raises(ProxyRefusedError) as caught:
        ytdlp.run(["x"], access=Access(proxy=PROXY), timeout_s=5, what="test", runner=refused)
    assert caught.value.kind == "proxy"
    assert "407 Proxy Authentication Required" in caught.value.detail
    assert "s3cr3tpass" not in str(caught.value) and "brd-customer" not in str(caught.value)


def test_the_chat_replay_fallback_does_not_repeat_the_proxy_url(tmp_path: Path) -> None:
    def refused(command: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
        return 1, "", f"{E402} ({PROXY})"

    with pytest.raises(ChatSourceError) as caught:
        youtube_replay.download_with_ytdlp("dQw4w9WgXcQ", proxy=PROXY, runner=refused)
    assert "s3cr3tpass" not in str(caught.value)
    assert ytdlp.classify_failure(1, str(caught.value)).kind == "proxy"


# ── the chat replay client ───────────────────────────────────


async def test_a_proxy_error_of_a_chat_request_says_what_the_proxy_answered() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ProxyError(f"407 Proxy Authentication Required for {PROXY}")

    async with innertube.new_client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ChatSourceError) as caught:
            await innertube.fetch_watch_page(http, "dQw4w9WgXcQ")
    text = str(caught.value)
    assert "ProxyError: 407 Proxy Authentication Required" in text and "s3cr3tpass" not in text
    refusal = net.proxy_refusal(text)
    assert refusal is not None and refusal.summary.startswith("407 Proxy Authentication Required")
    assert looks_blocked(text)  # the live chat of a session: the platform's blocked state
    assert ytdlp.classify_failure(1, text).kind == "proxy"  # the VOD lane: the video waits


def test_other_request_errors_keep_their_short_name() -> None:
    assert net.request_error(httpx.ConnectTimeout("slow http://u:p@h")) == "ConnectTimeout"
    assert not looks_blocked("YouTube chat request failed: ConnectTimeout")
    assert looks_blocked("Sign in to confirm you’re not a bot")


# ── the live lane ────────────────────────────────────────────

STREAMLINK_407 = (
    "error: Unable to open URL: https://www.youtube.com/watch?v=x (HTTPSConnectionPool(host='www.youtube."
    "com', port=443): Max retries exceeded with url: /watch?v=x (Caused by ProxyError('Unable to connect "
    "to proxy', OSError('Tunnel connection failed: 407 Proxy Authentication Required'))))"
)
STREAMLINK_UNREACHABLE = (
    "error: Unable to open URL: https://x (HTTPSConnectionPool(host='x', port=443): Max retries exceeded "
    "(Caused by ProxyError('Unable to connect to proxy', NewConnectionError('Connection refused'))))"
)


@pytest.mark.parametrize(
    ("output", "had_data", "kind"),
    [
        (STREAMLINK_407, False, "blocked"),
        (STREAMLINK_407, True, "blocked"),  # the proxy answered with a refusal: reconnecting cannot help
        (
            STREAMLINK_407.replace("407 Proxy Authentication Required", "402 Payment Required"),
            False,
            "blocked",
        ),
        (STREAMLINK_407.replace("407 Proxy Authentication Required", "403 Forbidden"), False, "blocked"),
        (STREAMLINK_UNREACHABLE, False, "blocked"),  # no start through a proxy that cannot be reached
        (STREAMLINK_UNREACHABLE, True, "network"),  # lost in mid-recording: reconnect as before
        # nothing else moved
        (
            "[plugins.youtube][error] Could not get video info - LOGIN_REQUIRED: Sign in to confirm",
            False,
            "blocked",
        ),
        ("error: Unable to open URL: https://x (429 Client Error: Too Many Requests)", False, "blocked"),
        ("error: No playable streams found on this URL: twitch.tv/kiwo", False, "not_live"),
        ("error: Unable to open URL: https://x (HTTPSConnectionPool: Read timed out.)", False, "network"),
        ("[cli][info] Stream ended", True, "ended"),
        ("something unexpected", False, "error"),
    ],
)
def test_the_live_lane_treats_a_refusing_proxy_as_the_blocked_state(
    output: str, had_data: bool, kind: str
) -> None:
    assert recorder.classify_failure(output, had_data=had_data).kind == kind


def test_a_stream_start_refused_by_the_proxy_releases_the_slot_and_warns_once_with_the_right_fix() -> None:
    failure = recorder.classify_failure(recorder.scrub(f"{STREAMLINK_407} {PROXY}", PROXY), had_data=False)
    assert failure.detail == "proxy refused: 407 Proxy Authentication Required"
    # Not a crash and not a failed session: the slot is released and the platform backs off.
    outcome = after_failure(failure, ever_recorded=False, failures=1, max_reconnects=3)
    assert outcome is not None and outcome.blocked and outcome.status == "released"

    now = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
    state, notices = live_health.note_blocked(
        {}, "youtube", now, failure.detail, notify_after=2, backoff_s=1800
    )
    assert notices == [] and live_health.backing_off(state, "youtube", now) is not None  # quiet back-off
    state, notices = live_health.note_blocked(
        state, "youtube", now, failure.detail, notify_after=2, backoff_s=1800
    )
    assert [(n.severity, n.event) for n in notices] == [("warn", "live.source_blocked")]  # one warn
    body = notices[0].body
    assert "proxy refused: 407 Proxy Authentication Required" in body
    assert "proxy account" in body and "not cookies" in body and "YOUTUBE_COOKIES_FILE" not in body
    state, notices = live_health.note_blocked(
        state, "youtube", now, failure.detail, notify_after=2, backoff_s=1800
    )
    assert notices == []  # nothing more while it stays blocked
    state, notices = live_health.note_ok(state, "youtube", now)
    assert [(n.severity, n.event) for n in notices] == [("info", "live.source_recovered")]  # one recovery
    assert state == {}


def test_a_platform_block_still_names_the_platform_fix() -> None:
    now = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
    _, notices = live_health.note_blocked({}, "youtube", now, BOT_CHECK, notify_after=1, backoff_s=1800)
    assert "YOUTUBE_COOKIES_FILE" in notices[0].body and "proxy account" not in notices[0].body


# ── the doctor ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("stderr", "detail"),
    [
        (E407, "proxy refused: 407 Proxy Authentication Required"),
        (E407_SUSPENDED, "proxy refused: 407 Account is suspended"),
        (E402, "proxy refused: 402 Payment Required"),
        (E403, "proxy refused: 403 Forbidden"),
        (E_REFUSED, "proxy refused: no answer (connection refused)"),
    ],
)
def test_the_window_download_row_says_the_proxy_refused(stderr: str, detail: str) -> None:
    def refused(command: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
        return 1, "", f"{stderr} {PROXY}"

    access = Access(proxy=PROXY)
    check = doctor.timed(
        "window_download", lambda: doctor.check_window_download("dQw4w9WgXcQ", access, refused)
    )
    assert (check.name, check.status, check.error_class) == (
        "window_download",
        "blocked",
        "ProxyRefusedError",
    )
    assert check.detail.startswith(detail) and "s3cr3tpass" not in check.detail

    table = doctor.format_table([check])
    assert "window_download  BLOCKED" in table and detail in table
    assert "Refused by the proxy (PROXY_URL): window_download." in table and "proxy account" in table
    assert "Blocked from this machine" not in table  # that hint (set a proxy, add cookies) would mislead


def test_the_chat_replay_row_says_the_proxy_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ProxyError("402 Payment Required")

    real_client = innertube.new_client
    monkeypatch.setattr(
        doctor.innertube,
        "new_client",
        lambda proxy, cookies: real_client(None, cookies, httpx.MockTransport(handler)),
    )
    check = doctor.timed("chat_replay", lambda: doctor.check_chat_replay("dQw4w9WgXcQ", Access(proxy=PROXY)))
    assert (check.name, check.status) == ("chat_replay", "blocked")
    assert check.detail.startswith("proxy refused: 402 Payment Required") and "s3cr3tpass" not in check.detail


def test_the_doctor_keeps_its_other_verdicts() -> None:
    bot = doctor.failure("window_download", ytdlp.error_for(ytdlp.classify_failure(1, BOT_CHECK)))
    assert (bot.status, bot.error_class) == ("blocked", "DownloadBlockedError")
    assert not bot.detail.startswith("proxy refused")
    gone = doctor.failure(
        "window_download", ytdlp.error_for(ytdlp.classify_failure(1, "ERROR: Private video"))
    )
    assert (gone.status, gone.error_class) == ("error", "VideoUnavailableError")
    table = doctor.format_table([bot, gone])
    assert "Blocked from this machine: window_download" in table and "Refused by the proxy" not in table
