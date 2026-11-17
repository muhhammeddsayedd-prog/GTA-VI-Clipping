# 22 — Account Setup Guide (manual steps for the owner)

Claude Code cannot do these. Do them in this order; most take a few minutes. Keep every credential in a password manager.

## 1. Brand email and identity (15 min)
- [ ] Create one Gmail for the brand, e.g. `6starsclips@gmail.com` (or similar). Turn on 2-step verification.
- [ ] Use it for every account below. It's also the **takedown inbox** connected to n8n (WF04). Put it in every bio/"contact" field so takedown requests reach it.
- [ ] Make a simple profile picture (a gold 6-pointed star on dark background works) and a one-line bio: "The best GTA 6 moments, every hour ⭐ Clips credited to creators. Takedowns: <email>".

## 2. Social accounts (production) (45 min)
Create on your own phone and home connection, a few hours apart if possible; don't use a VPN; don't create them all back-to-back on the same device.

- [ ] **YouTube**: create a channel "6 Stars Clips", handle `@6StarsClips` (or closest available). **Verify the channel with your phone** (Studio → Settings → Channel → Feature eligibility). Set country of residence correctly (needed for YouTube Partner Program later).
- [ ] **Instagram**: `@6starsclips`, switch to a **Professional account** (Creator). Required for API posting.
- [ ] **TikTok main**: `@6starsclips`. Personal or business account both work for API posting; choose **personal/creator** (business accounts are limited to the commercial music library, which doesn't matter for us, but personal keeps options open).
- [ ] **TikTok live**: `@6starsclips.live` (or `@6starsclips_live`). Second account, created on a different day/device if possible.
- [ ] For each: profile picture, bio, follow 10–20 GTA accounts, watch and like GTA content for 10–15 minutes. Do not post anything manually.
- [ ] Turn on 2-step verification everywhere.

## 3. Staging test accounts (30 min)
- [ ] TikTok `@6stars.test` (set to private), Instagram `@6stars.test` (private, Professional), YouTube channel "6 Stars Test" (all uploads private). These only ever receive private posts.

## 4. Upload-Post (15 min)
- [ ] Sign up; choose a plan that allows **≥ 2,000 uploads/month** and **3 profiles** (check current plans; upgrade later if needed).
- [ ] Create profiles: `6stars-main` (connect TikTok @6starsclips, Instagram, YouTube), `6stars-live` (connect TikTok @6starsclips.live), `6stars-staging` (connect the test accounts).
- [ ] Copy the API key.

## 5. Developer keys (40 min)
- [ ] **Anthropic**: API key (you have it). Check your usage tier/rate limits in the console.
- [ ] **Twitch**: https://dev.twitch.tv/console → Register application (category "Other", redirect `http://localhost`) → Client ID + Client Secret.
- [ ] **Google Cloud** (one project per environment, or one shared): enable **YouTube Data API v3** → API key. OAuth consent screen: External, add your Google account; **publishing status "In production"** (accept the unverified-app warning for your own use). Create OAuth client (Desktop app) → client id/secret. Claude Code's M02 adds `sixstars youtube auth` to produce the refresh token for the production and test channels.
- [ ] **Groq**: API key (express transcription). **OpenAI**: API key (fallback).
- [ ] **Apify**: API token (you have the account).
- [ ] **Residential proxy**: any provider with sticky sessions and pay-as-you-go GB (e.g. 5–10 GB/month to start). Get the proxy URL.

## 6. Infrastructure (40 min)
- [ ] **Hetzner**: create staging server (4 vCPU / 8 GB) and production server (16 dedicated vCPU / 32–64 GB, ≥ 500 GB disk). Ubuntu 24.04, add your SSH key.
- [ ] **Domain** (optional): two subdomains `api-staging.` and `api.` pointing to the servers. Without a domain use `<ip>.sslip.io` hostnames.
- [ ] **Supabase**: rename the existing project to `6stars-production`; create `6stars-staging` in the same Pro org. Copy for each: project ref, DB password, session-pooler connection string, service role key, URL. Create a personal access token for CI.
- [ ] **GitHub**: in the empty repo, create branches `main` and `develop`; Settings → Environments `staging`, `production` (add yourself as required reviewer for production); add the secrets listed in `docs/18` §5. Enable GHCR packages.
- [ ] **n8n**: confirm the HTTPS URL; create the Telegram, Gmail and Header Auth credentials (`docs/13`).
- [ ] **Telegram**: create a bot with @BotFather, get the token; send it a message and get your chat id (n8n Telegram Trigger shows it).

## 7. Hand-off to Claude Code
Put all values into `/opt/6stars/.env` on each server (and a local `.env` for development, pointing at staging). Then start M00.
