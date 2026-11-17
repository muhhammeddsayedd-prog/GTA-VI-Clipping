## M10 (part 1) — Posting rules: pure decision logic

**Date:** 2026-11-17 UTC · **Branch:** `m10-posting-rules` (from `m02-api-clients`) · **Not pushed, not merged.**

This is only the pure half of M10: no database, no network, no clock. The publisher, the DB queries, the job
handlers, the CLI and the API routes are **not** built yet (see "What the next agent builds").

### Local results

| Command | Result |
|---|---|
| `uv run ruff check .` | passed |
| `uv run ruff format --check .` | passed (161 files) |
| `uv run mypy --strict src/` | passed (111 source files) |
| `uv run pytest tests/unit` | 1564 passed, 6 skipped (the 6 are the M03 network tests that need `RUN_LIVE_TESTS=1`) |
| `uv run pytest tests/unit/posting` | 846 passed, about 11 s |

Mutation check (done by hand, not part of CI): 35 single-line changes to the rules (every comparison at a
boundary, each gate check removed, the platform ceiling removed, the staging rule removed, and so on) were
applied one at a time; the posting tests failed for all 35.

### Modules (`src/sixstars/posting/`)

| File | Content |
|---|---|
| `models.py` | `Lane`, `Platform`, `Action`, `Reason`, `Account`, `PostRecord`, `Clip`, `Decision`, `ensure_utc` |
| `params.py` | `PostingParams`, `PostingParams.from_settings(values, app_env)`, `REQUIRED_KEYS`, `OPTIONAL_KEYS` |
| `gate.py` | `posting_mode`, `is_dry`, `check_gate`, `privacy_problem`, `staging_problem`, `duplicate_channels` |
| `caps.py` | `cap_state` → `CapState`, `effective_cap`, `effective_gap_s`, `reserve_left`, `counts`, `on_rate_limit` → `RateLimitOutcome` |
| `precedence.py` | `decide_spike`, `decide_hourly_account`, `has_pending_spike`, `plan_spikes` → `SpikePlan`, `clip_block`, `planned_post`, `demoted_rank` |
| `slots.py` | `slot_start`, `next_slot`, `check_slot`, `slot_is_open`, `jitter_offset_s`, `jitter_window`, `fire_at` |
| `selection.py` | `hourly_candidates`, `pick_clip`, `last_hourly_creator`, `plan_hourly_slot` → `SlotPlan`, `empty_slots_alert` |
| `captions.py` | `build_caption`, `youtube_title`, `hashtags_for`, `build_fields`, `build_upload_request` → `UploadRequest`, `idempotency_key`, `UnsafePostError` |
| `ranking.py` | `freshness`, `rank_score`, `is_expired`, `half_life_h` |

`publisher.py`, `hourly.py`, `spike.py` are still the M00 stubs: they are for the next agent.

### Public API

