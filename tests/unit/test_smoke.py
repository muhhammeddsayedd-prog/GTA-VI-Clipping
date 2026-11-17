"""Smoke tests: the package imports and the CLI entry point works."""

import pytest
from typer.testing import CliRunner

import sixstars
from sixstars.cli import NOT_IMPLEMENTED, app

runner = CliRunner()


def test_package_imports() -> None:
    assert sixstars.__version__ == "0.1.0"


def test_cli_version_default_env() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == "sixstars 0.1.0 (env=dev)"


def test_cli_version_reports_app_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    result = runner.invoke(app, ["version"])
    assert result.output.strip() == "sixstars 0.1.0 (env=staging)"


@pytest.mark.parametrize(
    "args",
    [
        ["api"],
        ["live-manager"],
        ["scheduler"],
        ["vod", "process", "https://example.com/v"],
        ["post", "hourly", "--dry-run"],
    ],
)
def test_placeholder_commands(args: list[str]) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    assert NOT_IMPLEMENTED in result.output
