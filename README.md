# 6 Stars Clips — Development Kit

Version 1.0 · 16 November 2026

Everything needed to build **6 Stars Clips**: a fully automated system that finds the most-trending GTA 6 creators on Twitch and YouTube, detects the best moments from live-chat spikes, turns them into branded vertical clips, and posts them to TikTok, Instagram Reels and YouTube Shorts around the clock with no human interaction.

**Launch: Wednesday 18 November 2026, 11:00 UTC.**

---

## How to use this kit

1. Create the GitHub repo locally and copy **everything in this kit** into the repo root.
2. Open the repo in VS Code with Claude Code.
3. Read `docs/00-decisions.md` and `docs/01-prd.md` once yourself (10 minutes).
4. Do the manual setup in `docs/22-account-setup-guide.md` (accounts, keys, servers). Claude Code cannot create social accounts or sign up for services for you.
5. Tell Claude Code: *"Read CLAUDE.md, then start milestone M00 in development-plan/."*
6. Work milestone by milestone, in order. Each milestone file is self-contained and ends with a definition of done. Don't start the next milestone until the current one passes its acceptance tests.

## What's in the kit

```
6stars-clips-kit/
├── README.md                     ← you are here
├── CLAUDE.md                     ← standing instructions for Claude Code (goes in repo root)
├── docs/
│   ├── 00-decisions.md           ← every decision made so far, in one place
│   ├── 01-prd.md                 ← product requirements
│   ├── 02-architecture.md        ← system architecture and data flow
│   ├── 03-repo-and-stack.md      ← tech stack, repo layout, coding standards
│   ├── 04-database.md            ← schema explained table by table
│   ├── 05-chat-spike-engine.md   ← the core moment detector
│   ├── 06-discovery.md           ← finding trending creators
│   ├── 07-live-lane.md           ← live recording, live spikes, express path
│   ├── 08-vod-lane.md            ← chat-first processing of finished streams
│   ├── 09-clip-factory.md        ← rendering, layouts, subtitles, bleeping
│   ├── 10-quality-gate.md        ← automatic approval rules
│   ├── 11-posting-engine.md      ← hourly lane, spike lane, cap manager
│   ├── 12-guardian.md            ← claims, takedowns, health, emergency stop
│   ├── 13-n8n-workflows.md       ← every n8n workflow, node by node
│   ├── 14-api-integrations.md    ← every external API, with endpoints and gotchas
│   ├── 15-claude-prompts.md      ← every Claude prompt and its JSON schema
│   ├── 16-settings-reference.md  ← every runtime setting and its default
│   ├── 17-control-api.md         ← the worker's internal HTTP API
│   ├── 18-environments-and-deployment.md
│   ├── 19-testing-plan.md
│   ├── 20-launch-runbook.md
│   ├── 21-monitoring-and-operations.md
│   ├── 22-account-setup-guide.md ← manual steps for you
│   ├── 23-cost-model.md
│   ├── 24-risks-and-compliance.md
│   └── 25-later-phases.md
├── supabase/
│   └── migrations/  ← applied with the Supabase CLI
│       ├── 0001_init.sql         ← full schema
│       └── 0002_seed_settings.sql← default settings (production values)
├── config/
│   └── .env.example            ← copy to .env on each server
├── assets/
│   └── profanity.txt            ← bleep list
├── supabase/seed/                ← production.sql and staging.sql (accounts + overrides)
├── n8n/
│   └── README.md                 ← where exported workflow JSON files go
└── development-plan/
    ├── 00-overview.md            ← milestone map, order, timeline
    └── M00-… to M16-…            ← one self-contained file per milestone
```

## The system in one picture

```
 DISCOVERY (every 10 min) ─► finds trending GTA streams & videos on Twitch + YouTube
        │
        ├─► LIVE LANE: record stream + read chat live ─┐
        │                                              ├─► CHAT-SPIKE ENGINE (10 s buckets,
        └─► VOD LANE: download chat replay first ──────┘    start clip 20 s before spike)
                                                                   │
                         ┌─────────────────────────────────────────┤
                    S-tier live spike                       A/B spikes, VOD spikes
                         │                                         │
                  EXPRESS PATH (<4 min)                    CLIP FACTORY + QUALITY GATE
                         │                                         │
                  SPIKE POSTING LANE  (priority 1)       HOURLY POSTING LANE (priority 2)
                         └──────────────► CAP MANAGER ◄────────────┘
                                              │
                          TikTok (2 accounts) · Instagram · YouTube
                                              │
                         GUARDIAN (claims, takedowns, health) ─► Telegram
```
