# 28 — Learning Loop and Growth Engine (design spec)

Source: "6 Stars Clips — Learning Loop and Growth Engine design", Nov 19, 2026 (Muhammed). Copied verbatim
in substance; this file is the spec. Implementation notes, deviations and owner amendments are at the end
(section "Implementation notes").

## Summary

We're building two connected systems in one release, tested fully on staging before production:

- **Part A, the Learning Loop.** Every posted clip is measured at fixed ages and given a 1-to-6 Star Score. A
  weekly job learns which clip features predict stars and re-weights the posting queue.
- **Part B, the Growth Engine.** It spends up to $500 a month through Zernio on TikTok, Meta and Google Ads.
  Every campaign runs on a pre-set plan with KPIs and checkpoints, fully automated after shadow and approve
  phases.

## Decisions from Muhammed (19 Nov 2026)

| Topic | Decision |
|---|---|
| Goal | YouTube Partner Program first. Clipping campaigns stay manual, outside the engine. |
| Budget | $500 a month on a credit card. It's a cap, not a target: money moves only when a plan's rules say so. |
| Accounts | TikTok 6starsclipping (personal), Instagram @6starsclippy, YouTube @6starsClips-m3x (named "GTA VI Clips"). TikTok, Meta and Google ad accounts exist. |
| Ads access | Zernio API for every ad action (key in hand) |
| Jev | Yes, no monthly cap |
| Language | English only, worldwide English speakers, cheapest cost per follower first |
| Autonomy | Learned weight changes up to ±25% a week apply automatically. Boosts go shadow → approve → auto. 20% exploration. |
| Delivery | Built now, fully tested on staging, then promoted to production behind flags |

## Three blockers found while researching

1. **No clip can be boosted yet.** Rockstar's footage policy covers fan videos but explicitly excludes
   advertisements. Every clip we post contains GTA VI footage and a streamer. The Clip Boost lane is built but
   stays locked per clip until both rights exist. Real money goes to a Profile Growth lane that uses only our
   own creative (Part B).
2. **TikTok shows zero views.** That points to private or unpublished posts, not weak clips. It must be fixed
   before TikTok data can train anything.
3. **The YouTube channel is named "GTA VI Clips".** The account name appears on every ad, and it uses
   Rockstar's trademark. Rename it to the brand, "VI Stars Clips", before any YouTube ad runs.

## Fix first: TikTok zero views and the channel name

TikTok at zero views after about 13 posts is a delivery problem, not a content one: YouTube and Instagram
clips of the same moments get 2.5K–5K. Until it's fixed, TikTok rows are kept out of training so they don't
teach the model that every clip failed there.

| Likely cause | How to check | Fix |
|---|---|---|
| Posts land as private | Open a post in the TikTok app and check who can view it. Unaudited posting apps are forced to private, and a public request still comes back private with no error (bundle.social). The collector already saw "Post uploaded as Private" on the first real upload. | Ask Upload-Post support whether their TikTok app is audited for public posting on our profile. If not, post TikTok through Zernio and compare. |
| The account itself is private | `GET /api/uploadposts/tiktok/settings`: a private account lists no `PUBLIC_TO_EVERYONE` option (Upload-Post) | Switch the account to public in the app |
| Still in moderation | A TikTok post gets its ID only once it's public and has passed moderation (Upload-Post) | Nothing to fix if IDs arrive within hours; alert if one is missing after 6 h |
| Reach held back on a new account | Views are above zero but stuck under about 200 | Fewer, stronger posts; this is what Part A learns |

**New guard, all platforms.** If the median views of an account's last 5 posts are still zero 6 hours after
posting, the system alerts on Telegram, pauses that account's hourly lane and marks its rows
`delivery_suspect`. A Telegram command resumes the lane.

**Rename the YouTube channel to "VI Stars Clips".** Ads show the account name, and TikTok's ad policy bans
using third-party brand names in a way that could mislead people about an affiliation. The handle
@6starsClips-m3x can stay.

## Part A, measurement: snapshots at fixed ages

Each post is measured at 1 h, 3 h, 6 h, 24 h, 72 h and 7 days. Each reading is a new row, so a 3-hour-old
clip is only ever compared with other clips at 3 hours. Today the collector overwrites one set of numbers on
posts every 2 hours with a 20-second budget, so it can't keep up with ~42 posts a day.

