# 21 — Monitoring and Operations

## 1. What you receive on Telegram

| Severity | Examples | Delivery |
|---|---|---|
| 🔴 critical | emergency stop, takedown actioned, claim detected, post removal failed (manual removal needed), service restart storm, failover running, all accounts failing | instant |
| 🟠 warn | queue low, express slow, account paused by cap, spend cap reached, tool failures, disk 80% | instant (max 1 per type per 30 min) |
| 🔵 info | launch, daily report, deploys | instant for launch/deploys; everything else in the daily report |

## 2. Commands
`/status` · `/report` · `/stop` · `/start` (production bot only).

## 3. Routine (optional, the system doesn't need it)
- Daily: read the report.
- Weekly: check Supabase disk/egress, Hetzner disk, Upload-Post usage vs plan, Anthropic spend.
- Monthly: rotate `CONTROL_API_TOKEN`, refresh YouTube cookies if used, review `restricted`/`blocked` creators.

## 4. Data retention
| Data | Kept |
|---|---|
| Clip files (Storage) | 3 days after posting; rejected 24 h |
| Live buffers | 30 min rolling |
| Downloaded windows | deleted after render |
| Chat files | 7 days |
| `candidates`, `events` | 30 days (scheduler prunes) |
| `posts`, `clips`, `spikes`, `creators` | forever |

## 5. Tuning cheatsheet
| Want | Change |
|---|---|
| More spike posts | lower `spikes.z_s` (e.g. 3.6) or `spikes.s_min_confidence` (0.75) |
| Fewer, better clips | raise `gate.hourly.min_score` to 8 |
| Clips start too late | raise `spikes.lead_s` (max 30) or `live.chat_offset_s` |
| Clips start too early | lower `spikes.lead_s` (min 15) |
| Faster express | `factory.preset_express = ultrafast`, GPU + `factory.use_nvenc = true` |
| Fewer posts | lower `cap_24h` per account or remove hours from `tiktok_main.hourly_hours_utc` |
| Less spend | lower `discovery.max_vod_per_run`, `vod.max_spikes_per_video`, `ai.daily_budget_usd` |
