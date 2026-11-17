# Progress log

One entry per milestone, newest last. Each entry records: milestone ID, date, what was built, deviations from the plan, open issues, and what the next milestone must know. Orchestration state lives in `development-plan/ORCHESTRATION.md`.

---

## M00 — Repo bootstrap, tooling, config, logging

**Date:** 2026-11-16/17 UTC (finished 2026-11-17 ~00:15 UTC) · **Branch:** `m00-repo-bootstrap`

### What was built

- `pyproject.toml`: project `sixstars` 0.1.0, Python `>=3.12,<3.13`, all runtime dependencies from the milestone file, dev dependency group (`pytest pytest-asyncio respx ruff mypy types-requests`), script `sixstars = "sixstars.cli:app"`, hatchling build, ruff (line length 110, rules E,F,I,B,UP,N,SIM), mypy strict on `src/` with the `pydantic.mypy` plugin, pytest config (`asyncio_mode = "auto"`). `uv.lock` committed.
- Package tree from `docs/03-repo-and-stack.md` §2 under `src/sixstars/`: 93 modules, every stub has a role docstring naming the milestone that implements it; every subpackage has `__init__.py`.
- `src/sixstars/config.py`: `Env(BaseSettings)` with every variable in `config/.env.example`, cached accessor `env()`, uncached `load_env()`, `Env.require(name)` for call-time checks (raises `MissingConfigError`).
- `src/sixstars/logging.py`: structlog JSON to stdout with `ts` (ISO UTC), `level`, `event`, `env`, `service`; `get_logger(service)`, `bind_context(**kw)`, `clear_context()`, `configure_logging(level)`.
- `src/sixstars/cli.py`: typer app with `version` plus placeholders `api`, `live-manager`, `worker --queue`, `scheduler`, `discovery run-once`, `vod process <url>`, `spikes scan <chat.json>`, `clip render <spike_id>`, `post hourly --dry-run`, `settings get/set`.
- Tests: `tests/unit/test_smoke.py`, `test_config.py`, `test_logging.py` (25 tests), `tests/conftest.py`; empty `tests/integration/` and `tests/fixtures/`.
- `.github/workflows/ci.yml`: on push and pull request: setup uv, `uv sync --locked --all-extras`, `ruff check .`, `ruff format --check .`, `mypy --strict src/`, `pytest tests/unit`.
- `.gitignore` additions (`data/`, `*.ts`), `.editorconfig`, `.env.example` at the repo root (byte copy of `config/.env.example`, enforced by a test).
- `docs/00-decisions.md`: new section "Decisions made during the build" with D100–D105.

### Local results