### What each platform gives us per post

| Signal | TikTok (Upload-Post) | Instagram (Upload-Post) | YouTube (Data + Analytics API) |
|---|---|---|---|
| Views, likes, comments | Yes | Yes (plays) | Yes |
| Shares, saves | Yes | Yes | No |
| Reach | Yes | Yes | No |
| Completion | `full_video_watched_rate` + per-second retention | Not documented | `averageViewPercentage` (needs the analytics scope; Muhammed approved the re-auth) |
| Average watch time | `average_time_watched` | Not documented | `averageViewDuration` |
| Followers from the post | `new_followers` | No | `subscribersGained` per video |
| Where views came from | `impression_sources` (For You, Search, Follow) | No | Traffic source report |

Source for the Upload-Post columns: Get Analytics. TikTok fields can take hours to appear and are left out
rather than sent as zero, so a missing field is stored as null, never 0.

### Collector changes

- New table `post_metrics_snapshots` (post, platform, captured_at, age_h, every field above, raw jsonb, paid
  flag). Rows are only added, never overwritten.
- A due-snapshot queue: the youngest post due for a reading goes first. The time budget goes up, kept inside
  Upload-Post's live limit of 100 requests per 5 minutes.
- The WF05 schedule moves from every 2 h to every 30 min, so the 1 h and 3 h readings are actually taken.
- Paid numbers are kept apart. While a post is boosted, its snapshots record organic and paid figures
  separately where the platform splits them. Where it doesn't, the post's Star Score freezes at its last
  pre-boost reading.

## Part A, Star Score and the learning loop

Every post gets a 1-to-6 Star Score that compares it with our own account at the same age, never with raw
views. The posting queue then learns which clip features predict 5 and 6 stars.

### Star Score (per post, final at 24 h, provisional at 3 h and 6 h)

1. **Views ratio:** views ÷ the account's median at the same age over the last 14 days (`delivery_suspect`
   rows excluded), on a log scale.
2. **Quality:** completion or average-view percentage, share rate and follow rate, each as a z-score within
   the platform.
3. **Blend:** 50% views ratio and 50% quality, turned into a percentile within the platform.
4. **Stars:** bottom 20% = 1, then 2, 3, 4; top 10% = 5; top 3%, or at least 10× the median, = 6.
5. **Clip Star:** the same clip goes to all three platforms in the same hour. Its clip-level score is the
   weighted median of its platform scores, plus a bonus when two or more platforms agree.

### Features per clip (a `clip_features` view, mostly from data we already store)

- **Spike:** percentile for the channel, tier, reaction share, loudness, first-time boost.
- **AI score parts:** hook, clarity, payoff, rewatch.
- **Clip:** length, live or VOD, event type, creator, hook and title wording, render style, music ratio.
- **Timing:** minutes from moment to post, posting hour, days since launch.
- **Jev tags:** about 25 typed questions per clip, each answered as a probability. Examples: quotable line in
  the first 2 s, meta moment, reversal, shouting, clear without context, title under 8 words, names the
  streamer. Jev takes text input, so it gets the transcript, title, caption, hook and our frame descriptions.

### Weekly learning (Mondays, automatic within limits)

- **Fit:** a regularised regression of Clip Star on the features, using only rows from the last 28 days, with
  recent weeks weighted more. It needs at least 60 scored clips; until then it only reports.
- **New rank:** predicted_star × freshness × tier bonus, plus a campaign-creator bonus (organic only, as
  Muhammed asked).
- **Shadow week first:** the learned rank runs beside the current rank for 7 days and logs what it would have
  posted. It only takes over when its picks would have scored higher.
- **Limits:** weights move at most ±25% a week, stored in `scoring_config` key `rank.learned_weights` and
  versioned. One flag rolls it back.
- **Exploration:** 20% of hourly slots go to Thompson-sampled picks across creator, moment type and length
  bucket, so the queue doesn't collapse onto one streamer.
- **Momentum:** a post that reaches 5 or 6 stars at 3 h boosts queued clips from the same creator or topic for
  the next 6 h.

