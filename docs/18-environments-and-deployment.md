# 18 — Environments and Deployment

## 1. Environments

| | Staging | Production |
|---|---|---|
| Git branch | `develop` | `main` |
| Server | Hetzner Cloud, 4 vCPU / 8 GB (shared vCPU is fine) | Hetzner, 16 dedicated vCPU / 32–64 GB (Cloud CCX line or a dedicated AX server), ≥ 500 GB disk |
| Supabase | `6stars-staging` (new project, Pro org) | `6stars-production` (existing project) |
| Domain | `api-staging.<your-domain>` | `api.<your-domain>` |
| Target game | Grand Theft Auto V | Grand Theft Auto VI |
| Upload-Post profile | `6stars-staging` → private test accounts | `6stars-main`, `6stars-live` |
| `live.max_slots` | 3 | 6 |
| n8n workflows | `[STG] …` | `[PROD] …` |
| Posting | dry run, then private posts in M15 | dry run until launch_at, then public |

Server location: Hetzner Germany/Finland is fine (latency to Twitch/YouTube CDNs is good; audience location doesn't matter for posting).

If you don't own a domain, use free `sslip.io` style hostnames (`api.<server-ip>.sslip.io`) with Caddy's automatic HTTPS.

## 2. Server setup (both environments)

1. Ubuntu 24.04, SSH key login only, `ufw` allow 22/80/443.
2. Install Docker Engine + compose plugin.
3. Create user `deploy` in the docker group; `/opt/6stars` owned by deploy.
4. `/opt/6stars/.env` from `config/.env.example` (chmod 600).
5. `/opt/6stars/data` volume (scratch). `/opt/6stars/secrets` for cookie files.
6. DNS A record → server IP.

## 3. Containers (`docker/docker-compose.yml`)

| Service | Command | Replicas | Restart | Notes |
|---|---|---|---|---|
| caddy | caddy | 1 | always | ports 80/443, proxies to api:8080 |
| api | `sixstars api` | 1 | always | |
| live-manager | `sixstars live-manager` | 1 | always | |
| worker-express | `sixstars worker --queue express` | 2 | always | |
| worker-normal | `sixstars worker --queue normal` | 4 (staging 2) | always | |
| scheduler | `sixstars scheduler` | 1 | always | |

All share the same image and `/data` volume. `stop_grace_period: 60s` so renders can finish. Log driver `json-file` with rotation (max-size 50m, max-file 5).

Entrypoint script: `pip install -U yt-dlp streamlink` (fast, keeps them current), then exec the command.

## 4. Dockerfile outline

- Base `python:3.12-slim-bookworm`.
- apt: `ffmpeg` (or static build), `libicu72` (TwitchDownloaderCLI), `fonts-*` none (copy Montserrat), `curl`, `unzip`, `tini`.
- TwitchDownloaderCLI Linux x64 zip → `/usr/local/bin`.
- `uv sync --frozen --no-dev`.
- Pre-download faster-whisper `medium` and PANNs checkpoint into `/models` at build time.
- Non-root user, `TZ=UTC`, `tini` as PID 1.

## 5. CI/CD (GitHub Actions)

| Workflow | Trigger | Steps |
|---|---|---|
| `ci.yml` | PR + push to any branch | uv sync → ruff → mypy → pytest (unit + integration with a Postgres service container and migrations applied) |
| `deploy-staging.yml` | push to `develop` (after CI) | build image → push `ghcr.io/<owner>/6stars-clips:staging-<sha>` → `supabase db push` to staging → SSH to staging server: pull, `docker compose up -d` → call `/v1/health` |
| `deploy-production.yml` | push to `main` (after CI), plus manual approval via GitHub Environment `production` | same with `prod-<sha>` tag and production secrets |

GitHub secrets per environment: `SSH_HOST`, `SSH_USER`, `SSH_KEY`, `SUPABASE_ACCESS_TOKEN`, `SUPABASE_PROJECT_REF`, `SUPABASE_DB_PASSWORD`. The `.env` files stay on the servers (not in GitHub).

Rollback: `docker compose` with the previous image tag (`IMAGE_TAG` in `.env`), migrations are additive only.

## 6. Promotion rule

`develop` → `main` only after the M15 acceptance checklist passes in staging. Production settings differences live in `supabase/seed/production.sql` and the `.env`; code is identical.
