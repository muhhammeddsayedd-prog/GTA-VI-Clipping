"""Unit tests for environment configuration loading."""

import re
from pathlib import Path

import pytest
from pydantic import SecretStr

from sixstars import config
from sixstars.config import Env, MissingConfigError, env, load_env

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_defaults_without_any_environment() -> None:
    e = load_env()
    assert e.APP_ENV == "dev"
    assert e.DATA_DIR.as_posix() == "/data"
    assert e.DB_POOL_MAX == 10
    assert e.DATABASE_URL is None
    assert e.TWITCH_CLIENT_ID is None


def test_env_is_cached_singleton() -> None:
    assert env() is env()


def test_env_example_matches_env_fields() -> None:
    text = (REPO_ROOT / "config" / ".env.example").read_text(encoding="utf-8")
    names = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, flags=re.MULTILINE))
    assert names == set(Env.model_fields)


def test_root_env_example_is_a_copy() -> None:
    assert (REPO_ROOT / ".env.example").read_bytes() == (REPO_ROOT / "config" / ".env.example").read_bytes()


def test_dotenv_file_empty_values_unknown_keys_and_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "APP_ENV=staging   # staging | production\n"
        "TWITCH_CLIENT_ID=\n"
        "GROQ_API_KEY=\n"
        "DB_POOL_MAX=\n"
        "PROXY_URL=\n"
        "SSH_HOST=203.0.113.5\n"
        "UPLOAD_POST_PROFILE=default\n"
        "ANTHROPIC_API_KEY=sk-test\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(config.ENV_FILE_VAR, str(dotenv))
    e = load_env()
    assert e.APP_ENV == "staging"
    assert e.TWITCH_CLIENT_ID is None
    assert e.GROQ_API_KEY is None
    assert e.PROXY_URL is None
    assert e.DB_POOL_MAX == 10
    assert not hasattr(e, "SSH_HOST")
    assert isinstance(e.ANTHROPIC_API_KEY, SecretStr)
    assert "sk-test" not in repr(e)
    assert e.require("ANTHROPIC_API_KEY") == "sk-test"


def test_process_environment_wins_over_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("APP_ENV=staging\n", encoding="utf-8")
    monkeypatch.setenv(config.ENV_FILE_VAR, str(dotenv))
    monkeypatch.setenv("APP_ENV", "production")
    assert load_env().APP_ENV == "production"


def test_require_raises_for_missing_value() -> None:
    with pytest.raises(MissingConfigError, match="UPLOAD_POST_API_KEY"):
        load_env().require("UPLOAD_POST_API_KEY")
