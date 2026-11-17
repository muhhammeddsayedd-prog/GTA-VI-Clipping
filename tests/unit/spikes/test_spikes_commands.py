"""`sixstars spikes scan`: file loading, the table and the option handling. No database: the settings come
from a JSON file made from the seed migration."""

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from sixstars.chat import synthetic
from sixstars.chat.models import write_jsonl
from sixstars.cli import app
from sixstars.spikes import commands
from sixstars.spikes.config import SpikeConfig, SpikeConfigError
from sixstars.spikes.detector import scan_replay

runner = CliRunner()


@pytest.fixture
def settings_file(seed_values: dict[str, Any], tmp_path: Path) -> Path:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(seed_values), encoding="utf-8")
    return path


@pytest.fixture
def chat_file(tmp_path: Path) -> Path:
    path = tmp_path / "chat.jsonl"
    chat = synthetic.merge(
        synthetic.flat_chat(3000, 5),
        synthetic.burst(1200.0, 120, 80),
        synthetic.burst(2400.0, 30, 6, seed=9, user_prefix="small"),
    )
    write_jsonl(path, chat)
    return path


@pytest.mark.parametrize(
    ("seconds", "expected"), [(0, "00:00:00"), (59.9, "00:00:59"), (3725, "01:02:05"), (36000, "10:00:00")]
)
def test_clock(seconds: float, expected: str) -> None:
    assert commands.clock(seconds) == expected


def test_parse_overrides() -> None:
    assert commands.parse_overrides(["spikes.z_b=3", 'spikes.weights={"msgs": 1}']) == {
        "spikes.z_b": 3,
        "spikes.weights": {"msgs": 1},
    }
    for bad in ("spikes.z_b", "posting.enabled=true", "spikes.z_b={oops"):
        with pytest.raises(ValueError, match="--set"):
            commands.parse_overrides([bad])


def test_load_config_from_a_file_with_overrides(settings_file: Path, tmp_path: Path) -> None:
    assert commands.load_config(settings_file, {"spikes.lead_s": 30}).lead_s == 30.0
    wrong = tmp_path / "list.json"
    wrong.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(SpikeConfigError):
        commands.load_config(wrong, {})


def test_table_lists_the_spikes_best_first(cfg: SpikeConfig, chat_file: Path) -> None:
    spikes = commands.ranked(scan_replay(commands.load_chat(chat_file), config=cfg))
    lines = commands.format_table(spikes)
    assert lines[0].split() == ["#", "time", "tier", "score", "msgs", "uniq", "window", "top", "tokens"]
    assert lines[1].split()[:6] == ["1", "00:20:00", "S", "9.50", "125", "85"]
    assert "00:19:40-00:20:15" in lines[1]
    assert lines[2].split()[1] == "00:40:00"
    assert commands.top_tokens(spikes[0]).count(" x") == 3


def test_scan_command_prints_the_table_and_details(chat_file: Path, settings_file: Path) -> None:
    result = runner.invoke(
        app,
        [
            "spikes",
            "scan",
            str(chat_file),
            "--settings-file",
            str(settings_file),
            "--details",
            "--sample",
            "3",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "2 spikes (S=1, A=1, B=0), showing 2" in result.output
    assert "median 5 msgs per bucket" in result.output
    assert "[1] 00:20:00 tier S score 9.50 | msgs_z=+10.00 uniq_z=+10.00" in result.output
    assert "first bucket 00:20:00" in result.output
    detail = result.output.split("[1] ")[1].split("[2] ")[0]
    assert sum(line.strip().startswith("+") for line in detail.splitlines()) == 3


def test_scan_command_top_and_set_options(chat_file: Path, settings_file: Path) -> None:
    base = ["spikes", "scan", str(chat_file), "--settings-file", str(settings_file)]
    one = runner.invoke(app, [*base, "--top", "1"])
    assert "showing 1" in one.output and "00:40:00" not in one.output
    strict = runner.invoke(app, [*base, "--set", "spikes.min_messages=100"])
    assert "1 spikes" in strict.output
    assert runner.invoke(app, [*base, "--set", "nonsense"]).exit_code == 2
    assert runner.invoke(app, [*base, "--set", 'spikes.z_b="high"']).exit_code == 2


def test_scan_command_reports_bad_input(settings_file: Path, tmp_path: Path) -> None:
    missing = runner.invoke(
        app, ["spikes", "scan", str(tmp_path / "none.jsonl"), "--settings-file", str(settings_file)]
    )
    assert missing.exit_code == 1 and "cannot read" in missing.output
    no_database = runner.invoke(app, ["spikes", "scan", str(tmp_path / "none.jsonl")])
    assert no_database.exit_code == 2 and "DATABASE_URL" in no_database.output


def test_scan_finds_the_known_burst_of_the_real_twitch_sample(
    chat_fixtures: Path, settings_file: Path
) -> None:
    """M04 acceptance: the real burst (51 messages in the bucket at 09:55:10, stream median 7) is in the
    top 3. The sample holds only the minutes around the burst, so everything before it is silence and
    the whole burst (09:55:00 to 09:55:40) is one spike that opens in its first bucket."""
    sample = chat_fixtures / "twitch_replay_sample.json"
    result = runner.invoke(app, ["spikes", "scan", str(sample), "--settings-file", str(settings_file)])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[2].split()[:3] == ["1", "09:55:00", "S"]
    config = commands.load_config(settings_file, {})
    top = commands.ranked(scan_replay(commands.load_chat(sample), config=config))[:3]
    burst = 35710.0
    assert any(spike.window_start <= burst and burst + config.bucket_s <= spike.window_end for spike in top)
    assert (top[0].first_t, top[0].window_start, top[0].window_end) == (35700.0, 35680.0, 35740.0)
