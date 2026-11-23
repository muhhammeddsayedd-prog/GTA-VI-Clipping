# Scoring overhaul changelog

Every change to how spikes and clips are scored. Each behaviour sits behind a flag in the `scoring_config`
table (production Supabase). **Rollback = set the flag's `enabled` to false**; it takes effect within 30 s
(cache), with no redeploy:

```sql
update scoring_config set value = jsonb_set(value, '{enabled}', 'false') where key = '<flag>';
```

Every change to `scoring_config` writes a row to `scoring_config_history`; its `version` is stamped on
`spikes.scoring_version`, `clips.scoring_version` and every `scoring_log` row. `scoring_log` holds one row
per decision (stages `spike.confirm`, `factory.cut`, `gate`) with all sub-scores.

Monitor after each flip: `uv run python scripts/scoring_monitor.py production --since <flip time>` plus the
n8n failed executions. Rollback triggers: errors or failed jobs above the previous rate, approvals per hour
more than 5x, more than 20% of new approvals flagged (safety, spoiler, claim or removal), AI spend per hour
more than 3x.

Snapshots taken before any change (2026-11-18 ~15:45 UTC):
- Supabase tables copied to `backup_spikes_20261118`, `backup_clips_20261118`, `backup_live_sessions_20261118`,
  `backup_settings_20261118` (production).
- `backups/settings/2026-11-18-production.json` (all production settings), committed.
- n8n: Phase 1 touches no n8n workflow, so none was exported.

Funnel before Phase 1 (production, per hour, 18 Nov UTC; spikes → clips → approved → posted):
11h 40→20→1→0, 12h 66→22→4→3, 13h 79→34→2→2, 14h 120→45→3→3.

| Time (UTC) | Change | Flag | Rollback | Result after monitoring |
|---|---|---|---|---|
| 2026-11-18 16:06 | PR #58: code + migration 0026 deployed to production, every flag off (no behaviour change; config version 8 = the seed rows). Live sessions start saving their chat baseline every 5 min (data only). | — | flags are off | pending |
| 2026-11-19 ~06:00 | **Production ops (owner-approved):** TikTok hourly lane off: `platform_accounts.tiktok_main.lanes` `{hourly}` → `{}`. Reason: 0 plays on all 16 public TikTok posts (L0 diagnosis, `development-plan/progress-notes/L0-tiktok-diagnosis.md`). | — (data change) | `update platform_accounts set lanes = array_append(lanes,'hourly') where id='tiktok_main'` (also in the `account.lane_paused` event) | TikTok stopped posting |
| 2026-11-19 | L0 (learning loop): delivery guard + migration `0032_learning_delivery.sql` (posts.delivery_suspect / organic_only / boosted, platform_accounts.hourly_paused_until / hourly_pause_reason). Staging first. | `learning.delivery_guard` (off), `learning.exclude_tiktok` (on: TikTok kept out of Star Score/training) | flag off; `/resume <account>` clears a pause | pending (staging) |
| 2026-11-19 | L1: fixed-age snapshots (`post_metrics_snapshots`, migration `0033_learning_snapshots.sql`), snapshot pass first in each stats run, overflow to worker job `stats.collect` (`stats.job_budget_s` 240), `clients/youtube_analytics.py`; WF05 builder now every 30 min (applied per environment by hand). | `learning.snapshots` (off; `youtube_analytics: false` until the YouTube OAuth token exists) | flag off | pending (staging) |
| 2026-11-23 | Cover engine C0 (staging, probe only): spec `docs/29`, `29a`; re-verification and Upload-Post probe in `docs/29b-cover-probe.md`; `scripts/covers_probe.py`. Three private posts (TikTok `SELF_ONLY` ×2: `cover_timestamp`, `tiktok_cover_image_url`; YouTube private `X0n4aCpzTVI`: `thumbnail_url` → `thumbnail_pending`, then the set-thumbnail endpoint → 200). No code path, setting or migration changed. | — (none yet; `cover.*` keys come in C2) | — | All three accepted. YouTube set-thumbnail worked on a non-YPP channel (display on Shorts surfaces still to check). |
| 2026-11-23 | Cover engine C1: cover-ready video (stage 0). Layout C facecam 672 → 768 px; face-aware facecam crop (MediaPipe BlazeFace, layouts A/C) so the face fills ~40% of the panel; captions in the first 2 s and ±1 s around the peak shown as one still phrase (≤ 6 words / 26 chars, one row, no cut word); gamma lift of dark gameplay (luma < 50) in the first 1.5 s. Migration `0038_cover_video_ready.sql` (flag + `clips.peak_s/peak_source/facecam_h`); acceptance tool `sixstars covers check-video`. Length, cuts and audio unchanged (unit + real-ffmpeg tests). | `cover.video_ready` (off everywhere; staging on after deploy) | `update scoring_config set value = jsonb_set(value, '{enabled}', 'false') where key = 'cover.video_ready';` | Pending: 20-clip check on staging. |
| 2026-11-23 | Cover engine C2: migration `0039_cover_engine.sql`: `covers` table (one row per cover attempt, with `render_id`, `account_id`, `mode`, `config_version`), the `cover.*` keys of docs/29a §8 (`cover.lane_modes` keyed `normal`/`spike`/`campaign:<slug>`/`*`), buckets `covers-work` (private) and `covers-public` (public, Instagram `cover_url` only). `covers/config.py` (versioned snapshot, safety floors: exploration ≥ 0.2, QA score 0–10), `covers/store.py`, `storage.public_url`. | `cover.enabled` (off everywhere) | `update scoring_config set value = 'false' where key = 'cover.enabled';` | No behaviour yet (no stage reads it until C3+). |
