## M15 (part) — First real upload through Upload-Post: two private staging posts

**Date:** 2026-11-17 UTC · **Branch:** `m15-staging-post` (from `develop`) · **Not pushed, not merged.**

The first real uploads of the project: the same clip, once to `youtube_main` as `private` and once to
`tiktok_main` as `SELF_ONLY`, from this machine against the staging database (`tyhejpxcfgyvhwkrrojq`),
through the real publisher (`sixstars posting clip`). Nothing went to Instagram. The production project was
not touched. Exactly two upload requests were sent (one per platform, `api_usage` has two `upload_video`
rows) and one delete request.

### Result

| | YouTube (`youtube_main`, `@6starsclips-m3x`) | TikTok (`tiktok_main`, `@6starsclipping`) |
|---|---|---|
| Request id / idempotency key | `post:ff1437e2-ec0a-455a-b652-50e6c4ad8910:youtube_main` | `post:ff1437e2-ec0a-455a-b652-50e6c4ad8910:tiktok_main` |
| `posts` row | `a805a252-85a4-4590-9867-2220095fb823` | `8720a296-74ae-4fa7-842c-ae2899a827aa` |
| Privacy field sent | `privacyStatus=private` | `privacy_level=SELF_ONLY` |
| Outcome | published, video id `kwtCUdnP_Zk`, then **deleted** by the removal path (row is `removed`) | published; **no post id and no URL** (TikTok gives none for a post that is not public); row is `published` |
| Link | was `https://www.youtube.com/watch?v=kwtCUdnP_Zk` (now "Video unavailable") | none; Upload-Post `original_publish_id` `v_pub_url~v2.7709065962657630260` |
| Time from request to final status | about 80 s | about 4 min (75 s sending the file, then `processing`) |
| Evidence it is not public | public Data API `videos.list?part=status&id=` → `items: []`; watch page and Shorts page without sign-in → `LOGIN_REQUIRED`, "Private video"; oEmbed 403; thumbnail 404 | indirect only, see below |

**TikTok privacy was not confirmed by any API answer.** The Upload-Post status, history, analytics and
media-list answers do not carry the privacy TikTok applied. What is known: the request carried
`privacy_level=SELF_ONLY` (checked on the wire); `GET /api/uploadposts/tiktok/settings?profile=default`
lists `SELF_ONLY` for the account (`PUBLIC_TO_EVERYONE`, `MUTUAL_FOLLOW_FRIENDS`, `SELF_ONLY`); the
connection has the `video_privacy` capability (the docs: connections without it publish videos as public);
no `fallback_to_inbox`; the TikTok media list of the account is empty and the profile `video_count` is 0
after the post; TikTok released no public post id (the docs: a post that is not public never gets one). The
public profile page could not be read from here (TikTok serves a script challenge to curl). **The owner
should open the TikTok app once and confirm the video shows "Only me".**

### Fields sent (read from the HTTP call itself, without the key and the file bytes)

Header names: `Authorization`, `Idempotency-Key` (= the request id). File field `video` = `ketchup.mp4`
(`video/mp4`). Both match the current Upload-Post docs (`api/upload-video.md`: `privacyStatus` with
`"private"`, `privacy_level` with `SELF_ONLY`).

```
YouTube: async_upload=true, title="Ketchup caused the crash? Sure 😭", youtube_title=<same>,
         youtube_description="Ketchup caused the crash? Sure 😭\n🎥 @staging-posttest on YouTube\n#GTA6 #GTAVI #GTA6Clips #Gaming #GTA5 #GTARP #Shorts",
         categoryId=20, privacyStatus=private, selfDeclaredMadeForKids=false,
         first_comment="Follow for more (staging test)", user=default, platform[]=youtube, request_id=<key>
TikTok:  async_upload=true, title="Ketchup caused the crash? Sure 😭\n🎥 @staging-posttest on YouTube\n#GTA6 #GTAVI #GTA6Clips #Gaming #GTA5 #GTARP",
         privacy_level=SELF_ONLY, first_comment="Follow for more (staging test)", user=default,
         platform[]=tiktok, request_id=<key>
```

### `launch.dry_run`

Set through `sixstars.settings.set` inside `try/finally`, one short window per platform (times of this
machine; the database clock was about 4 s ahead):

| Window | false from | true again at |
|---|---|---|
| YouTube | 09:11:05.76 | 09:12:27.57 |
| TikTok | 09:13:15.58 | 09:14:31.85 |

Read back after each window and again at 09:41: `launch.dry_run = true` (`settings.updated_at`
09:14:35 database time), `control.emergency_stop = false` (never changed), `launch.launch_at` and every
`platform_accounts` row unchanged. Both windows were inside minutes 10 to 50; the staging server had no
approved clip, no posting job and no pending post at the time (checked before each window).

### How it was run

1. Clip: `C:\Users\iway\6stars-hardening-out\renders\ketchup.mp4` (ffprobe: h264 1080×1920 30 fps, AAC
   stereo, 48.7 s, 23.9 MB; hook "Ketchup caused the crash? Sure 😭", no real-world name).
