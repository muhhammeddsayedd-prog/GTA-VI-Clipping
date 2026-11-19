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
