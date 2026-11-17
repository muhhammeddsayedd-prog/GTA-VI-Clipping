## Hardening — source access (discovery, VOD lane, chat)

**Date:** 2026-11-17 UTC · **Branch:** `hardening-sources` (from `develop` at `1db9836`) · not pushed, not merged

Reason: the first real passes on the staging server (Hetzner, data-centre address, no proxy, no cookies, no Twitch keys) had the yt-dlp search source in error on every pass (`Unable to download API page: ('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))`), video downloads refused with "Sign in to confirm you're not a bot", and only Apify (one run per 6 hours) feeding discovery.

Nothing here tries to get past YouTube's checks. The only ways around the block are the owner's `PROXY_URL` and `YOUTUBE_COOKIES_FILE`, the official Data API, channel feeds, Apify and Twitch.

### What was built

| # | Item | Where |
|---|---|---|
| 1 | Data API `search.list` as a discovery source, fallback by default, with a call budget | `clients/youtube.py` (`search_list`), `discovery/sources.py` (`search_api_step`), `discovery/store.py` |
| 1b | Streams seen live are looked up again every pass with `videos.list` (1 unit per 50) | `sources.known_live_stubs`, `store.recent_live_sources` |
| 2 | RSS: rotation checked, 50 feeds per pass instead of 25 | migration 0010, tests |
| 3 | Source health: one warning after N failed passes, one message on recovery, yt-dlp back-off and probe | `discovery/health.py`, `discovery/run.py` |
| 4 | Language: script and term signals overrule a reported `en` | `discovery/scoring.py` (`language_signal`, `detect_language`), `sources.language_of` |
| 5 | Yield: bonus for edited uploads with engagement, factor for long streams with thin chat; two settings changed | `scoring.yield_adjustment`, migration 0010 |
| 6 | A cookies path without a file means "no cookies", everywhere | `chat/net.py` (`cookies_file`, `usable_cookies`) |
| 7 | `sixstars discovery doctor`; a proxy or cookies added later end the back-offs at once | `discovery/doctor.py`, `health.gate`, `vod/store.block_retry_at` |

Migration `supabase/migrations/0010_sources.sql` (applied to the test databases only; **the orchestrator pushes it to both Supabase projects**). Matching rows are in `docs/16-settings-reference.md`. Until it is applied the code runs on `discovery.config.SOURCES_DEFAULTS`, which holds the same values, except that the three *changed* values below keep their old value in the table.

### 1. `search.list`: what the documentation says today, and the budget

**The brief and the documentation disagree; the documentation was followed (CLAUDE.md rule 4).** The brief says a `search.list` call costs 100 units of the 10,000 daily units. Since the revision of 2026-06-01 it does not:

- https://developers.google.com/youtube/v3/determine_quota_cost : "Projects that enable the YouTube Data API have a default quota allocation of 100 `search.list` calls, 100 `videos.insert` calls, and 10,000 units per day combined for all other endpoints." and "The `search.list` and `videos.insert` methods have their own quota buckets." Table row: search / list / "100 quota per day. Each call costs 1 quota."
- https://developers.google.com/youtube/v3/docs/search/list : "A call to this method has a quota cost of 1 unit in the Search Queries quota bucket."
- https://developers.google.com/youtube/v3/revision_history (June 1, 2026): the API moves to a granular quota system, starting with `videos.insert` and `search.list`.

So the budget is counted in **calls**, and `search.list` no longer draws on the units of `videos.list` / `channels.list` at all. **Not verified:** which accounting the owner's Google Cloud project is on (the console is the only place that shows it; the API does not report usage). The defaults are therefore safe under both readings.