`uv sync`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy --strict src/`, `uv run pytest` (25 passed) all green on Windows, Python 3.12. `uv run sixstars version` prints `sixstars 0.1.0 (env=dev)` when no `.env` is loaded.

### Deviations from the milestone file

1. **`.env` is loaded automatically.** `Env` reads process environment variables first, then a dotenv file: `.env` in the working directory, or the path in `SIXSTARS_ENV_FILE` (empty string disables file loading). On a machine with the staging `.env` present, `uv run sixstars version` therefore prints `env=staging`; the acceptance output `env=dev` is what a checkout without `.env` prints (CI, or `SIXSTARS_ENV_FILE= uv run sixstars version`).
2. **Almost every value is optional.** Only `APP_ENV`, `SERVICE_NAME`, `LOG_LEVEL`, `DATA_DIR`, `DB_POOL_MAX`, `APIFY_YOUTUBE_ACTOR` and `TWITCH_DOWNLOADER_CLI` have defaults; everything else is `None` when unset or empty. Empty values count as unset (`env_ignore_empty`), unknown keys are ignored (`extra="ignore"`). `SERVICE_NAME` defaults to `sixstars`.
3. **Types.** `DATABASE_URL` and `PROXY_URL` are `SecretStr` (both embed credentials) rather than plain `str`. `YOUTUBE_COOKIES_FILE` and `TWITCH_DOWNLOADER_CLI` are `Path`. `TWITCH_CLIENT_ID` and `YOUTUBE_OAUTH_CLIENT_ID` are plain `str`.
4. **Placeholder commands exit with code 1** after printing "not implemented yet", so a stub is never mistaken for a successful run.
5. **Dev tools are a PEP 735 dependency group** (`[dependency-groups] dev`), which `uv sync` installs by default, not an optional extra. There are no extras yet, so `--all-extras` in CI is a no-op kept for when a `media` extra appears.
6. **`faster-whisper` and `panns-inference` stayed in the main dependencies**: they installed without trouble on the dev machine, so no `media` extra was needed.
7. **Ruff excludes `*.md` and `.claude/`.** Ruff 0.17 formats Python code blocks inside Markdown and wanted to rewrite `development-plan/M04-chat-spike-engine.md` and `docs/05-chat-spike-engine.md`.
8. **Not created in M00** (they belong to later milestones): `docker/`, the deploy workflows, `ai/prompts/*.md`, `api/routes_*.py`, `assets/fonts|sfx|brand`.
9. **Committed on `m00-repo-bootstrap`, not `develop`**: the orchestrator merges.

### Open issues

- **CI has not run on GitHub.** The branch was not pushed (the orchestrator does that), so the "CI passes on GitHub" acceptance test is unverified. The workflow uses `astral-sh/setup-uv@v6` and `actions/checkout@v4`; uv installs Python 3.12 from `requires-python`.
- **Linux install size is unverified.** `panns-inference` pulls in PyTorch. Whether the locked Linux wheel is the CPU build or the much larger CUDA build was not checked; M11 should check the image size and, if needed, pin `torch` to the PyTorch CPU index through `[tool.uv.sources]`.
- `development-plan/ORCHESTRATION.md` is untracked and was left that way (it is the orchestrator's file and names project refs and n8n ids; the repo is public).

### Notes for M01

- Read config with `from sixstars.config import env`; get a required value with `env().require("DATABASE_URL")` (returns the plain string, raises `MissingConfigError` if unset). `SecretStr` fields need `.get_secret_value()` if read directly. Never log either.
- Tests: `tests/conftest.py` has an autouse fixture that disables dotenv loading, deletes every `Env` variable from the process environment and clears the `env()` cache. Integration tests that need a real `DATABASE_URL` must set it inside the test or fixture with `monkeypatch.setenv` (from a separately named variable such as `TEST_DATABASE_URL`), then call `config.env.cache_clear()`.
- The local `.env` has extra keys that are not `Env` fields and are ignored: `SUPABASE_PROJECT_REF`, `SUPABASE_DB_PASSWORD`, `SSH_HOST`, `SSH_USER`, `SSH_KEY_PATH`, `UPLOAD_POST_PROFILE`. If code needs one of them (M10 will need the Upload-Post profile name), add it to `Env` **and** to `config/.env.example` and the root `.env.example`: `test_env_example_matches_env_fields` and `test_root_env_example_is_a_copy` fail otherwise.
- Empty in the staging `.env` today (so `None`): `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `GROQ_API_KEY`, `YOUTUBE_OAUTH_REFRESH_TOKEN`, `PROXY_URL`.
- Logging: `get_logger("worker")` then `log.info("event_name", key=value)`; call `bind_context(job_id=...)` at the start of a job and `clear_context()` at the end. The module is named `sixstars.logging`; inside it the standard library module is imported as `stdlib_logging`.
- `db.py`, `settings.py`, `jobs.py`, `events.py`, `notify.py` and `services/worker.py` exist as docstring-only stubs: replace their contents, keep the docstrings.
- CI runs `pytest tests/unit` only; M01 adds the Postgres service and the integration test step.
- Stub modules must stay importable under `mypy --strict`; third-party packages without type stubs (for example `chat_downloader`, `panns_inference`) will need a `[[tool.mypy.overrides]]` entry with `ignore_missing_imports` when first imported.
