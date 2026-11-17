# M11 — Control API, Docker, CI/CD, Staging Deploy

> **Project context (same in every milestone).** 6 Stars Clips is a fully automated Python system that finds trending GTA 6 creators on Twitch and YouTube, detects the best moments from chat-activity spikes (live and in replays), renders branded 1080×1920 clips with English subtitles, approves them with an automatic quality gate, and posts them via the Upload-Post API to TikTok (accounts `tiktok_main` for hourly posts and `tiktok_live` for spike posts), Instagram Reels and YouTube Shorts. Two posting lanes: **spike** (S-tier live spikes, posted within ~4 minutes, priority 1) and **hourly** (one clip per hour per account, priority 2). State lives in Supabase Postgres (+ Storage bucket `clips`); background work uses a Postgres job queue (`jobs` table, `claim_job()`); runtime thresholds live in the `settings` table; n8n (separate VPS) only fires schedules, reads the takedown Gmail inbox and relays Telegram notifications by calling this system's control API. Environments: **staging** (GTA 5 content, private test accounts, branch `develop`) and **production** (GTA 6, real accounts, branch `main`). Launch: **2026-11-18 11:00 UTC**. Standing rules: `CLAUDE.md`. Progress log: `development-plan/PROGRESS.md`.

**Window:** Tue 19:30–21:30 UTC · **Requires:** M10 · **Launch-critical:** yes

## Goal
Package everything into one Docker image, run it as a set of services on the staging Hetzner server behind HTTPS, expose the control API for n8n, and automate deploys from GitHub.

## Context you need
### Control API (FastAPI, `sixstars api`, port 8080, behind Caddy)
All routes except `/v1/health` need `Authorization: Bearer <CONTROL_API_TOKEN>` (constant-time compare). Every trigger route writes a heartbeat (`trigger:discovery`, `trigger:hourly`, `trigger:guardian`, `trigger:stats`).

| Route | Does |
|---|---|
| `GET /v1/health` | process + DB ping → `{"ok":true,"env":…}` |
| `POST /v1/discovery/run` | `run_discovery()` (≤ 60 s) → summary |
| `POST /v1/posting/hourly` | `schedule_hourly_slot()` → `202 {"slot_at","fires_at"}` |
| `POST /v1/guardian/check` | placeholder → implemented in M13 (return 501 until then) |
| `POST /v1/guardian/takedown` | placeholder (M13) |
| `POST /v1/stats/collect` | placeholder (M13) |
| `GET /v1/reports/daily?format=telegram\|json` | placeholder (M13) |
| `GET /v1/status?format=telegram\|json` | live sessions, approved queue by lane, posts last hour/24 h by account, emergency stop, dry run, spend today |
| `POST /v1/control/stop` `{reason}` / `POST /v1/control/start` | set/clear `control.emergency_stop` (+ reason), event, alert |
| `POST /v1/control/settings` `{key,value}` | update a setting |

### Services (docker compose, one image)
| Service | Command | Replicas (prod/staging) |
|---|---|---|
| caddy | Caddy reverse proxy, automatic HTTPS for `PUBLIC_API_HOST` → api:8080 | 1 |
| api | `sixstars api` | 1 |
| live-manager | `sixstars live-manager` (stub until M14: heartbeat only) | 1 |
| worker-express | `sixstars worker --queue express` | 2 / 1 |
| worker-normal | `sixstars worker --queue normal` | 4 / 2 |
| scheduler | `sixstars scheduler` (stub until M13: heartbeat + ranking refresh every 10 min) | 1 |
All `restart: always`, shared volume `/data`, `.env` file, `stop_grace_period: 60s`, json-file logs rotated (50 MB × 5), `TZ=UTC`.

### Image
`python:3.12-slim-bookworm`; apt `ffmpeg` (≥ 6; else static build), `libicu72`, `curl`, `unzip`, `tini`, `fontconfig`; TwitchDownloaderCLI Linux x64 into `/usr/local/bin` (steps recorded in M03's PROGRESS entry); Montserrat fonts → `/usr/share/fonts/truetype/montserrat` + `fc-cache -f`; `uv sync --frozen --no-dev --extra media`; pre-download faster-whisper `medium` and the PANNs checkpoint into `/models` (set env vars so the libraries find them); non-root user; entrypoint upgrades `yt-dlp` and `streamlink` with pip, then `exec tini -- sixstars "$@"`.

### Servers
Staging: Hetzner 4 vCPU / 8 GB, Ubuntu 24.04, Docker + compose, user `deploy`, `/opt/6stars/{.env,data,secrets,docker-compose.yml,Caddyfile}`, ufw 22/80/443. DNS `api-staging.<domain>` (or `<ip>.sslip.io`).

### CI/CD (GitHub Actions)
- `ci.yml` (from M00/M01): lint, types, unit + integration tests.
- `deploy-staging.yml`: on push to `develop` after CI → build and push `ghcr.io/<owner>/6stars-clips:staging-<sha>` → `supabase db push` to the staging project (`SUPABASE_ACCESS_TOKEN`, `SUPABASE_PROJECT_REF`, `SUPABASE_DB_PASSWORD`) → SSH (`SSH_HOST`, `SSH_USER`, `SSH_KEY`): `cd /opt/6stars && IMAGE_TAG=staging-<sha> docker compose pull && docker compose up -d` → poll `/v1/health` for 2 minutes.
- `deploy-production.yml`: same for `main`, GitHub Environment `production` with required reviewer (you), tag `prod-<sha>`. (Used in M16.)

## Build
1. `api/main.py`, `api/auth.py`, `api/routes_*.py` + `/v1/status` implementation.
2. `services/worker.py` (already wired in M08), `services/scheduler.py` stub with ranking refresh, `services/live_manager.py` stub.
3. `docker/Dockerfile`, `docker/docker-compose.yml`, `docker/docker-compose.staging.yml` (replica overrides), `docker/Caddyfile`, `docker/entrypoint.sh`.
4. GitHub workflows above; document required secrets in PROGRESS.md.
5. Provision the staging server (script `scripts/provision_server.sh` that the owner runs once over SSH; idempotent).
6. Deploy staging.

## Acceptance tests
- Unit/integration: auth (missing/wrong token → 401), status route shape, stop/start toggles the setting.
- `curl https://api-staging.<domain>/v1/health` → ok over valid HTTPS.
- `curl -X POST -H "Authorization: Bearer …" …/v1/discovery/run` runs discovery on the server; VOD jobs are processed by the containerised workers into approved clips.
- `docker compose ps` all healthy; killing a container → it restarts.
- Pushing a trivial commit to `develop` deploys automatically.

## Definition of done
Staging runs the whole pipeline in containers; deploys are automatic; PROGRESS.md lists hostnames (no secrets).

## Handoff to M12
n8n needs: `API_BASE` (staging URL), the bearer token, and the notify webhook URL/secret to put into `.env` (`N8N_NOTIFY_WEBHOOK_URL`, `N8N_NOTIFY_SECRET`).