**Weekly review by Claude:** it compares matched star and flop pairs (same creator, similar time), writes
hypotheses into `docs/reports/<week>.md`, and adds the useful ones as new Jev questions.

## Part B, ad rules: every restriction and how the system avoids it

The engine fails closed: any rule it can't prove is met counts as broken, and nothing is spent. Each rule
below is a hard check in code, not a weight, and learning can never loosen it.

| Restriction | Source | What the system does, automatically |
|---|---|---|
| Rockstar allows fan videos, but its policy doesn't cover advertisements | Rockstar policy | Every clip has `ad_rights.rockstar = false` by default, so Clip Boost is locked. It unlocks only with a written licence from Take-Two (copyright@take2games.com, case by case). |
| Ads need rights to every video, image and person's likeness in them | Meta; TikTok bans clips from unauthorised media sources | `creators.ad_rights` stores a written permission and a link to the proof. With none, that creator's clips are never boosted. |
| No unauthorised third-party brands that suggest an affiliation | TikTok policy | Ad copy check: a blocklist (GTA, Grand Theft Auto, Rockstar, Take-Two, streamer names) plus a Jev check ("implies affiliation?") on every headline and caption. The YouTube channel rename is required before YouTube ads. |
| Music must be licensed for ads | Meta | Profile Growth creative uses only our own sound, or TikTok's ad-cleared library. A clip with any music is never boosted. |
| Clipping campaigns forbid paid boosting of submitted clips | Typical rule: "Organic traffic only. Paid boosting … prohibited" (Content Rewards) | `posts.organic_only` is set when Muhammed submits a post to a campaign (Telegram `/campaign <post>` or a console toggle). Every clip of a creator with an active campaign is organic-only by default. |
| Paid YouTube views don't count toward YPP | YouTube rules summary | YouTube campaigns always optimise for subscribers, never for views |
| Editing a rejected ad and resubmitting it looks like dodging review | Meta treats "circumventing review systems" as a violation | A rejected creative is never edited and resubmitted automatically. It's retired and logged, and Telegram asks Muhammed. Two rejections on one platform in 7 days pause that platform. |
| A promoted TikTok post can't be deleted until the ad is stopped | Relevant Audience | Guardian stops the ad before it removes or hides any boosted post |
| Meta needs beneficiary and payer names when ads reach EU users | Zernio boost docs (DSA fields) | Both are filled from config on every Meta campaign |

What this means today: the Profile Growth lane can start once the creative is approved. The Clip Boost lane
is fully built and tested in shadow mode, but it spends nothing until a clip has both a creator permission and
a Take-Two licence.

## Part B, two paid lanes

The engine runs two lanes that share one budget, one plan system and one control loop. Profile Growth gets all
the money until Clip Boost is unlocked; after that, Clip Boost can take up to 60%.

| | Profile Growth | Clip Boost |
|---|---|---|
| Buys | Followers and subscribers | More reach for a clip that's already a 5 or 6 star |
| Creative | Our own brand videos: VI Stars neon identity, motion text, our own sound. No game footage, no streamer, no Rockstar marks. | The organic post itself (TikTok Spark Ad, Instagram post boost, YouTube video asset) |
| Objective per platform | TikTok: follows (community interaction). Instagram: profile visits. YouTube: subscribers. | TikTok: video views + follows. Instagram: reach. YouTube: subscribers. |
| Can spend today | Yes, after Muhammed approves the creative set once | No. Shadow only until a clip has both rights |
| Main KPI | Cost per follower or subscriber | Organic lift + cost per follower |

### Profile Growth creative, made by the system

- **Templates:** a template set in `assets/ads/` renders 6–10 s vertical variants with the existing factory
  (ffmpeg). Variables are hook line, colour scheme, length, end card, and whether the clip count or posting
  rhythm is shown ("a new moment every hour").
- **Checks:** every variant passes the ad copy check and a Jev review before upload. On YouTube, the variant
  is uploaded unlisted so Google Ads can use it as a video asset.
- **Approval:** Muhammed approves the template set once in approve mode; after that, new variants from
  approved templates go straight to tests.
- **Fatigue:** a variant retires when frequency passes 3, or its cost per follower rises 40% over its first 3
  days.

