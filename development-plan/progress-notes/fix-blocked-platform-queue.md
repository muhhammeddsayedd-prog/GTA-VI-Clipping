# fix-blocked-platform-queue — a blocked platform no longer starves the working one

Date: 2026-11-17. Branch `fix-blocked-platform-queue` (from `develop`). Not a milestone: a production-readiness
fix found on the staging server.

## What happened on staging

From the Hetzner data-centre address YouTube refuses video downloads (bot check); YouTube search and chat
replay work, Twitch works fully. Discovery ranked both platforms together and queued the best videos. Every
YouTube video hit the block, went back to `new` with `skip_reason = download_blocked: …` and was retried
every 30 minutes (M08). `discovery.max_vod_backlog` (24) counted those rows, filled up with 24 blocked
YouTube videos, and discovery then queued nothing at all, Twitch included. The same shape existed for live
slots: discovery re-assigned streams of a platform the live lane had just released as blocked (M14 note).

## What changed

| Where | Change |
|---|---|
| `src/sixstars/vod/health.py` (new) | The VOD lane's blocked state per platform in the settings key `vod.source_health` (`{platform: {since, last_blocked_at, blocks, error, access}}`). Written in `process._blocked` (every classified refusal), removed in `process._process` when a video ends with at least one downloaded window. Writes read the row `for update`, so two workers do not overwrite each other; both writers never raise. |
| `src/sixstars/discovery/blocked.py` (new) | The pure decisions: `vod_room` (backlog), `vod_limits` (0 or the probe), `overflow` (bound on waiting blocked videos), `live_limits`, and the summary lines. |
| `discovery/assign.py` | `pick_videos(..., platform_limit=, held=)` and `plan_slots(..., platform_limit=)` (`SlotPlan.held`). A video or stream passed over for its platform takes nothing from the total, so the next best one of another platform is taken. |
| `discovery/store.py` | `videos_waiting()` replaced by `video_queues()` (per platform: waiting / waiting out a block / processing); `blocked_waiting()`, `give_up_blocked()`. |
| `discovery/run.py` | Loads both states (never raises), trims, computes the limits, passes them to the permission checks, the slot plan and the video pick; notes and skip reasons. `score_candidate` applies `discovery.platform_weight`. |
| `discovery/config.py`, `discovery/scoring.py` | `BLOCKED_DEFAULTS`, three config fields, `apply_platform_weight`. |
| `supabase/migrations/0013_blocked_platforms.sql`, `docs/16-settings-reference.md` | Four new keys; the description of `discovery.max_vod_backlog` brought up to date (its value is untouched). |

### Rules

1. **VOD queueing.** A platform is blocked while `vod.source_health` has a refusal for it younger than
   `discovery.blocked_platform_stale_s`. Its candidates and creators are still stored and scored (ranking
   history is kept), none of its videos is queued, and the pass says so once: `skipped.platform_blocked`
   (videos above the cutoff that were held back) and one line in `notes`. No notification, no event beyond
   the usual `discovery.run`; the VOD lane's own "downloads are blocked" message (once per hour) is unchanged.
2. **Probe: the existing retry is the probe.** While at least one video of the blocked platform is open
   (`new` or `processing`), discovery queues none: those videos already have a job that tries again every
   `vod.blocked_backoff_s` (30 min), one of them really tries (the others are stopped by the lane's gate),
   and the first window that downloads removes the record. Only when no video of the platform is open does
   discovery queue exactly one, the best, as the probe; the next pass sees it open and queues no more. This
   was chosen over "one per interval" because it needs no extra clock or state: "is something already
   probing" is a count of rows.
3. **Safety net.** If the record is not refreshed for `discovery.blocked_platform_stale_s` (2 h, four
   back-offs) it stops counting, so a record that nobody clears (a probe video without spikes ends `done`
   with no download; a lost job) cannot hold a platform back for ever.
4. **Backlog.** `discovery.max_vod_backlog` counts videos in `new` that are not waiting out a block. A
   blocked platform therefore cannot stop another one.
5. **Bound.** While a platform is blocked, at most `discovery.max_blocked_waiting` (12) of its videos stay
   waiting; the oldest beyond that become `skipped` with the reason "gave up waiting: …", their queued jobs
   are cancelled, one `video.skipped` event each, one line in `notes`. Videos of a platform whose block has
   lifted are never trimmed; they drain as before.
6. **Live slots.** A platform in `live.source_health` whose `skip_until` is in the future gets no new slot
   (`skipped.live_platform_blocked`, one note); the slots go to the next best streams of other platforms.
   After the back-off it gets at most one new slot per pass, which is the session the live lane probes
   with. When every platform that has streams is blocked, nothing is assigned and the note says so. As in
   the live manager, a changed access (proxy or cookies added) ends the back-off at once.
7. **Permission checks.** Candidates of a platform that gets nothing in this pass no longer use up the
   in-pass checks (`discovery.permission_checks_per_run`); otherwise blocked YouTube videos at the top of
   the ranking would have kept unchecked Twitch creators from being picked.
8. **Platform weight.** `discovery.platform_weight` (`{}` by default) multiplies a finished score per
   platform, clamped to 0–100, noted in `score_parts.platform_weight`. No platform preference in code.

## Settings (migration 0013)

| Key | Default | |
|---|---|---|
| `discovery.platform_weight` | `{}` | factor per platform, missing = 1 |
| `discovery.max_blocked_waiting` | `12` | videos per platform kept waiting out a block |
| `discovery.blocked_platform_stale_s` | `7200` | how long a refusal counts without being refreshed |
| `vod.source_health` | `null` | written by code |

`discovery.max_vod_backlog` stays seeded at 24. Staging was raised to 60 by hand; with this fix 24 is enough
again and the value can be set back, but nothing depends on it.

## What the next deploy does on staging

- Apply migration 0013 (the code also runs without it, on the same defaults).
- The record `vod.source_health` is empty until the VOD lane's next refusal (at most one back-off, 30 min).
  A discovery pass in that gap can still queue YouTube videos once; from the first refusal on it does not.
- The first pass with the record trims the waiting YouTube videos to the 12 newest (expect that many
  `video.skipped` events once).

## Open issues

- A blocked platform is detected by window downloads and caption requests only, as before. Chat replay
  working says nothing about downloads, so it does not clear the record.
- The record is cleared by a downloaded window. A probe video that ends without any spike does not clear
  it; the stale time covers that case (up to 2 h later than ideal).
- The `platform_weight` clamp at 100 can tie several boosted candidates at the top; ties are broken by
  source id as everywhere.
- Not verified on the staging server or against real platforms; only with fakes against the test database.

## Tests

- `tests/unit/discovery/test_blocked_platforms.py`: queueing with one blocked and one working platform,
  backlog full of blocked videos, probe, bound, record and stale time, live slots, platform weight.
- `tests/integration/test_discovery_blocked.py`: the staging situation end to end (24 YouTube videos in
  `new` with `download_blocked`, Twitch candidates → Twitch is queued), block lifted, probe, stale record,
  live slots with one and with every platform blocked, the live probe, platform weight, migration 0013.
- `tests/integration/test_vod_process.py`: the bot check writes the record, a later refusal counts up, the
  first downloaded window removes it.
