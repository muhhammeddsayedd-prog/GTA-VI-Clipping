# L0 — TikTok zero views: diagnosis (2026-11-19 ~05:25 UTC)

Read-only checks against production (DB, Upload-Post, TikTok's public pages). Nothing was changed.

## Findings

| Check | Result |
|---|---|
| `posts` (tiktok_main, not dry run) | 16 published, 17 Nov 21:03 → 19 Nov 02:03 UTC. **All 16 have a real `platform_post_id` and `post_url`.** All read by the collector (`stats_updated_at` set); views 0 and likes 0 on every one. |
| `GET /api/uploadposts/tiktok/settings?profile=default` | `privacy_level_options` = PUBLIC_TO_EVERYONE, MUTUAL_FOLLOW_FRIENDS, SELF_ONLY → **the account is not private.** |
| `platform_accounts.tiktok_main.privacy` | `PUBLIC_TO_EVERYONE` (what we send). |
| Upload-Post history (2 posts) | `success: true`, no error code, `changes: []`, no `fallback_to_inbox`, `first_comment_posted: true`. |
| Upload-Post post-analytics | `post_metrics_source: platform_api`: views 0, reach 0, likes 0, comments 1 (our own first comment), `new_followers` 0, `impression_sources` "For You" = 1 (share, no views behind it), `audience_types` all 0. |
| Upload-Post profile analytics | followers 0, impressions 0, profileViews 0, video_count 16, 30-day reach all 0. |
| TikTok oEmbed (logged out) | 200 for all three videos checked: oEmbed only answers for public videos. |
| TikTok video page (logged out) | `privateItem:false`, `secret:false`, `forFriend:false`, **`playCount:0`**, `commentCount:1`. |

Same window, other platforms (posts older than 24 h): Instagram median 235 views (max 263), YouTube median 21
(max 514).

## Conclusion

The posts are **public and published**, and TikTok itself reports **0 plays**, not just Upload-Post. The
"posts land as private", "account is private" and "still in moderation" causes are ruled out (the "Post
uploaded as Private" note from M15 came from the staging `SELF_ONLY` test upload). The "new account held back"
row doesn't fit either: a public video normally gets at least a few test views, and we have exactly 0 on all
16, including 2-day-old posts.

Exactly 0 plays on public videos means TikTok is **not distributing the account at all**. The usual cause is
an account-level restriction: "not eligible for the For You feed" (often unoriginal or reposted content: our
posts are re-cut Twitch clips) or an automation/new-account flag. This is visible only in the TikTok app:

- Settings and privacy → Account → **Account status** (violations, "For You feed eligibility").
- On a video: ⋯ → **Analytics**, and check for "Ineligible for For You feed" / "Under review".

## Not changed (needs the owner)

- No posting code changed. The TikTok hourly lane is still posting (16 posts so far).
- If the delivery guard (L0) is turned on, it fires at once for `tiktok_main` and pauses its hourly lane.

Side note: `youtube_main` is paused until 2026-11-19 12:02 UTC after a YouTube rate limit (cap_24h 24 → 22).
That is the existing behaviour and unrelated to this.
