# 20 — Launch Runbook

Launch: **Wednesday 18 Nov 2026, 11:00 UTC (13:00 Cairo)**. Times below are UTC.

## T−36 h → T−4 h: build and staging (milestones M00–M15)
See `development-plan/00-overview.md` for the schedule.

## T−4 h (07:00): production deploy — M16
- [ ] Staging acceptance checklist (docs/19 §4) passed; results in PROGRESS.md.
- [ ] Merge `develop` → `main`; production deploy succeeds; `/v1/health` OK.
- [ ] Production Supabase: migrations applied, `supabase/seed/production.sql` run.
- [ ] Settings check: `env.target_game = "Grand Theft Auto VI"`, `launch.launch_at = 2026-11-18T11:00:00Z`, `launch.dry_run = true`, `control.emergency_stop = false`.
- [ ] Upload-Post profiles `6stars-main` (TikTok main, Instagram, YouTube) and `6stars-live` (TikTok live) connected; a test call to the analytics endpoint works for both.
- [ ] YouTube OAuth refresh token for the production channel works (`sixstars youtube whoami`).
- [ ] n8n `[PROD]` workflows created, credentials set, all active.
- [ ] Telegram: test alert received from production.

## T−4 h → T−30 min: production dry run
- [ ] Discovery finds GTA 6 streams (pre-launch: trailer reactions, countdown/preload streams) — check `/v1/status`.
- [ ] Live slots recording; spikes appearing; clips rendering and passing the gate.
- [ ] Queue holds ≥ 24 approved hourly clips by 10:30. If not: lower `discovery.vod_score_cutoff` to 35, `gate.hourly.min_score` to 6 for launch day only, and widen `env.search_terms` (e.g. add "GTA 6 trailer", "GTA 6 reaction").
- [ ] Dry-run hourly slots at 08:00, 09:00, 10:00 logged correctly (posts rows with `dry_run = true`).
- [ ] Spend in budget.

## T−30 min (10:30): go decision
- [ ] Set `launch.dry_run = false` (posting still waits for `launch_at`).
- [ ] Health check (`/v1/status`) green, posted to Telegram automatically at 10:45 by the scheduler.

## T (11:00): launch
- Hourly slot fires at 11:00 (+0–5 min jitter) and posts the top clip to TikTok main, Instagram and YouTube.
- Spike lane is live from 11:00.
- Nothing to do by hand.

## T+1 h → T+48 h: watch (passive)
- Telegram alerts only. Expect the first daily report at 00:00 UTC Thursday.
- GTA 6 releases on Thu 19 Nov: expect a surge in live streams; `live.max_slots` can be raised to 8 if CPU allows (check `/v1/status`).
- After 2026-11-25: change WF03 to hourly.

## If something goes wrong
| Symptom | Action |
|---|---|
| Emergency stop fired | Read the alert; check the claimed posts; `/start` only after removing the cause |
| Queue empty | Lower thresholds as above; check discovery alert messages |
| Express too slow | Lower `live.max_slots`; set `factory.preset_express = ultrafast`; consider a GPU server |
| An account paused (cap/limit) | Nothing: it resumes automatically; if it repeats, lower its `cap_24h` |
| Upload-Post errors on one platform | Check the connection in the Upload-Post dashboard (token may need reconnecting) |