This lane is weaker on paper than boosting a viral clip. It's the one way to spend today without breaking a
policy, and the follower cost tests (next section) show quickly whether it's worth the money. If cost per
follower stays above the ceiling for two weeks, the lane pauses and the budget sits unspent.

### Clip Boost Boost Score

Computed in shadow for every 5- and 6-star post, so it's tuned before it ever spends:

```
priority = (Σ_i w_i·z_i − w_risk·r) / expected cost per follower
```

The sub-scores are velocity vs the account median, momentum (rising or decaying), completion, share rate,
follow rate, cross-platform agreement, the creator's past boost results, freshness of the moment, and Jev's
ad-fit. Risk counts the creator's claim history, profanity and sensitive topics. The weights live in
`scoring_config` key `boost.weights` and learn weekly within ±25%.

## Part B, targeting: English speakers, cheapest followers first

The first two weeks are a test: three geo cells per platform run on equal small budgets, and the money then
follows the cheapest followers who keep watching. Language targeting is always English.

| Cell | Countries | Why |
|---|---|---|
| A: low cost | India, Philippines, Pakistan, Nigeria, Kenya, South Africa, Ghana | Large English-speaking audiences with very low ad prices. One published YouTube Promotions test reached about $0.02 per subscriber with a similar mix. |
| B: high value | US, UK, Canada, Australia, New Zealand, Ireland | Costly followers whose later views earn the most |
| C: broad | Worldwide, English language, the platform's own optimisation | Lets each platform find cheap pockets we didn't list |

**The trade-off.** A cell A follower is cheap, but each of their later Shorts views earns little once we're
monetised. YPP entry counts subscribers and views wherever they come from, so before YPP the engine optimises
cost per follower. After YPP, it changes the score to cost per follower ÷ expected revenue per follower, and
budget drifts toward cell B.

### How a cell is judged (after 3 days)

- Cost per follower or subscriber.
- 7-day retention: followers still following after a week, where the platform reports unfollows.
- Follow-on watching: the share of our next organic views that come from followers (TikTok `audience_types`,
  YouTube subscriber views). Cheap followers who never watch again are worth nothing.
- Score: cost per watching follower = cost ÷ (followers × follow-on share). The lowest cost per watching
  follower wins the next step of the plan.

Age targeting stays 18+ on every platform: GTA VI is rated for adults, and an adult audience keeps the ad
accounts safe.

## Part B, plans, KPIs and checkpoints for the $500 cap

Every campaign runs on a plan picked when it starts. Each plan sets the first day's budget, how the daily
allowance moves, the most days it can run and a hard total. The first month should spend well under $500:
roughly $120 on tests, with the rest released only to green campaigns.

### Plans (rows in `ad_plans`, editable without a deploy; amounts in USD)

| Plan | Lane | Starts when | Day 1 | Green day | Amber day | Red day | Max days | Hard total |
|---|---|---|---|---|---|---|---|---|
| Cell test | Profile Growth | New cell × creative variant | $2 | Continue | Continue | End | 3 | $6 |
| Scale | Profile Growth | A cell test won its checkpoint | $5 | ×1.5, up to $20/day | Hold | ×0.5; second red ends it | 7 | $80 |
| Evergreen | Profile Growth | A Scale plan finished green | Best green day | Hold | Hold | End | Re-checked every 3 days | Monthly cap |
| Probe | Clip Boost | 5-star post, top 40% priority | $5 | Move to Standard | End | End | 1 | $5 |
| Standard | Clip Boost | Top 15%, or a green Probe | $10 | ×1.5 | Hold | ×0.5; second red ends it | 3 | $40 |
| Hero | Clip Boost | 6 stars on 2+ platforms, top 3% | $20 | ×1.5 | Hold | ×0.5; second red ends it | 4 | $100 |

### Starting KPI ceilings

Muhammed asked for a recommendation; they're learned weekly within ±25% once there are 10 or more finished
campaigns.

| KPI | Green | Amber | Red |
|---|---|---|---|
| Cost per watching follower, cell A | ≤ $0.05 | ≤ $0.10 | > $0.10 |
| Cost per watching follower, cell B | ≤ $0.25 | ≤ $0.50 | > $0.50 |
| Cost per watching follower, cell C | ≤ $0.08 | ≤ $0.15 | > $0.15 |
| Organic lift (Clip Boost only) | ≥ 1.2 | 1.0–1.2 | < 1.0 |
| Ad rejections, per platform per week | 0 | 1 | 2 or more (platform pauses) |