2. `sixstars gate check-file <file> --duration 47.17 --p5`: no black or frozen part, P5 flagged nothing
   ($0.0008). **The clip row did not go through `run_gate`**: `clip render-file` stores no transcript, music
   flags or score parts, so a row in `gating` could only have been judged on invented facts. The rows were
   inserted by hand instead (below).
3. Before each upload a script checked: `APP_ENV=staging` and the staging database; the minute; the
   `platform_accounts` row (`privacy` exactly `private` / `SELF_ONLY`, `upload_post_user = 'default'`,
   enabled, not paused); emergency stop off; `posting.staging_allow_instagram` false; no other clip in
   flight; no posting job; no row for this clip and account yet. Then: clip → `approved`, plan in dry mode,
   `launch.dry_run = false`, `posting plan` in live mode (must say `allow ok` for this clip and account and
   `staging_instagram_not_allowed` for Instagram), `commands.clip(clip_id, account)` (what
   `sixstars posting clip` runs), `launch.dry_run = true`, clip out of the queue. The script also refused a
   second HTTP upload request in the same run (none was attempted).
4. `posting plan` (live): `youtube_main allow ok`, `tiktok_main allow ok`, `instagram_main skip
   staging_instagram_not_allowed`, `tiktok_live skip account_disabled`. In the second window YouTube showed
   `slot_already_filled`.
5. The clip became `posted` after the YouTube post, so it was set back to `approved` by hand for the TikTok
   window (`posting clip` needs an approved clip; a clip is `posted` as soon as one account has it and
   nothing is pending).

### Rows created in the staging database (all identifiable by `staging-posttest`)

- `creators` `91e90396-ad47-4ea8-9db9-01e61211e254`: platform `youtube`, `channel_id` and `handle`
  `staging-posttest`, display name "M15 staging post test (not a real channel)". Tier was `auto` only
  during the two windows; it is now **`restricted`** (discovery never clips it and the RSS source skips it;
  not `blocked`, because the guardian removes the posts of blocked creators).
- `clips` `ff1437e2-ec0a-455a-b652-50e6c4ad8910`: `source_id` and `topic` `m15-staging-posttest`,
  `source_url` `https://example.invalid/m15-staging-posttest`, lane `hourly`, score 5, `local_path` the file
  above, `gate_result` a note saying the row was inserted by hand. Status now **`posted`** (not in the
  hourly queue).
- `posts`: the two rows above (YouTube `removed`, TikTok `published`), left as evidence and report data.
- Written by the code as usual: events (`post.planned`, `post.published`, `post.status_pending`,
  `clip.posted`, `stats.collected`, `post.removed`), `post.retry` jobs (all done), three `account_stats`
  snapshots, `api_usage` rows, `creators.avg_clip_views = 0` for the test creator.

### What Upload-Post did differently from what the code expected

1. **YouTube private: `post_url` is a sentence, not a link** (`"Post uploaded as Private. No public URL
   available."`). The client already drops a value that does not start with `http`, so the row had no link
   and the publisher asked for one every 5 minutes for 20 minutes. **Fixed** (below).
2. **TikTok `SELF_ONLY`: `platform_post_id` and `post_url` are both null**, not the "temporary publish id"
   the docs describe; the publish id is in `original_publish_id` and is not usable as a post id. The row is
   `published` without id and link, as designed. For a post that is not public this never changes.
3. **The first comment was not posted on either platform**: YouTube refuses comments on a private video,
   and TikTok skips it when the post id is not known at publish time. The second case will also happen in
   production whenever TikTok is slow to release the id. Both are only `warnings` in the result; the
   publisher does not store or log them.
4. **The status answer has two shapes** (running and finished); both parse correctly. While running, the
   per-platform entry has `success: false` without an error; the publisher never reads it (the top-level
   status is not final). The finished answer contains the account's email address (`user_email`): raw
   answers must not be logged.
5. **Time.** The upload call returned after 75 to 80 s (24 MB from this machine). The publisher used the
   clock value read before the upload as `published_at` (YouTube row: 09:11:08, real time about 09:12:29)
   and as the base of the first status retry (so that retry was already due). **Fixed** (below). The
   YouTube row in staging keeps the early time.
6. **TikTok needs more than the 30 s inline wait** (about 3 minutes of `processing`), so the post went
   through `post.retry` jobs every 30 s. The staging server's worker ran those jobs too (it reads the
   status; it cannot send, the file is not on the server) and the row was resolved by the local run when
   the status turned `completed`. This worked as designed. Each resolver starts its own link-refresh chain,
   so up to three `post.retry` jobs per post were queued at the same time until 20 minutes after
   publishing: harmless, a little wasteful, not changed.
7. `clips.status = 'posted'` after the first account blocks `posting clip` for a second account (point 5
   of "How it was run"). In the hourly lane all accounts are planned together, so this only affects the
   staging command.

