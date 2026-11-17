"""Environment configuration loaded with pydantic-settings. `Env` declares every variable listed in
`config/.env.example`; values come from process environment variables first and then from a dotenv file
(`.env` in the working directory, or the path in `SIXSTARS_ENV_FILE`; set that variable to an empty
string to disable file loading). Unknown keys are ignored and empty values count as unset, so a missing
secret never crashes import: each client checks the values it needs at call time, using `require()`.
Runtime thresholds do not live here; they are read from the `settings` table (see `sixstars.settings`).
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE_VAR = "SIXSTARS_ENV_FILE"
DEFAULT_ENV_FILE = ".env"


class MissingConfigError(RuntimeError):
    """Raised when code asks for an environment value that is not set."""


class Env(BaseSettings):
    """Typed view of the process environment. Field names match the variable names exactly."""

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=True,
    )

    # Environment
    APP_ENV: Literal["staging", "production", "dev", "test"] = "dev"
    SERVICE_NAME: str = "sixstars"
    LOG_LEVEL: str = "INFO"
    DATA_DIR: Path = Path("/data")

    # Supabase
    SUPABASE_URL: str | None = None
    SUPABASE_SERVICE_ROLE_KEY: SecretStr | None = None
    DATABASE_URL: SecretStr | None = None
    DB_POOL_MAX: int = 10

    # Control API
    CONTROL_API_TOKEN: SecretStr | None = None
    DASHBOARD_API_TOKEN: SecretStr | None = None  # the admin console; refused on the trigger routes
    PUBLIC_API_HOST: str | None = None

    # n8n
    N8N_NOTIFY_WEBHOOK_URL: str | None = None
    N8N_NOTIFY_SECRET: SecretStr | None = None

    # Anthropic
    ANTHROPIC_API_KEY: SecretStr | None = None

    # Transcription (express path)
    GROQ_API_KEY: SecretStr | None = None
    OPENAI_API_KEY: SecretStr | None = None

    # Twitch
    TWITCH_CLIENT_ID: str | None = None
    TWITCH_CLIENT_SECRET: SecretStr | None = None

    # YouTube
    YOUTUBE_API_KEY: SecretStr | None = None
    YOUTUBE_OAUTH_CLIENT_ID: str | None = None
    YOUTUBE_OAUTH_CLIENT_SECRET: SecretStr | None = None
    YOUTUBE_OAUTH_REFRESH_TOKEN: SecretStr | None = None
    YOUTUBE_COOKIES_FILE: Path | None = None

    # Apify
    APIFY_TOKEN: SecretStr | None = None
    APIFY_YOUTUBE_ACTOR: str = "streamers/youtube-scraper"

    # Upload-Post
    UPLOAD_POST_API_KEY: SecretStr | None = None

    # Downloads (PROXY_URL embeds credentials, so it is a secret)
    PROXY_URL: SecretStr | None = None
    TWITCH_DOWNLOADER_CLI: Path = Path("/usr/local/bin/TwitchDownloaderCLI")

    def require(self, name: str) -> str:
        """Return the plain value of variable `name`, or raise `MissingConfigError` if it is unset."""
        value = getattr(self, name)
        if value is None:
            raise MissingConfigError(f"environment variable {name} is required but not set")
        if isinstance(value, SecretStr):
            return value.get_secret_value()
        return str(value)


def _env_file() -> str | None:
    """Path of the dotenv file to read, or None when file loading is disabled."""
    return os.environ.get(ENV_FILE_VAR, DEFAULT_ENV_FILE) or None


def load_env() -> Env:
    """Build a fresh `Env` from the current process environment and dotenv file (no caching)."""
    return Env(_env_file=_env_file())


@lru_cache(maxsize=1)
def env() -> Env:
    """Cached singleton accessor for the environment configuration."""
    return load_env()
