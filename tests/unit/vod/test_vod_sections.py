"""Window downloads: section maths, the yt-dlp arguments and what happens to the files."""

from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from sixstars.factory import tools
from sixstars.vod import sections
from sixstars.vod.sections import Section
from sixstars.vod.ytdlp import Access, DownloadBlockedError, YtDlpError

URL = "https://www.youtube.com/watch?v=abcdefghijk"
Runner = Callable[[Sequence[str], float], tuple[int, str, str]]


def media(duration_s: float, has_video: bool = True) -> tools.MediaInfo:
    return tools.MediaInfo(
        width=1920, height=1080, duration_s=duration_s, has_video=has_video, has_audio=True
    )


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """`tools.probe` answers from this mapping of file name to duration (missing: unreadable)."""
    durations: dict[str, float] = {}

    def fake(path: Path, ffprobe: str = "ffprobe") -> tools.MediaInfo:
        if path.name not in durations:
            raise tools.FfmpegError("probe failed")
        return media(durations[path.name])

    monkeypatch.setattr(sections.tools, "probe", fake)
    return durations


def writing_runner(out_dir: Path, name: str, extra: Sequence[str] = ()) -> tuple[Runner, list[list[str]]]:
    commands: list[list[str]] = []

    def runner(command: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
        commands.append(list(command))
        (out_dir / f"{name}.mp4").write_bytes(b"video")
        for suffix in extra:
            (out_dir / f"{name}{suffix}").write_bytes(b"left over")
        return 0, "", ""

    return runner, commands


# ── section maths ────────────────────────────────────────────


def test_section_is_the_padded_window_rounded_to_milliseconds() -> None:
    section = sections.section_for(4485.00049, 4575.0006)
    assert (section.start_s, section.end_s) == (4485.0, 4575.001)
    assert section.duration_s == pytest.approx(90.001)
    assert section.spec == "*4485.000-4575.001"


def test_section_never_starts_before_the_video() -> None:
    assert sections.section_for(-12.0, 60.0) == Section(0.0, 60.0)


def test_section_never_ends_after_the_video() -> None:
    assert sections.section_for(17950.0, 18040.0, 18000.0) == Section(17950.0, 18000.0)
    assert sections.section_for(100.0, 190.0, 0) == Section(100.0, 190.0)  # unknown length


def test_a_section_outside_the_video_is_refused() -> None:
    with pytest.raises(ValueError, match="outside the video"):
        sections.section_for(18050.0, 18140.0, 18000.0)
    with pytest.raises(ValueError):
        sections.section_for(50.0, 50.4)


# ── arguments ────────────────────────────────────────────────


def test_arguments_download_one_section_at_a_capped_height(tmp_path: Path) -> None:
    arguments = sections.build_arguments(URL, Section(4485, 4575), tmp_path, "spike-1", max_height=720)
    assert arguments[arguments.index("--download-sections") + 1] == "*4485.000-4575.000"
    selector = arguments[arguments.index("-f") + 1]
    assert selector.startswith("bv*[height<=720][vcodec^=avc1]+ba[ext=m4a]/")
    assert "bv*[height<=720]+ba/b[height<=720]" in selector
    assert arguments[arguments.index("--output") + 1] == str(tmp_path / "spike-1.%(ext)s")
    assert arguments[arguments.index("--merge-output-format") + 1] == "mp4"
    assert "--force-keyframes-at-cuts" not in arguments
    assert arguments[-1] == URL


def test_exact_cuts_ask_for_a_re_encode(tmp_path: Path) -> None:
    arguments = sections.build_arguments(URL, Section(0, 60), tmp_path, "s", max_height=1080, exact_cuts=True)
    assert "--force-keyframes-at-cuts" in arguments


# ── download ─────────────────────────────────────────────────


def test_download_runs_yt_dlp_with_proxy_and_cookies_and_checks_the_file(
    tmp_path: Path, probe: dict[str, float]
) -> None:
    probe["spike-1.mp4"] = 89.9
    runner, commands = writing_runner(tmp_path, "spike-1", extra=(".f399.mp4.part", ".temp.mp4"))
    cookies = tmp_path / "cookies.txt"
    access = Access(proxy="http://user:pw@proxy:1", cookies_file=cookies)

    done = sections.download_section(
        URL,
        Section(4485, 4575),
        tmp_path,
        "spike-1",
        access=access,
        max_height=1080,
        timeout_s=60,
        runner=runner,
    )

    assert done.path == tmp_path / "spike-1.mp4" and done.start_s == 4485 and done.duration_s == 89.9
    command = commands[0]
    assert command[command.index("--proxy") + 1] == "http://user:pw@proxy:1"
    assert command[command.index("--cookies") + 1] == str(cookies)
    assert "--download-sections" in command
    assert sorted(path.name for path in tmp_path.iterdir()) == ["spike-1.mp4"]


def test_a_complete_file_from_an_earlier_attempt_is_reused(tmp_path: Path, probe: dict[str, float]) -> None:
    (tmp_path / "spike-1.mp4").write_bytes(b"video")
    probe["spike-1.mp4"] = 90.0

    def never(command: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
        raise AssertionError("yt-dlp must not run again")

    done = sections.download_section(
        URL,
        Section(100, 190),
        tmp_path,
        "spike-1",
        access=Access(),
        max_height=1080,
        timeout_s=60,
        runner=never,
    )
    assert done.duration_s == 90.0


def test_a_broken_or_short_file_is_downloaded_again(tmp_path: Path, probe: dict[str, float]) -> None:
    (tmp_path / "spike-1.mp4").write_bytes(b"half a file")
    probe["spike-1.mp4"] = 20.0  # less than half of the 90 s asked for
    assert sections.existing(tmp_path, "spike-1", Section(100, 190)) is None
    assert not (tmp_path / "spike-1.mp4").exists()

    (tmp_path / "spike-2.mp4").write_bytes(b"no moov atom")
    assert sections.existing(tmp_path, "spike-2", Section(100, 190)) is None
    assert not (tmp_path / "spike-2.mp4").exists()


def test_a_blocked_download_raises_and_leaves_no_files(tmp_path: Path, probe: dict[str, float]) -> None:
    def blocked(command: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
        (tmp_path / "spike-1.mp4").write_bytes(b"partial")
        (tmp_path / "spike-1.f140.m4a").write_bytes(b"partial")
        return 1, "", "ERROR: [youtube] abc: Sign in to confirm you're not a bot. Use --cookies"

    with pytest.raises(DownloadBlockedError):
        sections.download_section(
            URL,
            Section(100, 190),
            tmp_path,
            "spike-1",
            access=Access(),
            max_height=1080,
            timeout_s=60,
            runner=blocked,
        )
    assert list(tmp_path.iterdir()) == []


def test_a_run_that_ends_without_a_usable_file_is_an_error(tmp_path: Path, probe: dict[str, float]) -> None:
    def nothing(command: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
        return 0, "", ""

    with pytest.raises(YtDlpError) as caught:
        sections.download_section(
            URL,
            Section(100, 190),
            tmp_path,
            "spike-1",
            access=Access(),
            max_height=1080,
            timeout_s=60,
            runner=nothing,
        )
    assert caught.value.kind == "error" and "missing or too short" in caught.value.detail


def test_summarise_failures() -> None:
    assert sections.summarise_failures(["forbidden", "timeout", "forbidden"]) == "forbidden x2, timeout x1"
    assert sections.summarise_failures([]) == ""
