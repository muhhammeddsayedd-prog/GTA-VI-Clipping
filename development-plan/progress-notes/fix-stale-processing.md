# fix-stale-processing

Date: 2026-11-17. Branch `fix-stale-processing` (from `develop`).

## What happened on staging

The worker containers were restarted while a `vod.process` job was running. The `videos` row stayed in
`processing`. The lane counted videos in `processing` against `vod.max_concurrent` (1), so every other
`vod.process` job logged `vod_deferred` ("concurrency") and queued itself again every 120 s: 400 deferrals
in 15 minutes across 59 jobs, both workers idle. Two further faults made it permanent:

- the row only stopped counting after `vod.processing_stale_s` (2 hours);
- the released job of the zombie video itself ran again at once, found its own video "in work by another
  worker", ended as `ignored` (job `done`), and so the video had no job left.

## What changed

- **What counts as in work** (`vod/store.py: count_in_work`): a video in `processing` holds a slot only
  when a `vod.process` or `replay.scan` job is `running` for it (payload `video_id`, or `live_session_id`
  of its session), or it was updated in the last `vod.orphan_grace_s` (a run by hand with the CLI has no
  job). A dead worker's job stops being `running` through `jobs.requeue_stale` (scheduler) or
  `jobs.release` (SIGTERM), so nothing else is needed for that.
- **Atomic claim** (`store.claim`): the video and the slot are taken in one transaction under the advisory
  lock `vod.concurrency_slot`. Result `claimed`, `busy` (another worker has the video) or `full` (no
  slot). The separate count in `_gates` is gone, so two workers can no longer both pass the check.
- **A job resumes its own video**: when job `job_id` finds its video in `processing` and no other job is
  running for it, it takes it over at once (event `video.processing` with `resumed: true`). This is the
  normal path after a restart: the released job runs again and continues from the stored spikes/verdicts.
- **Recovery** (`process.recover_orphans`): `store.reset_orphans` puts every `processing` video with no
  running job, not updated for `vod.orphan_grace_s`, back to `new` with one `video.recovered` event
  (level warn) each. Then: a queued job resumes it; if the newest job is `failed` the video is closed with
  `give_up` (so a video that kills its worker every time cannot loop); if there is no job, or the newest
  one is `done`, a new `vod.process` job is queued (dedupe key `vod:<video_id>`, priority 30 like
  discovery). Runs from the scheduler (task `vod_recovery`: looks every 15 s, runs once per
  `vod.recovery_interval_s`, read each time) and at the start of every `vod.process` / `replay.scan` job.
- **Deferrals**: a job that finds no slot waits `vod.concurrency_retry_s` x 2^n, capped at
  `vod.concurrency_retry_max_s`, shortened by up to `vod.concurrency_retry_jitter`; n travels in the
  payload (`deferrals`) and is dropped after any other reason. `vod_deferred` is logged at info. Deferrals
  never used job attempts (the deferred job ends `done` and a new job with 0 attempts replaces it) and
  never used `videos.attempts`; a test now pins both.
- **Renders are not starved** (`process.concurrency_limit`, `slots.effective_concurrency`): the limit in
  force is `max(1, min(vod.max_concurrent, live normal workers - 1))`. Live workers are heartbeats named
  `worker-normal-*` not older than `vod.worker_fresh_s` (`jobs.live_workers`, new read-only helper).

## Settings (migration 0015_vod_recovery.sql, rows in docs/16)

| Key | Seed |
|---|---|
| `vod.orphan_grace_s` | 300 |
| `vod.recovery_interval_s` | 60 |
| `vod.concurrency_retry_max_s` | 600 |
| `vod.concurrency_retry_jitter` | 0.2 |
| `vod.worker_fresh_s` | 90 |

`vod.processing_stale_s` now only governs the takeover of a `processing` row by a run without a job (CLI
without `--force`).

## Operating notes

- With 2 `normal` workers the lane processes 1 video at a time whatever `vod.max_concurrent` says; with 4
  workers up to 3. With a single worker the floor of 1 applies and renders wait behind the video.
- Right after a restart the old workers' heartbeats stay fresh for `vod.worker_fresh_s` (their names
  differ from the new ones), so for up to 90 s the worker count is too high. With `vod.max_concurrent`
  above `workers - 1` a video started in that window can take the last worker. Keep `vod.max_concurrent`
  at or below `workers - 1` to rule that out.
- The zombie row on staging needs no manual repair: the first recovery pass after deploy resets it and
  queues a job for it (its old job ended as `done`).

## Known limits

- A run by hand (`sixstars vod process`) has no job. If it goes silent for longer than
  `vod.orphan_grace_s` (no `touch`), recovery puts the row back to `new` and queues a job; and a queued job
  for the same video takes the video over while the CLI is still on it. The steps are idempotent, but the
  work can be done twice. Do not run the CLI on a video that has a queued job.
- Two jobs for the same orphaned video that start at the same moment both see "another job is running" and
  both end as `ignored`; the next recovery pass queues a fresh job.
- The pre-claim gates (paused, emergency stop, downloads blocked) still put a video back to `new` even when
  another worker is processing it. Not changed here.