A day is green when every KPI is green, amber when one is amber, and red otherwise. Budgets change once per
plan day, never hourly, because ad platforms re-learn after big changes.

### Checkpoints

- **Day 3, per cell:** keep the cheapest cell per platform; end the rest.
- **Day 7, per lane:** if no cell is green on a platform, that platform stops for the month.
- **Day 14, mode gate:** approve mode moves to auto only if spend matched our ledger every day, nothing was
  rejected, and at least one platform is green.
- **Monthly, the 1st:** a report on spend, followers bought, cost per watching follower by cell, and Star
  Score effects. Weights and ceilings are re-learned and the next month's cap resets.

## Part B, control loop

Every campaign in both lanes moves through the same states, checked every hour by `POST /v1/ads/cycle`.
Budgets change only at the end of a plan day; a stop can happen in any cycle.

Each hourly cycle runs these steps in order:

1. **Reconcile:** read spend and delivery for every live campaign from Zernio into `ad_days` and the ledger.
   Pause the platform on any mismatch over 10%.
2. **Stops:** end campaigns hit by a takedown, a claim, an `organic_only` flag, the kill switch, an ad
   rejection or an account warning.
3. **Day close:** for each campaign whose plan day ended, set the day colour and the next allowance, or end
   it.
4. **Candidates:** Profile Growth uses due cell × creative tests; Clip Boost uses 5- and 6-star posts that
   pass every ad rule. Every result is logged in `scoring_log`.
5. **Starts:** fill free slots by priority within the day's remaining allowance. Shadow mode logs only.
   Approve mode sends a Telegram card (plan, cell, creative, cost) that expires after 2 h. Auto mode creates
   the campaign through Zernio.
6. **Report:** one Telegram line per start, end, pause and checkpoint verdict.

### Guardrails and kill switches

Money is protected twice: by our code, and by spending limits set inside each ad account that hold even if
our code fails.

| Limit | Value | When hit |
|---|---|---|
| Monthly cap | $500 (`ads.budget.monthly_usd`) | No new campaigns; running ones finish the current day |
| Daily ceiling | Remaining cap ÷ days left, never above $40 | New campaigns wait for tomorrow |
| Exploration | 20% of each day's spend | New cells and variants wait |
| Per creator per day (Clip Boost) | 30% of the daily ceiling | That creator's next candidate waits |
| Running at once | 4 per platform | Candidates queue by priority |
| Platform backstop | Account spending limit of about $200 on each ad account, set once by Muhammed | The platform itself stops delivery |

Automatic pauses (one platform at a time; only Muhammed resumes them):

- Platform-reported spend more than 10% above our ledger
- 2 or more ad rejections in 7 days
- Any policy warning or restriction on the ad account
- Spend data older than 3 h
- A card payment failure reported by the platform

**Kill switch.** `ads.enabled = false` pauses every live campaign within one cycle. In Telegram:

- `/ads stop`: everything, now
- `/ads status`: live campaigns, today's spend, this month's spend
- `/ads yes <id>` and `/ads no <id>`: approve-mode cards
- `/campaign <post>`: mark a post organic-only

The console gets an Ads page with the same controls.

**Learning limits.** Weekly learning may move score weights and KPI ceilings by at most ±25%. It can never
change ad rules, caps, the backstop, the platform split or the mode.

## Architecture: what we add to the repo

Two new packages, `learning/` and `ads/`, behind control-API endpoints that n8n triggers, the same way stats
work today. Migrations only add, every behaviour is behind a flag, and secrets live in Supabase or n8n
credentials.

### External services

| Service | Used for | What we know | To prove on staging |
|---|---|---|---|
| Zernio Ads API | Every ad action on TikTok, Meta and Google | `POST /v1/ads/boost` takes a Zernio post ID or a native platformPostId. It accepts a goal (engagement, awareness, video_views and others), a budget and a bid strategy; TikTok ads need their own TikTok Ads connection, separate from posting. Ad analytics come with a daily breakdown. | Can it boost posts made through Upload-Post? Does it have a follower or subscriber goal on each platform? Budget updates and pause through the API |
| Upload-Post | Posting (unchanged) and per-post organic analytics | Fields as in Part A | TikTok posts are actually public |
| YouTube Analytics API | Per-video retention and subscribers gained | Needs the analytics read scope (re-auth approved) | Report latency per video |
| Jev (TypeSafe) | Typed tags and ad copy checks | Text in, yes/no, choice or score out, with probabilities | Cost and speed per 100 clips |

