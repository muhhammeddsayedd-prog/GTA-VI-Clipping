# M00 — Repo Bootstrap, Tooling, Config, Logging

> **Project context (same in every milestone).** 6 Stars Clips is a fully automated Python system that finds trending GTA 6 creators on Twitch and YouTube, detects the best moments from chat-activity spikes (live and in replays), renders branded 1080×1920 clips with English subtitles, approves them with an automatic quality gate, and posts them via the Upload-Post API to TikTok (accounts `tiktok_main` for hourly posts and `tiktok_live` for spike posts), Instagram Reels and YouTube Shorts. Two posting lanes: **spike** (S-tier live spikes, posted within ~4 minutes, priority 1) and **hourly** (one clip per hour per account, priority 2). State lives in Supabase Postgres (+ Storage bucket `clips`); background work uses a Postgres job queue (`jobs` table, `claim_job()`); runtime thresholds live in the `settings` table; n8n (separate VPS) only fires schedules, reads the takedown Gmail inbox and relays Telegram notifications by calling this system's control API. Environments: **staging** (GTA 5 content, private test accounts, branch `develop`) and **production** (GTA 6, real accounts, branch `main`). Launch: **2026-11-18 11:00 UTC**. Standing rules: `CLAUDE.md`. Progress log: `development-plan/PROGRESS.md`.

**Window:** Mon 22:00–23:00 UTC · **Requires:** nothing · **Launch-critical:** yes

## Goal
A clean, runnable Python project skeleton with the standards every later milestone relies on: package layout, dependency management, env config, structured logging, CLI entry point, test/lint/type-check tooling and CI.

## Context you need
- The repo is empty except for this kit (CLAUDE.md, README.md, docs/, development-plan/, supabase/, config/, assets/, n8n/).
- Stack: Python 3.12, `uv`, FastAPI, psycopg 3, httpx, tenacity, structlog, pydantic-settings, typer, pytest, ruff, mypy.
- Package name `sixstars`, source in `src/sixstars/`. Entry point command `sixstars` (typer app in `src/sixstars/cli.py`).
- Env vars are listed in `config/.env.example` (copy it to `.env.example` at the repo root too).

## Build
1. `pyproject.toml` with project `sixstars`, Python `>=3.12,<3.13`, dependencies:
   `fastapi uvicorn[standard] psycopg[binary,pool] httpx tenacity structlog pydantic pydantic-settings typer anthropic supabase websockets chat-downloader yt-dlp streamlink faster-whisper panns-inference numpy pillow python-multipart`
   dev: `pytest pytest-asyncio respx ruff mypy types-requests`.
   Script: `sixstars = "sixstars.cli:app"`. Configure ruff (line length 110, select E,F,I,B,UP,N,SIM) and mypy strict for `src/`.
   (If `panns-inference` or `faster-whisper` cause install trouble on the dev machine, put them in an optional extra `media` and install that extra in Docker; record it in PROGRESS.md.)
2. Create the package tree from `docs/03-repo-and-stack.md` §2 with empty modules + docstrings (one-paragraph role description each). Every subpackage gets `__init__.py`.
3. `src/sixstars/config.py`: `class Env(BaseSettings)` with every variable in `config/.env.example` (types: `APP_ENV: Literal["staging","production","dev","test"]`, URLs as `str`, secrets as `SecretStr`, `DATA_DIR: Path = Path("/data")`, `DB_POOL_MAX: int = 10`). Singleton accessor `env()` (cached). Missing secrets must not crash import; clients check what they need at call time.
4. `src/sixstars/logging.py`: structlog JSON to stdout with keys `ts` (ISO UTC), `level`, `event`, `env`, `service`; helper `get_logger(service)`; `bind_context(**kw)` for clip/spike/job ids.
5. `src/sixstars/cli.py`: typer app with placeholder subcommands from `docs/03` §3 (`api`, `live-manager`, `worker --queue`, `scheduler`, `discovery run-once`, `vod process`, `spikes scan`, `clip render`, `post hourly`, `settings get/set`), each printing "not implemented yet" except `version` which prints the package version and env.
6. `tests/unit/test_smoke.py` (imports package, CLI `version` works via typer's CliRunner).
7. `.github/workflows/ci.yml`: on push/PR: setup uv, `uv sync --all-extras`, `ruff check`, `ruff format --check`, `mypy src`, `pytest tests/unit` (integration tests are added in M01 with a Postgres service).
8. `.gitignore` (Python, .env, data/, *.mp4, *.ts, .venv), `.editorconfig`.
9. `development-plan/PROGRESS.md` with a header and the M00 entry.
10. Copy `config/.env.example` → `.env.example` at repo root.

## Acceptance tests
- `uv sync` succeeds; `uv run sixstars version` prints `sixstars 0.1.0 (env=dev)`.
- `uv run ruff check`, `uv run mypy src`, `uv run pytest` all pass.
- CI passes on GitHub.

## Definition of done
Skeleton committed on `develop`; CI green; PROGRESS.md created.

## Handoff to M01
M01 adds `db.py`, `settings.py`, `jobs.py`, `events.py`, `notify.py` and integration tests against Postgres.