```python
# params
PostingParams.from_settings(values: Mapping[str, object], app_env: str) -> PostingParams   # app_env: "staging" | "production"

# inputs (frozen dataclasses; every datetime must be timezone-aware, naive raises ValueError)
Account(id, platform: Platform, upload_post_user, handle, lanes: frozenset[Lane], cap_24h, spike_reserve, min_gap_s,
        privacy=None, hourly_hours_utc: frozenset[int] | None = None, enabled=True, paused_until=None)
PostRecord(account_id, platform, clip_id, lane, status, created_at, published_at=None, dry_run=False,
           creator_id=None, slot_at=None)          # .at = published_at or created_at
Clip(id, creator_id, lane, rank_score, status="approved", approved_at=None, spike_at=None, creator_handle="",
     source_platform="youtube", title=None, caption=None, hook=None, hashtags=())

# output
Decision(action: Action, code: Reason, reason: str, account_id, lane, dry_run=False, clip_id=None,
         defer_until=None, slot_at=None)            # .may_send (ALLOW only), .would_post (ALLOW or DRY_RUN)
Action: ALLOW | DRY_RUN | SKIP | DEFER

# gate
posting_mode(now, params) -> (Mode.STOPPED | Mode.DRY | Mode.LIVE, Reason)
check_gate(account, lane, now, params) -> Decision
duplicate_channels(accounts) -> frozenset[str]

# caps
cap_state(account, history, now, params, *, dry=False) -> CapState
    # CapState: cap, used_24h, free, spikes_24h, spikes_1h, reserve_left, spike_rate_ok, last_post_at, gap_s,
    #           gap_ends_at, gap_ok, .at_cap, .hourly_allowed, .spike_allowed
on_rate_limit(account, history, now, params) -> RateLimitOutcome(account_id, paused_until, new_cap_24h, cap_changed, reason)

# lanes
decide_spike(clip, account, history, now, params) -> Decision
decide_hourly_account(account, history, slot_at, now, params, *, spike_pending=False) -> Decision
has_pending_spike(account, spike_clips, history, now, params) -> bool
plan_spikes(clips, accounts, history, now, params) -> tuple[SpikePlan, ...]
    # SpikePlan(clip_id, decisions, demote_to_hourly, demoted_rank_score)
plan_hourly_slot(accounts, history, queue, slot_at, now, params, *, spike_clips=(), fallback=()) -> SlotPlan
    # SlotPlan(slot_at, decisions, slot_empty, fallback_used), .posts
empty_slots_alert(recent_slots_empty: Sequence[bool]) -> bool

# slot time
slot_start(now), next_slot(now), fire_at(slot_at, jitter_max_s, seed=""), jitter_offset_s(...), jitter_window(...)

# fields
build_upload_request(clip, account, params, *, preview=False) -> UploadRequest(clip_id, account_id, user, platform,
                                                                              idempotency_key, request_id, fields)
build_fields(clip, account, params) -> dict[str, str | bool]
rank_score(score, source_type, tier, preferred_creator, reference_at, now, params) -> float
is_expired(approved_at, now, params) -> bool
```

`history` is always the recent `posts` rows of **all** accounts: at least the last 24 h, plus the latest
hourly post of each account (R9), plus every row of the clips being decided (so "already posted" is seen).

### Reason codes

| Code | Action | Meaning |
|---|---|---|
| `ok` | ALLOW | every rule passed, live mode |
| `dry_run_setting` / `before_launch` | DRY_RUN | every rule passed, but `launch.dry_run` is true / `now < launch.launch_at` |
| `emergency_stop` | SKIP | `control.emergency_stop` is true |
| `account_disabled`, `lane_not_allowed`, `account_paused` | SKIP | account state |
| `privacy_invalid` | SKIP | TikTok/YouTube account without a privacy value the platform knows |
| `staging_privacy_not_private`, `staging_instagram_not_allowed` | SKIP | staging safety rule (real posts only) |
| `duplicate_channel` | SKIP | two enabled rows share platform + Upload-Post profile (planners only) |
| `clip_not_approved`, `clip_wrong_lane` | SKIP | spike lane was given a clip it must not post |
| `already_posted` | SKIP | the clip already has a post on this account |
| `posted_on_same_platform` | SKIP | the clip is on another account of the same platform (D42) |
| `spike_time_missing`, `spike_too_old` | SKIP | R7 |
| `at_cap` | SKIP | R8, rolling 24 h cap |
| `spike_hourly_limit` | SKIP | R6 |
| `min_gap_wait` | DEFER | R4, spike waits until `defer_until` |
| `spike_wait_exceeded` | SKIP | R4, the gap ends after `posting.spike_max_wait_s` |
| `slot_not_due` | DEFER | hourly slot asked for before its start |
| `slot_expired` | SKIP | hourly slot asked for after its own hour |
| `outside_hourly_hours` | SKIP | `hourly_hours_utc` |
| `slot_already_filled` | SKIP | the account already has an hourly post for this slot |
| `spike_pending` | SKIP | R2 |
| `spike_recent` | SKIP | R3 |
| `reserved_for_spike` | SKIP | hourly would eat the spike reserve |
| `min_gap` | SKIP | hourly never waits for the gap |
| `queue_empty`, `no_eligible_clip` | SKIP | R10 / R9 |

