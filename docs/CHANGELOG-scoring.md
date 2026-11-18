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
| 2026-11-18 16:30 | PR #59 hotfix (flags still off): `scoring_log` sub-scores JSON-safe (every gate insert had failed on a Decimal score; decisions unaffected); transcript-picked moments excluded from percentile history; VOD fallback picks logged. | — | redeploy previous image | Gate rows logged from 16:34. |
| 2026-11-18 16:34:38 | Warm-up 15 min → 2 min; the creator's stored chat baseline (median of the last 5 sessions) while a stream has no baseline of its own. v9. | `warmup.v2` | `scripts/scoring_flag.py production warmup.v2 off` | **Kept.** 30 min, per hour vs the 3 h before: spikes 79 (99), confirmed 41 (40), clips 35 (41), rendered 32 (31), approved 3.9 (2.0), errors 0 (0.33), failed jobs 0 (0.33), AI $1.55 (1.43), flagged approvals 0. No trigger. No new stream started in the window, so the stored baseline was not used yet; all 10 recording sessions have saved one. |
