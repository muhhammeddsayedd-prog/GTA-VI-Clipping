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
| 2026-11-18 17:06:33 | Reaction list extended (ICANT, LULW, OMEGALUL, pepeLaugh, KEKW*, KEKL, HAHA*, XD, ???), repeated-token bursts (3+) and YouTube custom emoji count as reactions. v10. | `reactions.extended` | `scripts/scoring_flag.py production reactions.extended off` | **Rolled back at 17:22:34 (v11).** Error trigger fired: 1 error event in 15 min (3.8/h vs 0.33/h), an age-gated YouTube video in the VOD lane (unrelated to scoring, but the rule is strict). Spikes 275/h vs 104/h, mostly a Twitch VOD replay batch (26) and YouTube transcript-fallback picks (30) that do not use chat; live Twitch spikes about doubled (21 vs 11 per 16 min). AI $2.65/h vs $1.59 (1.7x). Retry later with a narrower setting. |
| 2026-11-18 17:22:50 | Live: a stronger spike inside the 60 s merge gap is clipped and the earlier, weaker one cancelled if its render has not started. v12. | `merge.keep_stronger` | `scripts/scoring_flag.py production merge.keep_stronger off` | **Kept.** No trigger: errors 0 (0.67/h), failed jobs 0, AI $0.72/h (1.74). No supersede happened in the window (it only acts on two live spikes within 60 s). Renders fell to 12/h (37) because the four normal-queue workers are saturated by local Whisper (8/8 CPU, load 16, VOD renders up to 28 min) while 73 `vod.process` jobs wait: workload from the VOD lane, not this flag. |
| 2026-11-18 17:54:28 | Live spikes in the channel's top 3% (or provisional tier S) are not rejected by the chat-only P1 check (non-content rejects such as giveaways, raids and command spam stand) and go to the express render as tier S. v13. | `fastpath.live` | `scripts/scoring_flag.py production fastpath.live off` | **Kept.** No trigger: errors 0, failed jobs 0, AI $0.45/h (1.46). One spike took the fast path (p100 of its channel); P1 had confirmed it anyway; its express clip was rejected at the gate (score_too_low). Clips 12/h (41): the normal queue backlog (29 renders queued at 18:25). |
| 2026-11-18 18:25:26 | Adaptive cut: loudness + chat + speech anchor, the writer cuts 6-25 s (12-18 s target, 1-3 s lead and tail); gate minimum length 6 s for both profiles. v14. | `cut.adaptive` | `scripts/scoring_flag.py production cut.adaptive off` | **Rolled back 18:56:39 (v15), re-enabled 18:56:51 (v16) for a fresh window.** Error trigger fired: 9 `render_failed` (live cut file missing). All 9 jobs were queued 17:26-17:56, before this flag, and waited 54-82 min in the saturated normal queue, past `live.cut_keep_s` (45 min), so their cuts had been swept: caused by the backlog, not the flag. In the window: approved 9.8/h (2.0) just under the 5x line, clips 88/h (35) and rendered 57/h (27) as the queue drained faster; cut lengths 6.6-24.5 s, the anchor inside the cut in 23 of 26; AI $2.12/h (1.27). |
| 2026-11-18 18:56:51 | `cut.adaptive` re-enabled (see the row above). v16. | `cut.adaptive` | `scripts/scoring_flag.py production cut.adaptive off` | monitoring |
