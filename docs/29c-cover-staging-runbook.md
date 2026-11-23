# 29c — Cover engine: staging runbook (after the final merge)

The owner merges every session's work at the end (23 Nov). Everything below needs the cover engine on the
staging server, so it runs then, in this order. Production waits for the docs/29a section 10 gate.

## 0. Before turning anything on

1. **Migration numbers.** Check `supabase/migrations/` on develop for a clash with `0044_cover_engine.sql`; renumber if another session took 0044.
2. **The staging deploy built the image.** Its self-checks print `landmarker ok`, `rapidocr ok` and `chromium ok`. If the build failed, staging keeps the old image: fix and redeploy.
3. **The scheduler runs the jobs.** `sixstars worker` registers `cover.mine` and `cover.youtube_thumb` (worker log `handlers_registered`).

## 1. Turn the engine on (staging only)

```sql
update scoring_config set value = 'true'::jsonb where key = 'cover.enabled';
-- cover.platforms stays ["instagram","tiktok","youtube"]; TikTok image and YouTube custom stay false for now.
```

Rollback:

```sql
update scoring_config set value = 'false'::jsonb where key = 'cover.enabled';
```

## 2. Milestone checks

| Milestone | Run | Done when |
|---|---|---|
| C3 | Let 30 clips be mined (`events` type `cover.mined`); build `docs/reports/covers-c3.md`: per clip the top frame next to a random frame from the peak window, sides shuffled, the key in a separate file | The owner picks the top frame in ≥ 25 of 30 |
| C4 | `sixstars covers eval-lines --limit 50 --out /data/covers-c4.json`, after 00:00 UTC if the AI budget is short | ≥ 45 of 50 have a valid line, none invent (the `source` check enforces it); record the cost per 100 clips |
| C5 | Rebuild `docs/reports/covers-c5.md` from `covers-work/<clip_id>/*_3x4.jpg` of 30 clips | Every allowed layout renders, < 3 s per variant; owner review |
| C6 | Plant bad covers (wrong text, no face, dark, near-duplicate, spoiler line, too long, cut off) next to the C5 covers and run the QA | All plants rejected; ≥ 80% of the good ones pass; record the vision model and cost per 100 covers. If the "< 2% clipped" exposure rule rejects good covers, propose a new limit to the owner (do not change it alone) |
| C7 | Instagram stays off in staging (it can't post privately). TikTok: watch `cover_timestamp` on `SELF_ONLY` posts. **TikTok image test:** turn `cover.tiktok_image_enabled` on in staging for 10 `SELF_ONLY` posts | Screen-record the playback in the TikTok app: is the image a visible first frame? Then switch it back off |
| C7 YouTube | `cover.youtube_custom_enabled` on in staging (private videos; the probe showed it works on our non-Partner channel) | `covers.status = 'applied'` and the thumbnail is visible in Studio |
| C8 | Console → Covers shows each account's grid; `sixstars covers report --days 7` | Numbers match the `covers` table |

## 3. T: 48 h end to end and the fault drills

- **48 h at the normal cadence**, all three platforms (Instagram as dry run). Then run `sixstars covers report --days 2` and check the clip and post failure rates against the 48 h before.
- **Each fault drill, once:**
  - stop Chromium (`PLAYWRIGHT_BROWSERS_PATH=/nonexistent` for one worker);
  - rename the face landmarker model;
  - force a vision timeout (`ai.model.p13_cover_qa` set to an invalid model);
  - make Storage unreachable (a wrong `SUPABASE_URL` for one worker);
  - Upload-Post 5xx (the fake in tests; on staging, watch a real retry);
  - YouTube 403 (seen in the logs or forced).
- **What every drill must show:** the clip posts without a custom cover, and one Telegram alert (`cover.alert` once per kind per day).
- **Afterwards:** write the results into docs/CHANGELOG-scoring.md.

## 4. Production gate (docs/29a section 10)

The owner reviews the C5 and C6 sheets. Then take the backups (affected tables, and n8n workflows to `backups/n8n/<date>/`). Campaign lanes stay `frame_only` or `off`.

Switch on one step at a time, 24 h each:

1. `cover.enabled` with `cover.platforms = ["instagram"]`.
2. Add `"tiktok"`.
3. Bandit live.
4. `cover.tiktok_image_enabled`: only after the C7 test and the owner's yes.
5. `cover.youtube_custom_enabled`: only with the owner's yes.

Note: `cover.video_ready` (stage 0) moved to Render v2.