### Finding: the seeded YouTube cap must drop

Upload-Post allows 30 YouTube posts per 24 h per account; the seeds say `cap_24h = 40, spike_reserve = 8`.
The code already clamps every cap to `posting.platform_max_24h`, so with the proposed setting in place the
system cannot pass 30 even with the seed unchanged. But then the hourly lane gets only 30 − 8 = 22 posts a
day (two slots a day are skipped with `reserved_for_spike`; tested).

Proposed (both projects), keeping a margin of 2 under the platform limit like TikTok (14 of 15):

```sql
update platform_accounts set cap_24h = 28, spike_reserve = 4 where id = 'youtube_main';
```

28 − 4 = 24, so every hourly slot can post and 4 posts a day are kept for spikes (tested:
`test_a_day_of_hourly_slots_on_production_accounts`). The margin of 2 covers posts made outside this system
(the M15 test post, a manual upload) and a platform window that is not aligned with ours. `on_rate_limit`
still lowers the cap by 2 (floor 10) if YouTube reports a limit earlier.

### Proposed settings (SQL for a new migration, not applied, seeds not edited)

```sql
insert into settings (key, value, description) values
  ('posting.platform_max_24h', '{"tiktok": 15, "instagram": 50, "youtube": 30}'::jsonb,
   'Hard ceiling of posts per account per rolling 24 h set by Upload-Post. An account cap above it is ignored.'),
  ('posting.staging_allow_instagram', 'false'::jsonb,
   'Staging only: allow a real Instagram post (Instagram cannot post privately). Set true only for the single M15 test post.')
on conflict (key) do nothing;
```

- `posting.platform_max_24h` is **required**: `PostingParams.from_settings` raises `KeyError` without it, and
  `ValueError` when a platform is missing. Until the row exists nothing can be decided, so nothing posts.
- `posting.staging_allow_instagram` is optional; absent means false.

Optional, only if the owner wants the 18:00 UTC reserve release of docs/11 §3 (absent = never released):

```sql
insert into settings (key, value, description) values
  ('posting.reserve_release_hour_utc', '18'::jsonb, 'From this UTC hour until midnight, unused spike reserve beyond posting.reserve_release_keep is released to the hourly lane.'),
  ('posting.reserve_release_keep', '4'::jsonb, 'Spike reserve kept per account after the release hour.')
on conflict (key) do nothing;
```