Parameters used (all from the method's reference page): `part=snippet`, `type=video` (required by the three filters below), `q` (the `|` OR operator is documented; httpx sends it URL-escaped as `%7C`), `eventType=live` for the live mode, `order`, `publishedAfter` (RFC 3339) for the recent mode, `videoCategoryId=20`, `relevanceLanguage=en`, `videoDuration=long` for the recent mode, `maxResults=50`. One page per call, never `pageToken`.

**Design**

- `discovery.search_api_mode` = `fallback`: a call is made only in a pass where the yt-dlp search failed, found nothing or is backing off. `always` and `off` exist.
- At most `discovery.search_api_calls_per_pass` (1) per pass, one call per `discovery.search_api_interval_s` (1800 s), at most `discovery.search_api_daily_calls` (60) per UTC day, counted from the `api_usage` rows (`youtube` / `search.list`, 1 unit each; a refused call is recorded too).
- The calls alternate `live`, `recent`, `live`, ... (`discovery.search_api_modes`). **Recent** uses one OR query over all search terms. **Live** takes one term per call, in turn. This split comes from measurements on the real API (below), not from the brief's example.
- When Google answers 403/429 with a quota message, the source pauses until 08:10 UTC (after midnight Pacific in summer and winter time) instead of calling again every half hour.
- Budget spent: the source reports `skipped` with "daily budget spent (60 of 60 calls)", the summary carries one note on the first pass that finds it spent (`notes`), nothing is notified, and the pass goes on with feeds, Apify, the second look at known streams and (when it works) yt-dlp.
- `videos.list` enrichment is unchanged. Its 3,000-unit guard (`discovery.youtube_quota_daily_units`) no longer counts `search.list` rows (`store.youtube_units_today`), otherwise 30 searches would have switched the enrichment off.

**Arithmetic at the 10-minute cadence (144 passes a day), yt-dlp dead**

| | Per pass | Per hour | Per day |
|---|---|---|---|
| `search.list` calls | 1 in every third pass, else 0 | 2 (one live, one recent) | 48, cap 60, Google allows 100 |
| `search.list` in units, as documented today | 0 or 1, in its own bucket | 2 | 48 of 100, and **0 of the 10,000** |
| `search.list` in units, if the project were still charged the old way | 0 or 100 | 200 | 4,800 at the cadence, 6,000 at the cap: **4,000 of 10,000 left** |
| `videos.list` + `channels.list` | 2 to 5 measured (2 on a pass without a search: the known streams) | about 12 to 30 | about 300 to 700 (from the per-pass figures; guard 3,000) |

Either way at least 4,000 units a day stay free for `videos.list` / `channels.list` (the brief asks for 3,000). When the console confirms the separate bucket, `discovery.search_api_interval_s` = 1200 and `discovery.search_api_daily_calls` = 90 give 72 calls a day.

"Over an hour every term gets both" holds for recent videos (one OR query covers every term each hour). For live streams it cannot hold inside 100 calls a day with four terms (4 terms x 2 modes x 24 h = 192 calls), and the OR query that would make it hold finds few live streams (below). Each term gets its live search every four hours. What bridges the gap is item 1b.

**1b. Known live streams.** Without it a stream found by an hourly search would be seen once and its live slot released 20 minutes later ("not seen" counts as below the cutoff). Every pass now asks `videos.list` again for each stream that holds a slot and each stream whose newest observation within `discovery.live_recheck_window_s` (5400 s) was live, best first, at most `discovery.live_recheck_max` (50): viewers and growth stay current, and a stream that has ended becomes a video candidate (`origin: recheck`) and is queued for the VOD lane like any other. Cost: 1 unit per pass. Without the API key or over the unit budget these streams are simply not seen.

### Measurements on the real Data API (2026-11-17 about 08:00 UTC, staging key, GTA 5 terms)

| Call | Results | Usable |
|---|---|---|
| live, OR query, `order=relevance` | 50 live | 21 with a viewer count, **4 with 50+ viewers, 0 with 500+** |
| live, OR query, `order=viewCount` (first trial pass) | 15 live | top audience 4 viewers |
| live, `"GTA 5"`, `order=viewCount`, `relevanceLanguage=en` | 50 live | 46 with a count, **17 with 50+, 6 with 500+** (top 18,000) |
| live, `"GTA 5"`, `order=relevance`, no language | 50 live | 44 with a count, 19 with 50+, 7 with 500+ |
| recent 48 h, OR query, `viewCount`, no duration filter | 50 | **24** of at least 8 minutes (26 were Shorts) |
| recent 48 h, OR query, `viewCount`, `videoDuration=long` | 50 | **50**, of which 24 past streams; Treyten, Jefe and other English RP edits near the top |
| recent 48 h, OR query, `viewCount`, `videoDuration=medium` | 50 | 45, mostly non-English 13 to 19 minute edits |

`relevanceLanguage=en` does not keep other languages out (Portuguese, Indonesian and Hindi streams were in every listing); it is left on because the reference says it only orders by relevance to the language. Search result titles arrive HTML-escaped (`&#39;`); they are unescaped.

### Real pass with the yt-dlp search forced to fail (this machine, throwaway database `hs_live`)

Setup: `SIXSTARS_ENV_FILE` = the kit's `.env` (read only), `DATABASE_URL` = a throwaway Postgres with migrations 0001 to 0010 and the staging seed, the notify webhook pointed at a closed port, the yt-dlp search replaced by a fake that raises the server's exact error. Apify and the permission checks were switched off in that database (no Apify or Anthropic spend), `search_api_interval_s` = 0 and `search_api_calls_per_pass` = 2 so that one pass made both calls. Nothing was written to Supabase.

```
duration 11.84s  live_seen=50  videos_seen=50  creators=88 (88 new)
youtube_search      error    0    yt-dlp search failed for 'GTA 5': ERROR: query "GTA 5" page 1: Unable to download API page: ('Connection aborted.', RemoteDisconnected(...))
youtube_search_api  ok     100    live + recent search, 3 of 60 calls today
youtube_rss         skipped  0    no known creators yet
youtube_api         ok     100    2 quota units
search.list calls: 2 in the pass. videos.list + channels.list: 4 units in the pass.
live: 50 (39 not English, by a reported language or by a signal). videos: 50 (33 not English).
```

Top 5 live (score, viewers, language, channel, title):

```
58.10  18,031  und (term:shortsfeed)  SANGU IS LIVE      GTA 5 LIVE #gta5 #gtav #gaming
49.66   3,349  und (term:shortsfeed)  Gaming With VV     LIVE | tiger hunt challange | GTA V Mods #gtav #gtalive
45.37     149  en                     SH HARDIK GAMING   GTA 5 LIVE Panther Mode ON
44.68   1,217  pt                     XodaRapX           TA CHEGANDO!! MARATONA ANTES DO GTA 6! - GTA 5 ENHANCED
44.17     115  en                     Letest_official    GTA V PHANTHER EDITION
```

Top 5 videos (score, views, language, kind, channel, title):

```
71.42   97,799  en  edited (+6.0)   FuriousFade  Spinning my OPS with an ACTUAL RPG in GTA 5 RP!
67.25   13,858  en  edited (+5.0)   9ine         Caught The OPPS LACKING at a HOOD BLOCK PARTY in GTA 5 RP
66.82  283,359  en  edited (+3.7)   Jefe         My GANG Got ARRESTED.. So We Did a PRISON BREAK in GTA 5 RP.
64.05   20,732  en  edited (+3.69)  Fleeko       I SAVED my SONS with my GIRLFRIEND in GTA 5 RP..
61.60  268,883  pt  edited (+4.08)  dLorer       13 ANOS EM 1 VIDEO...
```

Reading it: the video list is the same kind of list M09 got from yt-dlp (FuriousFade, Jefe and Fleeko are in both), so for the VOD lane the Data API search replaces the dead source. The live list at 08:00 UTC is thin for English: the two big streams are "Shorts feed" mod showcases, now marked not English and ranked lower (the first would have scored 72 as English), but with nothing English above the cutoff of 55 the first one would still get a slot. That is decision D11 (allowed, ranked lower); the lever is `discovery.non_english_penalty`.

Data API use of the whole task: 11 `search.list` calls (2 + 2 in the two real passes, 5 in the comparison, 1 in the doctor, 1 in the live test) and about 20 units of `videos.list` / `channels.list`. Under the old accounting that would be about 1,120 units (limit of the task: 1,500). Apify: $0.

### 2. RSS

- The rotation was right: ordered by channel id, a cursor in `discovery.rss_cursor`, wrapping round, forbidden creators and Twitch creators left out. The integration test walks 7 creators with 3 feeds per pass and sees every creator within three passes and the wrap on the third and fifth.
- A finished stream found only by a feed is enriched, scored and queued for the VOD lane (`videos` row with `has_chat`, one `vod.process` job): tested with every search switched off.
- `discovery.rss_max_channels_per_run` 25 → 50 (feeds are free and are not refused from the server; 171 known creators are now covered every 40 minutes instead of 70).
- Not changed: a feed entry must still name the game in its title, and `clients/youtube.parse_feed` does not read the view count and rating the feed also carries (outside my scope; it would let a feed entry be scored without the Data API).

### 3. Source health

State in the settings key `discovery.source_health` (written by code). `ok` and `empty` count as working, `error` as failed; `skipped` and `not_configured` change nothing, except that a pass the yt-dlp search sits out while backing off counts towards the warning.

- After `discovery.source_failure_notify_after` (3) passes in a row: one `warn` "Discovery source <name> keeps failing" with the count, the time of the first failure, the error text in full and the fix for that source; event `discovery.source_failing`. Nothing more while it keeps failing.
- When a source that was reported works again: one `info` "Discovery source <name> recovered"; event `discovery.source_recovered`.
- yt-dlp search: after a failed pass the next one tries one search (a probe) instead of all twelve; after `discovery.ytdlp_backoff_after_failures` (2) failed attempts it skips itself for `discovery.ytdlp_backoff_s` (3600 s), then probes again. A day on the server costs the first 12 searches and then about one an hour, instead of 1,728; the pass no longer waits for doomed subprocesses.
- The same mechanism covers `youtube_search_api`, `youtube_api`, `youtube_rss`, `apify` and `twitch` (no back-off for those).
- Timeline on the server, from the integration test: pass 1 error (all searches), pass 2 error (one probe), pass 3 skipped and the warning goes out (20 minutes after the first failure), then silence.

### 4. Language

`defaultAudioLanguage` is what the uploader typed once. Two cheap signals may now overrule a reported `en` (or fill in a missing language) with `und` = "not English, language unknown":

1. Script: more than `discovery.non_latin_share` (0.3) of the letters of the title, the channel name or the first 500 characters of the description are not Latin.
2. Terms: the title, the channel name, a tag or a hashtag of the description contains one of `discovery.non_english_terms` (substring, any case): `hindi`, `tamil`, `indonesia`, `ao vivo`, `brasil`, `español`, ... The body of the description is not searched ("subtitles in Hindi" in an English creator's text must not count).

The result goes through the existing ranking rules (reach 0.6 instead of 1.0, score x `discovery.non_english_penalty`): allowed, ranked lower (D11). The signal is stored in `score_parts.language_signal` and shown by `run-once`. A language the API reports as not English is never changed.

**Honest limits.** A stream whose title, tags, hashtags and channel name are all English is still taken as English; only a transcript can tell. That is exactly the case of the "GTA 5 LIVE - DON'T BLINK! SECRET MOD ACTIVATED" streams seen on 2026-11-17 on six channels (audio language `en`, `en-US` or `en-IN`, channel country `US` or none, English tags). I looked at their metadata: there is nothing to detect. Because they are the streams that took the slots, three words that mark that genre are in the default list although they are not languages (`shortsfeed`, `shortslive`, `secret mod`); the setting's description says so. If these streams should never get a slot, `discovery.title_blocklist` is the place. Considered and not done: treating the region tag `en-IN` or the channel country as a language signal (it would mark English-speaking creators wrongly).

### 5. Yield

Settings changed by the migration, only while they still hold the seeded value (a value set by hand is kept):

| Key | Old | New | Reason (M08 notes) |
|---|---|---|---|
| `spikes.ab_min_confidence` | 0.6 | 0.55 | P1 reads the captions now and answers real moments at 0.50 to 0.55 |
| `vod.fallback_below_spikes` | 1 | 3 | the transcript fallback tops up a stream whose chat gave fewer than three confirmed spikes (about $0.06 per hour of stream) |
| `discovery.rss_max_channels_per_run` | 25 | 50 | item 2 |

Video ranking (`scoring.video_kind`, `scoring.yield_adjustment`; creator tiers untouched):

- **Edited upload or highlight** (no chat replay, at most `discovery.edited_max_duration_s` = 3600 s): up to `discovery.edited_upload_bonus` (6) points, in proportion to `(likes + 2 x comments) / views` against `discovery.edited_engagement_full` (0.04). Unknown engagement: half.
- **Long past stream** (at least `discovery.long_stream_min_s` = 3600 s): the score is multiplied by a factor between `discovery.thin_chat_factor` (0.75, no chat) and 1 (estimated chat replay at `discovery.chat_min_messages` = 1,500, the M04/M09 heuristic; the creator's measured rate is used when a chat of theirs was downloaded before).
- **Long upload without chat replay**: factor 0.75, no bonus.
- The parts are stored (`kind`, `yield_bonus`, `yield_factor`). The unit test puts the M08 case in one comparison: a 25-minute edit and a quiet 4-hour stream with the same audience; the stream won before, the edit wins now.

All five numbers are estimates set as settings, not measurements.

### 6. Cookies

`chat/net.py` `cookies_file()` is the one place that decides: `YOUTUBE_COOKIES_FILE` when the file exists, else None, with one warning `youtube_cookies_file_missing` per process. Used by `chat.open_live`, `chat.download_replay`, the chat commands, `vod.ytdlp.access_for` and discovery; `youtube_innertube.new_client` and `youtube_replay.build_ytdlp_command` also guard themselves, so a caller that passes the path straight through cannot crash them. The real doctor run below was made with the kit's `.env`, which names the absent `/secrets/youtube_cookies.txt`.

### 7. Operator table: what each thing the owner may send switches on

Set the variable in `/opt/6stars/.env`, restart the containers (`docker compose up -d`), no code change. Verify with `docker compose exec -T api sixstars discovery doctor` (add `--video <id of a past stream>` to test a chat replay); the row to look at is in the last column.

| Owner sends | What starts working | How, and how soon | Doctor row that must read OK |
|---|---|---|---|
| `PROXY_URL` (residential proxy; `downloads.proxy_platforms` already lists `youtube`) | yt-dlp search in discovery (12 free searches a pass, about 120 live streams); caption, window and audio downloads of the VOD lane; YouTube chat goes through it too | discovery: the next pass probes at once (the back-off ends because the access changed), one "recovered" message, the Data API search goes back to sleep; VOD lane: new videos at once, videos already waiting at their next retry (at most `vod.blocked_backoff_s` = 30 minutes) | `ytdlp_search`, `window_download` |
| `YOUTUBE_COOKIES_FILE` (Netscape file mounted at the path in `.env`) | caption, window and audio downloads of the VOD lane; chat replay and live chat requests carry the cookies | same timing as above for the VOD lane. The yt-dlp **search** does not use the cookies unless `discovery.ytdlp_use_cookies` is set to true (off by default: an account used for 1,700 searches a day is at risk, and the Data API search already covers discovery) | `window_download`, `chat_replay`; `environment` must say "YOUTUBE_COOKIES_FILE set" |
| `TWITCH_CLIENT_ID` + `TWITCH_CLIENT_SECRET` | Twitch live streams and the day's archives in discovery, follower counts, channel texts for the permission check | the next pass; a wrong key shows as source `twitch` in error and is reported after three passes | `twitch_token` |
| nothing | discovery on the Data API search (48 calls a day), channel feeds, the second look at known streams and Apify; downloads stay blocked and the videos wait | now | `data_api`, `rss` |

Chat needs none of the three (Twitch IRC and the YouTube chat endpoint worked without keys in M03); whether the YouTube chat endpoint answers from the server's address is what the `chat_replay` row shows.

### `sixstars discovery doctor`

`sixstars discovery doctor [--video ID_OR_URL] [--search-api] [--json]`. Read-only; each check makes one request the way the system would, with the same proxy and cookies rules. Statuses: `OK`, `BLOCKED` (the platform refused this machine: bot check, rate limit, a connection closed without an answer), `ERROR`, `not configured`, `skipped`, each with the class of the error. Cost: 1 unit of `videos.list`; with `--search-api` one of the 100 daily search calls. It always exits 0 (2 for a bad `--video`).

Run from this machine (home connection, kit `.env`, throwaway database):

```
check            status            time  detail
environment      OK                0.0s  APP_ENV staging; PROXY_URL not set; YOUTUBE_COOKIES_FILE names a file that does not exist (\secrets\youtube_cookies.txt); YOUTUBE_API_KEY set; Twitch keys not set; APIFY_TOKEN set
ytdlp_search     OK                2.8s  5 live results for 'GTA 5'
data_api         OK                0.5s  videos.list answered for 3ZGMLyZzrPI (1 quota unit)
data_api_search  OK                0.8s  search.list: 5 live results (1 search call)
rss              OK                0.6s  15 entries in the feed of UCA2sMhX7_2RNFVXeaZ9Hyzw
chat_replay      OK                1.2s  chat replay of 3ZGMLyZzrPI readable (first page: 50 actions)
window_download  OK                9.6s  5.0 s of 3ZGMLyZzrPI downloaded at 360p and deleted
twitch_token     not configured    0.0s  TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET are not set
apify            OK                0.0s  token set (not called: a run costs money)
```

Expected on the server today (from the integration test that feeds the two real error texts): `ytdlp_search BLOCKED [YtDlpError] ... Remote end closed connection without response`, `window_download BLOCKED [DownloadBlockedError] ... Sign in to confirm you're not a bot`, and a last line that names the fix. **Not run on the server** (the branch is not deployed). Command for the orchestrator after the deploy:

```bash
ssh -i C:/Users/iway/.ssh/6stars_ed25519 deploy@46.224.14.255 'cd /opt/6stars && docker compose exec -T api sixstars discovery doctor'
```

When the database knows no past stream yet, the chat and download checks use a 19-second public video without chat; add `--video <id>` of a past stream to test a chat replay.

### Settings added (migration 0010)

`discovery.search_api_mode` "fallback", `search_api_calls_per_pass` 1, `search_api_interval_s` 1800, `search_api_daily_calls` 60, `search_api_modes` ["live","recent"], `search_api_combined_modes` ["recent","completed"], `search_api_durations` {"recent":"long","completed":"long"}, `search_api_orders` {"live":"viewCount","recent":"viewCount","completed":"viewCount"}, `search_api_relevance_language` "en", `search_api_category_id` "20", `search_api_state` (written by code), `live_recheck_max` 50, `live_recheck_window_s` 5400, `source_failure_notify_after` 3, `ytdlp_backoff_after_failures` 2, `ytdlp_backoff_s` 3600, `ytdlp_use_cookies` false, `source_health` (written by code), `non_latin_share` 0.3, `non_english_terms` [32 words], `edited_upload_bonus` 6, `edited_max_duration_s` 3600, `edited_engagement_full` 0.04, `long_stream_min_s` 3600, `thin_chat_factor` 0.75 (all under `discovery.`). Changed: the three in the table of item 5.

### What other parts must know

- **Summary of `POST /v1/discovery/run`** (`DiscoverySummary.to_dict()`): new fields `youtube_search_api_calls`, `youtube_search_api_calls_today`, `notes` (list of strings said once). New source name `youtube_search_api` in `sources`. `errors` no longer lists the yt-dlp search while it backs off (status `skipped`), so the `discovery.run` event is `warn` only in the passes where a source really ran and failed.
- **Events**: `discovery.source_failing`, `discovery.source_recovered`. Notifications: the two titles of item 3.
- **`api_usage`**: `youtube` / `search.list`, 1 unit per call. Anything that sums YouTube units for the 10,000 budget must leave these rows out (`store.youtube_units_today` does).
- **`chat.net.cookies_file()`** is what the live lane (M14) should pass as `cookies_file`.
- **Candidates**: `origin` can now be `ytapi` or `recheck`; `language` can be `und` with `score_parts.language_signal`.
- `cli.py` got one block (`discovery doctor`); nothing else outside my paths was edited, apart from the `search_list` method, its fake and tests in `clients/`, as the brief allows.

### Deviations from the brief

1. Quota: counted in calls of the separate `search.list` bucket, not in 100-unit steps (documentation, above).
2. Rotation: live searches take one term per call; only recent searches combine the terms (measurement, above). "Every term gets both within an hour" is met for recent videos only.
3. Added without being asked: the second look at known live streams (1b), `videoDuration=long` for recent searches, the quota pause, three genre words in the term list, and the access check that ends the two back-offs when a proxy or cookies arrive.
4. The yt-dlp search does not use the cookies by default (`discovery.ytdlp_use_cookies`).
5. `tests/unit/vod/test_vod_pure.py` and the VOD integration fixture pin or account for the old `vod.fallback_below_spikes` so that each existing test still exercises one path; the new default has its own test.

### Not verified

- Nothing ran on the staging server: the doctor there, the Data API from the server's address (the staging report says `videos.list` works there), feeds from the server, pass time with the back-off.
- Which quota accounting the Google project is on (see item 1).
- `PROXY_URL`, a real cookies file and Twitch keys: none exists yet. The paths are covered by fakes; `yt-dlp --cookies` writes the jar back on exit, so a cookies file mounted read-only may fail (not tried).
- The full integration suite was not run here (over 30 minutes through the tunnel); CI runs it. Run here: `tests/unit` and the integration files I touched.
- The yield numbers and the language term list are settings chosen from one day of listings; no clip was rendered in this task.
- The live list was taken at 08:00 UTC, a quiet hour for English streams; an evening listing was not compared.