If Zernio can't run a YouTube subscriber campaign, the YouTube part falls back to the Google Ads API
directly. Everything else stays on Zernio.

### New tables

| Table | One row per |
|---|---|
| `post_metrics_snapshots` | Post × reading (age, organic and paid figures, raw) |
| `star_scores` | Post (provisional and final Star Score, inputs, config version) |
| `jev_tags` | Clip × question (probability, model version) |
| `ad_rights` | Creator or clip (Rockstar licence, creator permission, proof link, expiry) |
| `ad_creatives` | Profile Growth variant (template, text, file, per-platform asset ids, approval, status) |
| `ad_plans` | Plan (lane, start rule, ladder, KPI ceilings) |
| `ad_campaigns` | Campaign lifecycle (lane, platform, plan, cell, creative or post, mode, status, Zernio ids, end reason) |
| `ad_days` | Campaign × plan day (allowance, spend, impressions, views, followers, colour) |
| `ad_ledger` | Money movement (amount, source: ours or platform, reconciled) |

New columns: `posts.organic_only`, `posts.boosted`, `posts.delivery_suspect`. The view `clip_features` joins
clips, `scoring_log`, `jev_tags` and `star_scores`.

### Code

- `src/sixstars/learning/`: `snapshots.py`, `star.py`, `features.py`, `jev.py`, `fit.py`, `explore.py`.
- `src/sixstars/ads/`: `rules.py` (the hard checks, pure functions), `copycheck.py`, `score.py`, `plans.py`,
  `creative.py`, `controller.py`, `ledger.py`.
- `src/sixstars/clients/`: `zernio.py`, `youtube_analytics.py`, `jev.py`. Each has a fake for tests.

Flags in `scoring_config`: `learning.snapshots`, `learning.star`, `learning.rank` (off / shadow / live),
`learning.explore`, `ads.enabled`, `ads.mode` (shadow / approve / auto), `ads.lanes`, `ads.platforms`,
`ads.budget`, `ads.rules`, `ads.weights`.

### n8n

- WF05 stats: moves to every 30 min.
- New WF10 ads-cycle (hourly), WF11 ads-reconcile (every 3 h) and WF12 learning-weekly (Mondays 06:00 UTC).
- WF08 gets the `/ads` and `/campaign` commands; WF06's daily report gets Star Score and ad spend lines.

Console: a Performance page (Star Score leaderboard, sub-score breakdown and the reason for each score) and an
Ads page (live campaigns, plan day, colour, spend vs allowance, controls).

## Staging test plan and the production gate

Nothing reaches production until every test below passes on staging. Production then starts with every new
flag off, and flags turn on one at a time.

### On staging

1. **Unit tests:** every ad rule in its pass and fail case; plan ladders and day colours; Star Score maths;
   ledger reconciliation; the ad copy check against the blocklist.
2. **Replay:** copy the last 7 days of production posts and snapshots into staging. Run Star Score, features
   and the learned rank in shadow, and check that they reproduce the known stars and flops.
3. **Zernio capability probe:** a read-only listing of ad accounts and goals per platform. Then one campaign
   per platform at the minimum budget, created paused, read back and deleted. This answers the open questions
   in Architecture.
4. **Fault drills:** each must end with the right pause and a Telegram alert:
   - a spend mismatch, an ad rejection and a card failure;
   - a takedown on a boosted post (the ad stops before removal);
   - an `organic_only` post entering the candidate list;
   - Zernio unreachable, and the kill switch.
5. **48 hours end to end:** the full hourly cycle runs in shadow mode against staging data, with no errors and
   a ledger at $0.

### Production gate

Every box must be ticked before the first production flag flips.