### Read paths on the real posts

| Call | Result |
|---|---|
| `get_post_analytics(request_id)` and `(request_id, platform)` | Same shape. YouTube private: `post_metrics` with ten keys, all 0 → `PostNumbers(0, 0, 0, 0)`, observation `ok`. TikTok: `post_metrics_error` "TikTok post ID could not be resolved yet…" → empty numbers, observation `unknown` (not a finding). |
| `get_post_analytics_by_platform_id("default", "youtube", "kwtCUdnP_Zk")` | Metrics under `platforms.youtube.post_metrics` (the M02 fake puts them at the top level; both are parsed). TikTok: not possible, there is no post id. |
| Guardian `observe()` with `check_private_posts` true in memory only | YouTube `ok`, TikTok `unknown`; `judge` → no finding, no suspect. |
| Guardian real pass `service.run_check()` (setting as it is: false) | `{"checked": 0, "unverifiable": 2, "problems": [], "strikes": 0, "emergency_stop": false}`. |
| Stats `service.collect_stats()` | `{"accounts": 3, "posts": 1, "skipped": 1, "errors": []}`: YouTube numbers saved through Upload-Post (a private video is not in the public Data API), TikTok skipped (no metrics), three account snapshots. |
| After the delete (7 minutes later) | Both analytics forms: `post_metrics_error: "YouTube video not found (ID: kwtCUdnP_Zk)."` → observation `gone` / `removed_by_platform`. In the first minutes after the delete the cached answer still had `post_metrics` (`ok`). |

Key shapes are recorded in `docs/14-api-integrations.md` §1 (table "Verified by the first real uploads").
No parser bug was found: every real answer was handled by the existing parsers.

### Removal

`guardian.removal.remove_post` (no YouTube OAuth token → `UploadPostClient.delete_post` →
`POST /api/uploadposts/posts/unpublish` with `{"platform": "youtube", "user": "default", "post_id":
"kwtCUdnP_Zk"}`) → HTTP 200 `{"success": true, "message": "YouTube video deleted."}` at 09:34:15. **The
unpublish endpoint works on a private video.** The row is `removed` (`post.removed` event), the watch page
says "Video unavailable". TikTok has no delete: the post stays.

### Fixes (each with unit tests)

| File | Change |
|---|---|
| `posting/publisher.py` | `post_link()`: a YouTube result with a video id and no link is stored with `https://www.youtube.com/watch?v=<id>` and no link-refresh job is queued. Everything after the upload (`published_at`, the spike-to-post time, the next status read, the link refresh) is timed from the clock read when the upload call returns, not from the value read before it. |
| `stats/collector.py` | `parse_post_link()` + `save_link()`: when a post-analytics answer carries a post id or a real link that the row does not have yet, the stats pass stores it (never replacing a value). TikTok releases the id minutes to hours after publishing and the publisher only asks for 20 minutes; without the link a takedown email that quotes the TikTok link cannot be matched to the post. **Not seen live** (a `SELF_ONLY` post never gets an id); the shape is the documented one. |
| Tests | `test_posting_publisher.py` (private YouTube link, `post_link` table, `published_at` and the next status read after a slow upload), `test_upload_post_client.py` (the three real status answers), `test_stats_parsing.py` (the two real analytics answers, `parse_post_link`), `test_guardian_claims_rules.py` (the real answers are never a finding). |

Neither fix was needed for the uploads to work, and no upload was repeated.

### Local results

`uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy --strict src/` (161 files) and
`uv run pytest tests/unit` (3053 passed, 6 skipped) pass. Integration tests were not run (`save_link` is new SQL in `stats/collector.py`; the stats
pass with the new code ran once against the staging database without error, but with nothing to fill in).

### For the owner

- **TikTok `@6starsclipping`: one `SELF_ONLY` test video ("Ketchup caused the crash? Sure 😭", posted
  2026-11-17 09:17 UTC).** Confirm it shows "Only me", then delete it in the TikTok app (profile → the
  private tab (padlock) → the video → three dots → Delete). There is no API for it.
- YouTube `@6starsclips-m3x`: nothing to do, the private test video `kwtCUdnP_Zk` was deleted.
- Instagram: nothing was posted.

### Not verified, open

- The privacy TikTok applied (see above).
- A public TikTok post: when its id and link arrive, and whether `post_id_resolved_late` appears as
  documented. A public YouTube post: the real `post_url` form.
- `fallback_to_inbox`, a rejected platform result, a 409 for a key in use, a rate limit: none occurred.
- The Upload-Post monthly allowance: the async answers carried no `usage` object.
- `guardian.check_private_posts` stays false in staging. With it true, the guardian would read private
  posts through Upload-Post: the YouTube answer supports that (`ok`, later "not found"), the TikTok answer
  stays `unknown`.
- The M02 fake for `get_post_analytics_by_platform_id` puts `post_metrics` at the top level; the real
  answer has it under `platforms.<platform>` (`src/sixstars/clients/fakes.py` was outside this task).
