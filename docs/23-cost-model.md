# 23 — Cost Model (estimates, monthly, USD)

All numbers are estimates for planning. Check current prices when signing up.

| Item | Assumption | Estimate |
|---|---|---|
| Hetzner production | 16 dedicated vCPU, 32–64 GB | $60–150 |
| Hetzner staging | 4 vCPU / 8 GB | $8–15 |
| Supabase | Pro org $25 + second project compute + storage/egress (clips deleted after 3 days) | $35–60 |
| Upload-Post | plan with ~2,000 uploads/month and 3 profiles | $30–80 |
| Anthropic | ~100 rendered clips/day × P2 (~6k in/700 out tokens, Sonnet) + ~300 P1 calls/day (Haiku) + vision calls | $150–350 |
| Transcription (hosted) | express only, ~30 clips/day × 1.5 min | $5–15 |
| Apify | discovery runs every 10 min, ~50 results per term | $20–50 |
| Residential proxy | 10–30 GB/month (window downloads + live streams through proxy only when needed) | $30–120 |
| n8n | existing VPS | $0 extra |
| **Total** | | **≈ $340–840/month** |

Biggest levers: Anthropic (rendered clips per day, model choice for P2), proxy bandwidth (only route YouTube downloads through the proxy; Twitch usually works direct — make proxy use a per-platform setting), and Apify frequency.

Revenue: YouTube Shorts revenue share requires YPP (1,000 subscribers + 10M Shorts views in 90 days, or 4,000 watch hours). Revenue per 1,000 Shorts views depends heavily on where viewers are; global English audiences pay more than low-cost regions. The daily report tracks YPP progress.
