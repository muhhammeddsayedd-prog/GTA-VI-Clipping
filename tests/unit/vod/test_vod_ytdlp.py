"""yt-dlp runs of the VOD lane: how a failure is classified, and what goes on the command line."""

from collections.abc import Sequence
from pathlib import Path

import pytest

from sixstars import config
from sixstars.vod import ytdlp
from sixstars.vod.ytdlp import Access, DownloadBlockedError, VideoUnavailableError, YtDlpError

# The exact text YouTube answers video requests with from the Hetzner servers (yt-dlp 2026.10.26).
BOT_CHECK = (
    "ERROR: [youtube] dQw4w9WgXcQ: Sign in to confirm you're not a bot. Use --cookies-from-browser or "
    "--cookies for the authentication. See  https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-"
    "cookies-to-yt-dlp  for how to manually pass cookies. Also see  "
    "https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies  for tips on effectively "
    "exporting YouTube cookies"
)


def test_the_bot_check_is_a_block_not_a_failure_of_the_video() -> None:
    failure = ytdlp.classify_failure(1, BOT_CHECK)
    assert failure.kind == "blocked"
    assert "Sign in to confirm you're not a bot" in failure.detail
    assert isinstance(ytdlp.error_for(failure), DownloadBlockedError)


def test_the_bot_check_is_recognised_with_a_typographic_apostrophe() -> None:
    text = "WARNING: something else\nERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies"
    assert ytdlp.classify_failure(1, text).kind == "blocked"


def test_the_bot_check_wins_over_other_words_in_the_same_output() -> None:
    text = BOT_CHECK + "\nERROR: Video unavailable"
    assert ytdlp.classify_failure(1, text).kind == "blocked"


@pytest.mark.parametrize(
    "line",
    [
        "ERROR: [youtube] abc: Unable to download webpage: HTTP Error 429: Too Many Requests",
        "ERROR: [youtube] abc: This content isn't available, try again later.",
        "ERROR: [youtube] abc: The page needs to be reloaded.",
    ],
)
def test_rate_limits_are_blocks(line: str) -> None:
    assert ytdlp.classify_failure(1, line).kind == "blocked"


@pytest.mark.parametrize(
    "line",
    [
        "ERROR: [youtube] abc: Private video. Sign in if you've been granted access to this video",
        "ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader",
        "ERROR: [youtube] abc: Join this channel to get access to members-only content like this video",
        "ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate for some users.",
        "ERROR: [youtube] abc: The uploader has not made this video available in your country",
        "ERROR: [twitch:vod] 123: Video 123 does not exist",
        "ERROR: [youtube] abc: This live event will begin in 3 hours.",
    ],
)
def test_a_video_nobody_can_fetch_is_unavailable(line: str) -> None:
    failure = ytdlp.classify_failure(1, line)
    assert failure.kind == "unavailable"
    assert isinstance(ytdlp.error_for(failure), VideoUnavailableError)


def test_a_403_from_the_media_server_is_its_own_kind() -> None:
    text = "[https @ 000001] HTTP error 403 Forbidden\nERROR: ffmpeg exited with code 1"
    failure = ytdlp.classify_failure(1, text)
    assert failure.kind == "forbidden" and failure.detail == "ERROR: ffmpeg exited with code 1"
    error = ytdlp.error_for(failure)
    assert type(error) is YtDlpError and error.kind == "forbidden"


def test_timeout_and_anything_else() -> None:
    assert ytdlp.classify_failure(ytdlp.EXIT_TIMEOUT, "timed out after 900 s").kind == "timeout"
    assert ytdlp.classify_failure(1, "ERROR: Unable to download webpage: Connection reset").kind == "error"
    assert ytdlp.classify_failure(2, "").detail == "exit code 2"


def test_last_error_line_prefers_error_lines() -> None:
    assert ytdlp.last_error_line("ERROR: one\nWARNING: two\n\n") == "ERROR: one"
    assert ytdlp.last_error_line("just this\nand that") == "and that"
    assert ytdlp.last_error_line("") == ""


def test_base_command_adds_proxy_and_cookies_only_when_given(tmp_path: Path) -> None:
    plain = ytdlp.base_command(Access())
    assert "--proxy" not in plain and "--cookies" not in plain
    assert plain[1:3] == ["-m", "yt_dlp"] and "--ignore-config" in plain and "--no-playlist" in plain
    cookies = tmp_path / "cookies.txt"
    full = ytdlp.base_command(Access(proxy="http://user:secret@proxy:8000", cookies_file=cookies))
    assert full[full.index("--proxy") + 1] == "http://user:secret@proxy:8000"
    assert full[full.index("--cookies") + 1] == str(cookies)


def test_access_described_never_shows_values(tmp_path: Path) -> None:
    access = Access(proxy="http://user:secret@proxy:8000", cookies_file=tmp_path / "c.txt")
    assert access.described == "PROXY_URL set, YOUTUBE_COOKIES_FILE set"
    assert Access().described == "PROXY_URL not set, YOUTUBE_COOKIES_FILE not set"


def test_access_for_reads_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setenv("PROXY_URL", "http://user:secret@proxy:8000")
    monkeypatch.setenv("YOUTUBE_COOKIES_FILE", str(cookies))
    config.env.cache_clear()
    monkeypatch.setattr(ytdlp, "proxy_for", lambda platform: "http://p" if platform == "youtube" else None)

    assert ytdlp.access_for("youtube") == Access(proxy="http://p", cookies_file=cookies)
    assert ytdlp.access_for("twitch") == Access()


def test_a_cookies_file_that_does_not_exist_is_not_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YOUTUBE_COOKIES_FILE", str(tmp_path / "missing.txt"))
    config.env.cache_clear()
    assert ytdlp.access_for("youtube") == Access()


def test_without_proxy_and_cookies_nothing_is_added() -> None:
    assert ytdlp.access_for("youtube") == Access()


def test_run_returns_stdout_and_passes_the_timeout() -> None:
    seen: list[tuple[Sequence[str], float]] = []

    def runner(command: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
        seen.append((command, timeout_s))
        return 0, "out", ""

    assert ytdlp.run(["--version"], access=Access(), timeout_s=12, what="test", runner=runner) == "out"
    assert seen[0][0][-1] == "--version" and seen[0][1] == 12


def test_run_raises_the_classified_error_and_hides_the_proxy() -> None:
    proxy = "http://user:secret@proxy:8000"

    def blocked(command: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
        return 1, "", f"proxy {proxy} in use\n{BOT_CHECK} via {proxy}"

    with pytest.raises(DownloadBlockedError) as caught:
        ytdlp.run(["x"], access=Access(proxy=proxy), timeout_s=5, what="test", runner=blocked)
    assert "secret" not in str(caught.value) and "<proxy>" in caught.value.detail
    assert caught.value.kind == "blocked"

    def timeout(command: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
        return ytdlp.EXIT_TIMEOUT, "", "timed out after 5 s"

    with pytest.raises(YtDlpError) as late:
        ytdlp.run(["x"], access=Access(), timeout_s=5, what="test", runner=timeout)
    assert late.value.kind == "timeout"


def test_run_subprocess_reports_a_missing_program_and_a_timeout() -> None:
    code, _, stderr = ytdlp.run_subprocess(["sixstars-no-such-program-xyz"], 5)
    assert code == ytdlp.EXIT_NOT_STARTED and "Error" in stderr
    import sys

    code, _, stderr = ytdlp.run_subprocess([sys.executable, "-c", "import time; time.sleep(30)"], 0.5)
    assert code == ytdlp.EXIT_TIMEOUT and "timed out" in stderr