All four keys also need a row in `docs/16-settings-reference.md` (not edited here: outside this task's files).

### Ambiguities and contradictions in the docs, and the reading chosen (always the one that posts less)

1. **Which posts count toward cap, gap and "already posted".** docs/11 says "published posts (not dry)".
   Chosen: every non-dry row that is not `skipped`: `pending`, `published`, `failed`, `removed`,
   `remove_failed`. A pending post is on its way, and a failed or removed one may have used platform quota.
   Time of a post = `published_at`, else `created_at`.
2. **Dry-run rows.** They never count against real posts (otherwise the dry posts before launch would block
   launch day). While the system is in dry mode they do count, so a dry run behaves like the real thing.
3. **Window edges.** A post exactly 24 h old (or a spike exactly 1 h old) still counts. The gap is met at
   exactly `min_gap_s` (docs: `≥`). R3 uses `<` as written: a spike exactly 1800 s before the slot does not
   skip it. R7 "older than": exactly 1800 s is still a spike. Clip expiry: exactly 72 h is still usable.
4. **R3 reference time.** "ago" could be measured from the slot or from the jittered posting time. Chosen:
   from the slot start, and a spike posted after the slot start (during the jitter) also skips the slot.
5. **Two gap values** (`platform_accounts.min_gap_s` and `posting.min_gap_s`): the larger one applies.
6. **Cap above the platform limit** (YouTube 40 vs 30): the lower of the two applies (new setting above).
7. **18:00 UTC reserve release.** The numbers 18 and 4 are in docs/11 but have no key in docs/16. Not
   hard-coded: implemented behind two optional settings, off when they are absent.
8. **R4 wait start.** "up to `spike_max_wait_s`" has no stated start. Chosen: from `clips.approved_at` (when
   the spike became ready), not from the first attempt. Consequence: with gap 900 s and max wait 900 s, a
   second spike approved even one second before the first one posted cannot wait the full gap and is demoted
   to the hourly lane (R5 "second waits for the gap" only holds when it can finish inside the wait). Also a
   spike does not wait for a gap that ends after it would be too old (R7).
9. **Hourly lane and the gap.** docs/11 lists R3/R8/R11 for the hourly lane but the gap must hold on every
   account. Chosen: the hourly lane never waits; a slot that fails the gap is skipped (`min_gap`).
10. **Paused accounts and spikes.** A paused account is skipped, not waited for. `paused_until` equal to
    `now` is still paused (docs: `< now`).
11. **Emergency stop vs dry run.** Under emergency stop nothing is decided at all: no dry-run rows, no
    demotion of spikes.
12. **Staging rule and dry runs.** As specified, the staging privacy rule applies to real posts only; a
    staging dry run still records what it would post to Instagram. `privacy_invalid` applies in both modes.
13. **D18 (same clip everywhere) vs R9 (next clip for an account) vs D42.** Each account takes the
    best-ranked clip it may have. All accounts read the same ranked queue, so they get the same clip unless
    R9, "already posted there" or D42 forces a different one for that account. If only same-creator clips
    are left, that account skips the slot.
14. **D42 generalised.** "Different clips on each TikTok account" is implemented as data, not a TikTok
    special case: a clip never goes to two accounts of one platform.
15. **Two rows for one real account.** In both seeds `tiktok_main` and `tiktok_live` have the same profile
    and handle. If both were ever enabled, their caps would add up on one real TikTok account (28 > 15). The
    planners refuse both rows with `duplicate_channel` until one is disabled or moved to another profile.
    `decide_spike` / `decide_hourly_account` alone do not see other accounts: use the planners.
16. **Rate limit on YouTube.** docs: pause until the oldest post in the window is 24 h old. Chosen: that, but
    never less than the 1 h used for the other platforms (otherwise a pause of a few minutes would be
    followed by another error and another cap cut). The cap is clamped to the platform ceiling first, then
    lowered by 2, floor 10, never raised. The numbers 1 h, 2 and 10 are docs/11 constants without settings
    keys; they are named constants in `caps.py` (`RATE_LIMIT_PAUSE`, `LEARNED_CAP_STEP`, `LEARNED_CAP_FLOOR`).
17. **Other doc numbers without settings keys** kept as named constants: spike demotion boost 1.5, rank
    bonuses 1.3 / 1.15 / 1.1, max 5 clip hashtags, YouTube title 100, 2 empty slots before an alert.
18. **One hourly post per slot per account** is enforced from `posts.slot_at` (`slot_already_filled`), and a
    slot is only served inside its own hour (`slot_not_due`, `slot_expired`). Not in the docs; added so a
    double trigger (n8n plus the scheduler failover) or a late worker cannot double-post.
19. **Jitter.** docs say "random 0..jitter_max_s". `jitter_offset_s` derives the offset from the slot time
    (SHA-256), so both triggers of one slot agree on the time. Pass a secret `seed` if predictability matters.
20. **`title` for YouTube.** docs/11 §8 sends the caption as `title` and the real title as `youtube_title`.
    For the YouTube call `title` is set to the YouTube title too (≤ 100 characters), so a long caption can
    never become the video title if `youtube_title` were ignored. `selfDeclaredMadeForKids=false` is sent
    (listed in docs/14). `<` and `>` are removed from YouTube title and description.
21. **Compilations (R10)** do not exist in the schema or in any earlier milestone. `plan_hourly_slot` takes
    them as an optional `fallback` list; with nothing passed, an empty queue just skips the slot.
22. **Accounts table in the kit.** The kit still shows the old accounts table (two TikTok accounts, profile `6stars-main`);
    the rules read only the `platform_accounts` rows, so nothing in the code depends on it.

### What the next agent builds on top

1. **Migration `0004`** with the settings above and the `youtube_main` update; add the keys to docs/16.
2. **Loaders** (`db.py` SQL): `platform_accounts` → `Account`; `posts` (joined to `clips.creator_id`) →
   `PostRecord`; `clips` (+ creator handle, `spikes.spike_wall` as `spike_at`) → `Clip`; settings →
   `PostingParams.from_settings(values, env.app_env)`. Pass `now` from one clock read per decision.
3. **`posting/hourly.py`**: heartbeat, wait until `fire_at(slot, params.jitter_max_s)`, then **inside one
   transaction with a lock** (advisory lock per slot, or `select ... for update` on the accounts) load the
   history, call `plan_hourly_slot`, insert the `posts` rows (`pending`, or `skipped` + `dry_run` for
   `DRY_RUN`), commit, then publish. Without the lock two workers can both be allowed the last free post.
   Events: `posting.slot_skipped` per skipped account (use `decision.code` / `decision.reason`),
   `posting.slot_empty` when `plan.slot_empty`, alert when `empty_slots_alert(...)`.
4. **`posting/spike.py`**: `plan_spikes` for the ready spike clips; `ALLOW` → publish; `DEFER` → job
   `post.retry` with `run_after = decision.defer_until`; `demote_to_hourly` → `lane = 'hourly'`,
   `rank_score = plan.demoted_rank_score`; `spikes.spike_to_post_s` on the first publish.
5. **`posting/publisher.py`**: only for `decision.may_send`. Re-check `check_gate` right before the call
   (settings may have changed during the jitter), build with `build_upload_request(clip, account, params)`
   (raises `UnsafePostError` for a staging post that could be public), call `upload_video`, poll, store the
   result. On `RateLimited` or a per-platform limit error: `on_rate_limit(...)` → update `paused_until`,
   `cap_24h`, event + warn alert. Handle `fallback_to_inbox` and `post_url = null` (M02 notes).
6. **Dry-run rows and the unique `idempotency_key`.** A dry-run row written as `post:<clip>:<account>` would
   block the real row for the same clip and account later. Give dry-run rows a different key (for example
   `dry:<clip>:<account>:<slot or time>`) or delete them at launch. The rules already ignore dry rows in
   live mode.
7. **The Upload-Post client guard** checks `emergency_stop` and `launch.dry_run` only, not `launch_at`.
   Before launch with `dry_run = false`, only `Decision.may_send` stops a post: never send on `would_post`.
8. **Clip status.** Mark a clip `posted` per docs/11 §6.4; decide what happens to a clip that some account
   could not take (it stays in the queue here and the rules keep it off the accounts that already have it).
9. `ranking.rank_score` / `is_expired` are ready for the M13 scheduler.
10. CLI `sixstars post hourly [--dry-run]`, `sixstars post clip`, job handler `post.retry`, API route.

### Not verified

- Nothing was sent to Upload-Post and no database was touched: this is pure logic with unit tests only.
- The Upload-Post field names come from docs/14 and the M02 notes (not re-read online in this task); the
  first real upload is still to come. `first_comment` on all three platforms and `selfDeclaredMadeForKids`
  are documented there but untested live.
- Caption limits (2200 characters for TikTok/Instagram, 5000 for a YouTube description) are from memory, not
  from the platform docs.
- Whether Upload-Post counts failed uploads toward its 24 h limits is unknown; they are counted here.
- The limits 15 / 50 / 30 are taken from the M02 notes as given in the task.