- [ ] All staging tests and drills green, with results in `docs/CHANGELOG-scoring.md`
- [ ] TikTok delivery fixed: new posts get views from strangers
- [ ] YouTube channel renamed to "VI Stars Clips"
- [ ] Spending limits set inside each ad account (the backstop)
- [ ] Profile Growth creative set approved by Muhammed
- [ ] Production snapshot and backups taken (existing safety rule 1)

Order of switching on in production: `learning.snapshots` → `learning.star` → `learning.rank = shadow` (7
days) → `ads.enabled` in shadow mode (3 days) → approve mode → `learning.rank = live` → auto mode after the
day-14 checkpoint. Each step is logged with its rollback flag, and the existing rollback triggers apply.

## Remaining decisions (owner only; none blocks building or staging tests)

- [ ] Rights for Clip Boost: should the system draft a licence request to Take-Two (copyright@take2games.com)
  and permission requests to the top creators? Without both, Clip Boost stays in shadow.
- [ ] TikTok delivery: confirm in the app whether posts are private. Ask Upload-Post whether their TikTok app
  is audited for public posting.
- [ ] Backstop: set a spending limit inside each ad account (about $200 each).
- [ ] Creative: approve the first Profile Growth template set when it's rendered on staging.

The targeting video Muhammed shared ("I Bought 1,000 REAL YouTube Subscribers", Marcus Jones) couldn't be
read past its description: YouTube showed a bot check. The cell design uses the published campaign data cited
in the targeting section instead; Muhammed's notes from the video can still change the cells.

Sources (opened 18–19 Nov 2026): Rockstar's footage policy, and the TikTok and Meta ad IP policies;
Upload-Post's analytics and TikTok settings docs, and Zernio's ads docs; Content Rewards campaign rules,
YouTube YPP coverage, and the CPM and promotion data. Repo files read: `stats/collector.py`,
`posting/ranking.py`, `posting/selection.py`, `supabase/migrations/0026_scoring_config.sql`,
`supabase/seed/production.sql`, `docs/22-account-setup-guide.md`, `docs/25-later-phases.md`; project file
`gta6-priority-creators.csv`.

---

## Implementation notes (added 2026-11-19 when the build started)

### Owner amendments (19 Nov, on plan approval)

1. Delivery guard: a post counts as 0 views only if `stats_updated_at is not null` (it has actually been
   read). The TikTok missing-ID rule stays: a TikTok post with no platform post id 6 h after posting counts as
   0.
2. Every TikTok row is left out of Star Score medians, percentiles and training (flag
   `learning.exclude_tiktok`, on) until L0 confirms delivery is fixed, not just the last 5 posts.
3. The snapshot pass runs before the `posts.*` totals refresh. If 25 s can't cover the due snapshots,
   collection moves to a worker job and the n8n request only enqueues it.
4. The A2 Zernio probe uses a neutral brand image or creative only, never a GTA clip.
5. L1 checks on a live post whether Upload-Post returns `subscribers_gained` and `average_view_percentage`
   for YouTube without our yt-analytics scope, and reports it.
6. Ad copy blocklist: creator handles match only as `@handle` or the full display name. GTA, Grand Theft
   Auto, Rockstar and Take-Two match as whole words.
7. A1–A3 run in parallel with L2–L4.

### Deviations from the spec

- **Migration numbers:** 0027–0031 were taken by M18 design styles, so the spec's
  `0027_learning_and_ads.sql` is split into one migration per milestone, numbered from 0032.
- **YouTube metrics:** read from Upload-Post's YouTube `post_metrics` first (owner decision, 19 Nov). The
  YouTube Analytics client is built with a fake but stays optional until the channel is re-authorised with
  `yt-analytics.readonly`.
- **One TikTok account:** `tiktok_live` is disabled (one real TikTok account, `tiktok_main`).
- **Hourly-only pause:** `platform_accounts.paused_until` pauses both lanes, so the guard uses new columns
  `hourly_paused_until` / `hourly_pause_reason`.
- **Telegram arguments:** WF08 matches only the first word; `/ads` and `/campaign` send the full message text
  to the API.
- **Staging has no view data:** staging posts are private (D102), so Star Score and learning are proven on
  staging with the 7-day production replay.
- **Branching:** milestone PRs merge into `develop` with every flag off. The code and additive migrations may
  reach production dark when `develop` is promoted; no production flag flips before the gate.
