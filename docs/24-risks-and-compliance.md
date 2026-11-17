# 24 — Risks and Compliance

| Risk | Likelihood | Impact | Mitigation in the system |
|---|---|---|---|
| Copyright claims/strikes (reposting creators' footage) | Medium–high | High (3 YouTube strikes end a channel) | Creator tiers (restricted never clipped), credit on every clip, transformation layers (hook, subtitles, layout), music detection, private pre-check, auto-removal, block creator on first claim, emergency stop at 2 strikes |
| Rockstar/Take-Two game footage | Low for gameplay clips | Medium | No leaked/pre-release material: discovery skips titles with "leak"; spoiler gate |
| Reused/unoriginal content rules (reach limits, YPP rejection) | Medium | High for revenue | Visible editing on every clip (hook, styled captions, layout, zoom, end card, commentary line); avoid near-duplicates; quality gate score |
| TikTok/Instagram flags for automation | Medium | Medium | Official APIs through Upload-Post, jitter, minimum gaps, caps under limits, no comment automation |
| Platform API limits change | Medium | Medium | Caps in DB, auto-lowering on errors, per-account pause |
| Post removal impossible via API (TikTok/IG) | Medium | Medium | Telegram alert with links (only manual step) |
| Spoilers at launch | High in week 1 | Medium (reports, angry comments) | Spoiler gate until 2026-12-03; never in titles/hooks |
| Bad AI judgement (off-tone hook, wrong translation) | Medium | Low–medium | Gate score, safety flags, daily report sampling |
| IRL/personal info on stream | Low (GTA focus) | High | Safety flag `personal_info`; discovery restricted to the GTA category/terms |
| Download blocking (YouTube) | High from data centres | Medium | Residential proxy, cookies, tool auto-updates, transcript fallback |
| 38-hour build window | High | High | Milestone order puts posting and VOD lane first; live lane can join after launch |
| Cost overrun | Medium | Medium | Daily budgets per provider; VOD lane pauses first |

## Compliance notes
- Credit: every clip shows "🎥 @creator" and names the platform in the caption.
- Takedowns: handled automatically the same day; creator blocked permanently.
- Privacy: no personal data stored beyond public channel info and public chat messages needed for detection (chat files deleted after 7 days; samples of 40 messages kept per spike for audit).
- Terms of service: platform posting via an approved third-party client (Upload-Post). Chat reading uses public anonymous access. Downloading streams for clipping depends on each creator's permission; the tier system and takedown handling are there because permission is not guaranteed for `auto` creators.
- This document is not legal advice.
