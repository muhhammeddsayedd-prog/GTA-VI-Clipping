"""Twitch replay: parsing a trimmed real TwitchDownloaderCLI file and the download wrapper with a fake CLI."""

import json
import shutil
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from sixstars.chat import twitch_replay
from sixstars.chat.models import ChatSourceError

CLI = Path("/opt/TwitchDownloaderCLI")


def _sample(fixtures_dir: Path) -> Path:
    return fixtures_dir / "twitch_replay_sample.json"


def _comment(offset: Any = 10, **message: Any) -> dict[str, Any]:
    body = {"body": "hello", "bits_spent": 0, "user_badges": []} | message
    return {
        "content_offset_seconds": offset,
        "commenter": {"display_name": "Some_User", "_id": "77", "name": "some_user"},
        "message": body,
    }


# --- parsing ----------------------------------------------------------------------------------------


def test_fixture_counts_kinds_and_metadata(fixtures_dir: Path) -> None:
    chat = twitch_replay.parse_file(_sample(fixtures_dir))
    assert chat.platform == "twitch"
    assert chat.source_id == "1000000001"
    assert chat.message_count == 200
    assert Counter(message.kind for message in chat.messages) == {"chat": 199, "donation": 1}
    assert chat.duration_s == 42450.0
    assert chat.game == "Grand Theft Auto V"
    assert chat.title == "GTA V roleplay stream"
    assert sum(message.is_mod for message in chat.messages) == 4
    assert not any(message.is_broadcaster for message in chat.messages)


def test_fixture_is_sorted_and_contains_the_burst(fixtures_dir: Path) -> None:
    chat = twitch_replay.parse_file(_sample(fixtures_dir))
    times = [message.t for message in chat.messages]
    assert times == sorted(times)
    assert (times[0], times[-1]) == (35.0, 35752.0)
    per_bucket = Counter(int(t) // 10 for t in times)
    assert per_bucket[3571] == 51  # the burst: about seven times the stream's median of 7 per 10 s
    assert sum(per_bucket[bucket] for bucket in range(3570, 3576)) == 197


def test_fixture_message_fields(fixtures_dir: Path) -> None:
    chat = twitch_replay.parse_file(_sample(fixtures_dir))
    donation = next(message for message in chat.messages if message.kind == "donation")
    assert donation.text == "Cheer100"
    bot = next(message for message in chat.messages if message.username == "StreamElements")
    assert bot.user_id.isdigit()
    assert all(message.user_id and message.username for message in chat.messages)


def test_source_id_argument_wins(fixtures_dir: Path) -> None:
    assert twitch_replay.parse_file(_sample(fixtures_dir), "555").source_id == "555"


def test_parse_sorts_and_skips_broken_comments() -> None:
    data = {
        "video": {"id": "9", "end": 120},
        "comments": [
            _comment(50),
            _comment(5.5),
            _comment(None),
            _comment("12"),
            {"content_offset_seconds": 7, "commenter": None, "message": {"body": "deleted account"}},
            "not a comment",
        ],
    }
    chat = twitch_replay.parse_chat_json(data)
    assert [message.t for message in chat.messages] == [5.5, 50.0]
    assert chat.duration_s == 120.0
    assert chat.source_id == "9"


def test_badges_bits_and_notice_kinds() -> None:
    data = {
        "comments": [
            _comment(1, user_badges=[{"_id": "broadcaster", "version": "1"}]),
            _comment(2, user_badges=[{"_id": "moderator", "version": "1"}]),
            _comment(3, bits_spent=500),
            _comment(4, user_notice_params={"msg-id": "resub"}),
            _comment(5, user_notice_params={"msg-id": "subgift"}),
            _comment(6, user_notice_params={"msg-id": "raid"}),
            _comment(7, user_notice_params={"msg-id": "announcement"}),
            _comment(8, user_notice_params={"msg-id": None}),
        ]
    }
    messages = twitch_replay.parse_chat_json(data).messages
    assert [message.kind for message in messages] == [
        "chat",
        "chat",
        "donation",
        "sub",
        "gift",
        "raid",
        "notice",
        "chat",
    ]
    assert messages[0].is_broadcaster and not messages[0].is_mod
    assert messages[1].is_mod and not messages[1].is_broadcaster
    assert (messages[0].user_id, messages[0].username) == ("77", "Some_User")


def test_missing_video_block_gives_no_metadata() -> None:
    chat = twitch_replay.parse_chat_json({"comments": []})
    assert (chat.messages, chat.duration_s, chat.title, chat.game, chat.source_id) == (
        [],
        None,
        None,
        None,
        "",
    )


def test_parse_rejects_files_that_are_not_chat(tmp_path: Path) -> None:
    with pytest.raises(ChatSourceError, match="comments"):
        twitch_replay.parse_chat_json({"video": {}})
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ChatSourceError, match="cannot read"):
        twitch_replay.parse_file(bad)
    bad.write_text("[]", encoding="utf-8")
    with pytest.raises(ChatSourceError, match="not a JSON object"):
        twitch_replay.parse_file(bad)
    with pytest.raises(ChatSourceError, match="cannot read"):
        twitch_replay.parse_file(tmp_path / "missing.json")


@pytest.mark.parametrize(
    "raw",
    [
        "2902449425",
        "v2902449425",
        " 2902449425 ",
        "https://www.twitch.tv/videos/2902449425",
        "https://www.twitch.tv/videos/2902449425?t=1h2m3s",
        "twitch.tv/somechannel/v/2902449425",
    ],
)
def test_normalise_vod_id(raw: str) -> None:
    assert twitch_replay.normalise_vod_id(raw) == "2902449425"


