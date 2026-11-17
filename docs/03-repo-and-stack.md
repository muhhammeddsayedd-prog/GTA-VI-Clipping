# 03 — Repo Layout, Stack and Standards

## 1. Stack

| Layer | Choice |
|---|---|
| Language | Python 3.12, `uv` for dependencies and virtualenv |
| Web/API | FastAPI + uvicorn |
| Async | asyncio (live-manager), sync code fine elsewhere |
| DB | Supabase Postgres via psycopg 3 (`psycopg[binary,pool]`) |
| Storage | Supabase Storage via `supabase` Python client (service role key, server only) |
| Video | ffmpeg 7.x (static build in the image), ffprobe |
| Streams | streamlink, yt-dlp (updated on container start), TwitchDownloaderCLI (Linux x64 binary) |
| Chat | Twitch IRC over websockets (`websockets`), `chat-downloader` |
| Speech | `faster-whisper` (CTranslate2, CPU int8), hosted Whisper API for express |
| Audio classification | `panns-inference` (AudioSet tagging, PyTorch CPU) for music detection |
| AI | `anthropic` SDK |
| HTTP | `httpx` with `tenacity` retries |
| Logging | `structlog` JSON |
| Config | `pydantic-settings` for env; `settings` table for runtime values |
| CLI | `typer` |
| Tests | `pytest`, `pytest-asyncio`, `respx` (HTTP mocks) |
| Quality | `ruff`, `mypy --strict` |
| Containers | Docker, docker compose; Caddy for HTTPS |
| CI/CD | GitHub Actions, images on GHCR, deploy over SSH |

## 2. Repo layout

```
.
├── CLAUDE.md
├── README.md
├── pyproject.toml
├── uv.lock
├── .env.example                      (copy of config/.env.example)
├── docs/                             (this kit)
├── development-plan/                 (milestones + PROGRESS.md)
├── supabase/
│   ├── config.toml
│   └── migrations/0001_init.sql, 0002_seed_settings.sql, ...
├── n8n/workflows/*.json              (exported workflows, one per file)
├── assets/
│   ├── fonts/                        (Montserrat Black, Inter — OFL)
│   ├── sfx/                          (CC0 sound effects) + LICENSES.md
│   ├── brand/                        (logo, watermark PNGs, end card)
│   └── profanity.txt
├── docker/
│   ├── Dockerfile
│   ├── docker-compose.yml            (production)
│   ├── docker-compose.staging.yml    (staging overrides)
│   └── Caddyfile
├── .github/workflows/
│   ├── ci.yml
│   ├── deploy-staging.yml
│   └── deploy-production.yml
├── src/sixstars/
│   ├── __init__.py
│   ├── config.py           env settings (pydantic-settings)
│   ├── settings.py         runtime settings from DB, cached 30 s
│   ├── db.py               connection pool, query helpers, transactions
│   ├── jobs.py             enqueue / claim / complete / fail / retry
│   ├── events.py           write events rows
│   ├── notify.py           Telegram via n8n webhook, with outbox
│   ├── storage.py          Supabase Storage upload/delete/signed URL
│   ├── logging.py
│   ├── clients/
│   │   ├── anthropic_client.py
│   │   ├── twitch.py       Helix (app token), IRC handled in chat/
│   │   ├── youtube.py      RSS, Data API (key + OAuth for pre-check)
│   │   ├── apify.py
│   │   ├── transcribe.py   local faster-whisper + hosted API, one interface
│   │   ├── upload_post.py
│   │   └── fakes.py        test doubles for all clients
│   ├── chat/
│   │   ├── models.py       ChatMessage dataclass
│   │   ├── filters.py      bots, notices, subs, raids
│   │   ├── twitch_irc.py   live
│   │   ├── youtube_live.py live
│   │   ├── twitch_replay.py TwitchDownloaderCLI wrapper
│   │   └── youtube_replay.py
│   ├── spikes/
│   │   ├── buckets.py      bucketing + token counting
│   │   ├── baseline.py     trailing mean/std
│   │   ├── scoring.py      weighted z-score
│   │   ├── detector.py     live + replay detectors (same core)
│   │   ├── window.py       clip window rules
│   │   └── confirm.py      Claude confirmation
│   ├── discovery/          sources.py, scoring.py, tiers.py, assign.py, run.py
│   ├── live/               manager.py, recorder.py, buffer.py, session.py
│   ├── vod/                process.py, sections.py, fallback.py
│   ├── factory/            transcript.py, writer.py, facecam.py, layout.py,
│   │                       subtitles.py, bleep.py, overlays.py, render.py, presets.py
│   ├── gate/               gate.py, checks.py, music.py, visual.py, profiles.py
│   ├── precheck/           youtube_precheck.py
│   ├── posting/            caps.py, precedence.py, hourly.py, spike.py,
│   │                       publisher.py, captions.py, ranking.py
│   ├── guardian/           claims.py, takedowns.py, health.py, stop.py, failover.py
│   ├── stats/              collector.py, report.py
│   ├── ai/                 prompts/*.md, schemas.py, calls.py, costs.py
│   ├── api/                main.py, auth.py, routes_*.py
│   ├── services/           live_manager.py, worker.py, scheduler.py (process entrypoints)
│   └── cli.py              `sixstars <command>`
└── tests/
    ├── unit/               pure logic, no network
    ├── integration/        DB + fakes
    └── fixtures/           chat json samples, short video samples
```

## 3. Entry points (CLI)

| Command | Runs |
|---|---|
| `sixstars api` | FastAPI control API |
| `sixstars live-manager` | live recorders + chat + live spikes |
| `sixstars worker --queue express` | express worker |
| `sixstars worker --queue normal` | normal worker |
| `sixstars scheduler` | heartbeats, failover, cleanup |
| `sixstars discovery run-once` | one discovery pass (manual/testing) |
| `sixstars vod process <url>` | process one video end-to-end (testing) |
| `sixstars spikes scan <chat.json>` | print spikes for a chat file (testing) |
| `sixstars clip render <spike_id>` | render one clip (testing) |
| `sixstars post hourly --dry-run` | simulate an hourly slot |
| `sixstars settings get/set <key> [value]` | read/change a runtime setting |

## 4. Coding standards

- Small modules, pure functions for logic; side effects at the edges.
- Dataclasses or Pydantic models for all data passed between modules.
- No global mutable state except the DB pool and the settings cache.
- Every module has a docstring explaining its role in one paragraph.
- SQL in `.sql` strings next to the code that uses it, parameterised only (no f-strings with values).
- Retries only around idempotent operations; posting uses Upload-Post `Idempotency-Key` = `post:<clip_id>:<account_id>`.
- Feature flags and thresholds come from `settings` (see `docs/16-settings-reference.md`).
- Tests: every milestone adds tests; unit tests must run in under 60 seconds total.

## 5. Git workflow

- `develop` → staging, `main` → production. Feature branches `m05-clip-factory` etc. merged into `develop`.
- Conventional commits (`feat:`, `fix:`, `chore:`, `test:`, `docs:`).
- CI must be green before merging.
