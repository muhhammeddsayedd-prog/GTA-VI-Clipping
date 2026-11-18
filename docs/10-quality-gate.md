# 10 — Quality Gate

Replaces human approval. Every clip goes through it. Results are written to `clips.gate_result` as:
```json
{"profile":"hourly","passed":false,"checks":{"length":{"ok":true,"value":34.2},"music":{"ok":false,"value":0.22,"limit":0.10}, ...}}
```
The first failing check becomes `clips.reject_reason`. Never post a clip that failed.

## 1. Profiles

| Check | `hourly` profile | `express` profile |
|---|---|---|
| Creator tier | not `restricted`/`blocked`; `auto` creators under `gate.auto_creator_daily_cap` posted clips today | `preferred`, or `auto` **and** music_ratio = 0 |
| Length | 20–60 s | 15–45 s |
| Music ratio | ≤ 0.10 | = 0 |
| Spoiler | `spoiler = false` while now < `gate.spoiler_block_until`; spoilers never in hook/title (checked by P2 and a keyword check) | same |
| Safety | P2 safety flags all false; no `gate.reject_topics`; no slur as the focus | same |
| Watermark / other logos | P5 vision: no other account watermark, no TikTok/Instagram/YouTube logos, no "subscribe" end screens | same |
| Visual | ffmpeg `blackdetect` (d ≥ 2 s fails), `freezedetect` (n=0.003, d ≥ 2 s fails), also in the gameplay band of the clip's layout (`layout.gameplay_band`; layout C, facecam on top, has its own); P5: not a menu/loading/pause screen; facecam crop not empty | same |
| Subtitle sync | max |subtitle word start − transcript word start| ≤ 0.3 s | same |
| Score | P2 score ≥ 7 | ≥ 8 |
| Duplicate topic | no clip with the same `topic` approved/posted in the last 48 h | 6 h |
| Same source range | no other clip overlaps this source range (same source_id, ±30 s) | same |
| File | duration matches, has audio, 1080×1920, ≤ 100 MB | same |

## 2. Express fallback

If an express clip fails only express-specific checks (tier, music = 0, score 8, length 15–45) but would pass the hourly profile, it is **not rejected**: it is re-gated as hourly and goes to the top of the hourly queue (`lane = hourly`).

## 3. YouTube claim pre-check (hourly clips only)

A clip is **risky** if any is true: creator tier `auto` and first clip from this creator; music_ratio > 0; compilation; Claude safety flag "possible copyrighted media" (e.g., TV/film/music playing on stream).

Risky clips (when `precheck.enabled`, and under `precheck.daily_max` per day):
1. Status `precheck_pending`; job `clip.precheck`: upload to the **owner's own YouTube channel as private** using the owner's Google Cloud project (OAuth). (Unaudited projects can only create private videos — fine for this.)
2. Job `clip.precheck_result` after `precheck.wait_s` (30 min): `videos.list part=status,contentDetails` with OAuth. Fail if `status.uploadStatus = rejected` (reason claim/copyright), or `contentDetails.regionRestriction.blocked` is non-empty, or the video was removed.
3. Delete the private test video.
4. Pass → `approved`; fail → `rejected` (`reject_reason = precheck_claim`) and creator `claims_count + 1`.

Limits of the pre-check: the public API does not expose every Content ID claim (monetisation-only claims may not be visible). It catches blocks and rejections, which are the ones that hurt.

## 4. After passing

- `status = approved`, `approved_at = now()`. (`series_no` was already assigned with `nextval('clip_series_seq')` at render start, because the end card shows it; gaps from rejected clips are accepted.)
- Ranking (`docs/11` §5) computes `rank_score`.
- Upload MP4 to Storage.

## Design-style renders (development-plan/M18)

The gate judges the first render of a clip. The other design styles of the clip (`clip_renders`, job `clip.variants`) are rendered only after it approved the clip and get the technical checks only (frame size, file length within `gate.checks.duration_tolerance_s`, black and frozen picture within `max_black_s` / `max_freeze_s` in their own band). A render that fails is not posted: its accounts get the first render. Score, music, safety, duplicates and P5 are the same for every style (same source, same cut).