@pytest.mark.parametrize("raw", ["", "somechannel", "12ab", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"])
def test_normalise_vod_id_rejects_invalid(raw: str) -> None:
    with pytest.raises(ValueError):
        twitch_replay.normalise_vod_id(raw)


def test_build_command_matches_the_verified_cli_flags(tmp_path: Path) -> None:
    command = twitch_replay.build_command(CLI, "123", tmp_path / "chat.json", 8, tmp_path)
    assert command[:2] == [str(CLI), "chatdownload"]
    assert command[command.index("--id") + 1] == "123"
    assert command[command.index("--output") + 1] == str(tmp_path / "chat.json")
    assert command[command.index("--threads") + 1] == "8"
    assert command[command.index("--collision") + 1] == "Overwrite"
    assert command[command.index("--banner") + 1] == "false"
    # Images are only embedded with -E / --embed-images, which must stay off to keep the file small.
    assert "-E" not in command and "--embed-images" not in command


# --- download ---------------------------------------------------------------------------------------


class FakeCli:
    """Stands in for TwitchDownloaderCLI: plays back scripted results and writes the sample on success."""

    def __init__(self, sample: Path, results: list[tuple[int, str]]) -> None:
        self.sample = sample
        self.results = list(results)
        self.calls: list[tuple[list[str], dict[str, str], float]] = []

    def __call__(self, command: Sequence[str], env: Mapping[str, str], timeout_s: float) -> tuple[int, str]:
        self.calls.append((list(command), dict(env), timeout_s))
        code, stderr = self.results.pop(0)
        if code == 0:
            shutil.copyfile(self.sample, command[command.index("--output") + 1])
        return code, stderr


def test_download_runs_the_cli_and_parses_the_result(fixtures_dir: Path, tmp_path: Path) -> None:
    cli = FakeCli(_sample(fixtures_dir), [(0, "")])
    out = tmp_path / "nested" / "chat.json"
    chat = twitch_replay.download("v2902449425", out, cli_path=CLI, runner=cli, timeout_s=30.0)
    assert chat.message_count == 200
    assert chat.source_id == "2902449425"
    assert (
        out.exists() and json.loads(out.read_text(encoding="utf-8"))["video"]["game"] == "Grand Theft Auto V"
    )
    command, _, timeout_s = cli.calls[0]
    assert command[0] == str(CLI)
    assert command[command.index("--id") + 1] == "2902449425"
    assert command[command.index("--threads") + 1] == str(twitch_replay.DEFAULT_THREADS)
    assert timeout_s == 30.0


def test_download_without_out_path_leaves_no_file(fixtures_dir: Path) -> None:
    cli = FakeCli(_sample(fixtures_dir), [(0, "")])
    chat = twitch_replay.download("123", cli_path=CLI, runner=cli)
    written = Path(cli.calls[0][0][cli.calls[0][0].index("--output") + 1])
    assert chat.message_count == 200
    assert not written.exists()


def test_download_uses_the_configured_cli_path(fixtures_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWITCH_DOWNLOADER_CLI", "/somewhere/TwitchDownloaderCLI")
    cli = FakeCli(_sample(fixtures_dir), [(0, "")])
    twitch_replay.download("123", runner=cli)
    assert cli.calls[0][0][0] == str(Path("/somewhere/TwitchDownloaderCLI"))


def test_download_passes_the_proxy_in_the_environment(fixtures_dir: Path) -> None:
    cli = FakeCli(_sample(fixtures_dir), [(0, "")])
    twitch_replay.download("123", cli_path=CLI, runner=cli, proxy="http://user:pass@proxy:8080")
    command, env, _ = cli.calls[0]
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] == env["ALL_PROXY"] == "http://user:pass@proxy:8080"
    assert not any("proxy:8080" in part for part in command)


def test_download_retries_with_backoff_then_succeeds(fixtures_dir: Path) -> None:
    cli = FakeCli(_sample(fixtures_dir), [(1, "boom"), (-1, "timed out after 5 s"), (0, "")])
    delays: list[float] = []
    chat = twitch_replay.download("123", cli_path=CLI, runner=cli, sleep=delays.append)
    assert chat.message_count == 200
    assert len(cli.calls) == 3
    assert delays == [2.0, 8.0]


def test_download_raises_after_the_last_attempt(fixtures_dir: Path) -> None:
    cli = FakeCli(_sample(fixtures_dir), [(1, "first"), (1, "second"), (1, "ERROR: Invalid VOD ID")])
    delays: list[float] = []
    with pytest.raises(ChatSourceError, match="Invalid VOD ID"):
        twitch_replay.download("123", cli_path=CLI, runner=cli, sleep=delays.append)
    assert len(cli.calls) == 3
    assert delays == [2.0, 8.0]


def test_download_does_not_retry_when_the_cli_cannot_start(fixtures_dir: Path) -> None:
    cli = FakeCli(_sample(fixtures_dir), [(-2, "FileNotFoundError: no such file")])
    delays: list[float] = []
    with pytest.raises(ChatSourceError, match="FileNotFoundError"):
        twitch_replay.download("123", cli_path=CLI, runner=cli, sleep=delays.append)
    assert len(cli.calls) == 1
    assert delays == []


def test_real_runner_reports_a_missing_binary(tmp_path: Path) -> None:
    with pytest.raises(ChatSourceError, match="TwitchDownloaderCLI failed"):
        twitch_replay.download("123", cli_path=tmp_path / "no-such-binary", sleep=lambda _: None)


def test_download_rejects_a_bad_id_before_running_anything(fixtures_dir: Path) -> None:
    cli = FakeCli(_sample(fixtures_dir), [])
    with pytest.raises(ValueError):
        twitch_replay.download("not-a-vod", cli_path=CLI, runner=cli)
    assert cli.calls == []
