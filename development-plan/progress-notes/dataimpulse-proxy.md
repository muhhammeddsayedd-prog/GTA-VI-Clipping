# dataimpulse-proxy — YouTube video downloads through a sticky residential proxy

Date: 2026-11-18. Hotfix straight to `main` (owner's choice; `develop` held unreleased spike-hold work):
PR #54 (`1512da0`, deployed 14:38 UTC as `prod-1512da0`), follow-up PR #56; back-merged into `develop`
(PR #55). Not a milestone.

## Why

Production YouTube downloads failed with "Sign in to confirm you're not a bot" (the server's data-centre
IP is flagged). The owner bought a DataImpulse residential plan (5 GB, pay per GB, $1/GB): a plain HTTP
proxy, every port of 10000–20000 a sticky exit IP held ~30 min (port 823 rotates: not used).

## What changed

| Where | Change |
|---|---|
| `chat/net.py` + callers | `proxy_for(platform, lane)`; setting `proxy.lanes` = `["vod"]`. Live recording, discovery search and the chat CLI no longer use the proxy (they would have from the moment `PROXY_URL` was set). |
| `vod/proxy.py` (new) | Pure: `with_port`, `pick_port`, `cookies_plan`, `traffic_bytes`, `cost_usd`, `is_account_failure`, `budget_reached`, `low_traffic`. `job_accesses` = one `Access` per attempt (own random port, cookies per plan, PO-token URL, shared `TrafficMeter`). `record_usage`, `budget_pause`, `used_gb`, `status_line`. |
| `vod/ytdlp.py` | `Access` gains `cookies_held`, `sticky_port`, `pot_url`, `meter`; `described` unchanged across attempts (the back-off compares it). `--extractor-args youtubepot-bgutilhttp:base_url=…` with the proxy. Calls counted. |
| `vod/sections.py` | Through the proxy the format has no taller fallback than `downloads.max_height` (1080). Window bytes metered. |
| `vod/process.py` | `_run` = attempt loop; steps raise `_RefusedError` instead of `_blocked`. Retry on a new port; 401/402/407 not retried; after the last attempt the video waits as before. Chat replay goes through the job's session. Gate `proxy_budget`. DataImpulse alert title. #56: through sticky sessions a video refused on every port waits alone (no platform-wide block). |
| `guardian/health.py` | `BUDGETS` row `proxy` (alert at the budget). `check_proxy`: probe every `proxy.health_interval_s` (critical "DataImpulse proxy failing: <reason> — top up or check credentials"), warn at 80 % of `proxy.cycle_gb`. |
| `clients/proxy_probe.py` (new), `clients/fakes.py` | `ProxyProbe` / `FakeProxyProbe`. |
| `vod/store.py`, `cli.py` | `sixstars vod retry-waiting [--platform youtube]`. |
| `docker/docker-compose.yml` | Service `bgutil` (`brainicism/bgutil-ytdlp-pot-provider:2.0.2`); workers get `POT_PROVIDER_URL`. |
| `pyproject.toml`, `uv.lock`, `entrypoint.sh` | `yt-dlp[default,curl-cffi]`, `bgutil-ytdlp-pot-provider==2.0.2`. |
| `api/admin_queries.py`, `web/…/SpendView.tsx` | Spend page: DataImpulse provider, proxy budget ring. |
| `supabase/migrations/0025_proxy.sql` | Settings `proxy.*` (see docs/16 "Proxy"). |
| `config.py`, `.env.example` | `PROXY_STICKY_PORT_MIN`, `PROXY_STICKY_PORT_MAX`, `POT_PROVIDER_URL`. |

## Verified facts (docs and prod, 2026-11-18)

- yt-dlp 2026.10.26 is the latest stable; deno 2.9.8 in the image is found ("JS runtimes: deno-2.9.8").
- bgutil 2.0.2 (image and PyPI, 2026-10-07). The HTTP plugin sends `request.request_proxy` to the server
  (`getpot_bgutil_http.py`), so a token is minted from the same exit IP as the download.
- With no cookies yt-dlp's default clients do not ask for a PO token; bgutil is then idle. With
  `player_client=mweb` it mints a GVS token through bgutil (5/5 metadata OK vs 4/5 default, one mweb
  answer had only format 18): not switched, too few samples.
- Sticky ports: port 10000 twice = same IP, 10001 = another, neither the server's IP.
- Measured: a 60 s 1080p window ≈ 37.6 MB (AV1); pipeline windows (avc1 first) ≈ 50 MB each; ~10 MB
  fixed per video (chat replay, captions, calls).

## Deviations

- The brief's manual test asked for a full download of BKDDyA3urZs; a 60 s window was downloaded instead
  (the video is 1 h 43 min: ~3.9 GB in full). BKDDyA3urZs is no longer in the table.
- "8 waiting" were mostly GTA 5 videos closed by the GTA 6 switch; one was waiting.
- `YOUTUBE_COOKIES_FILE` is set but the file is absent on the server, so the cookies attempt never runs.

## Open issues

- `replay.scan` of a live session whose YouTube stream is still live fails after 3 attempts ("its chat is
  not a replay yet"); seen once (VZqAPdUvqnQ). Before the proxy the bot check stopped it earlier.
- `proxy.overhead_mb_per_call` (2 MB) is an estimate: compare our GB with the DataImpulse dashboard after
  a day and adjust.
- After a top-up set `proxy.cycle_gb` and `proxy.cycle_started_at`.
