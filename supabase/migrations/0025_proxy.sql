-- Residential proxy for YouTube video downloads (DataImpulse, owner's request 2026-11-18). Additive and
-- idempotent: settings only. See src/sixstars/vod/proxy.py and docs/16-settings-reference.md "Proxy".
--
-- Numbered 0025: 0023 is held by the spike-hold work on develop, 0024 by the YouTube quota work.
--
-- The provider gives every port of PROXY_STICKY_PORT_MIN..MAX its own exit IP for ~30 min. A video job
-- uses one random port for all its requests and retries a refusal on a new one. Traffic is billed per GB,
-- so only the VOD lane uses the proxy, the format is capped at downloads.max_height, only spike windows are
-- downloaded, and the bytes of every job go to api_usage (provider 'DataImpulse').

insert into settings (key, value, description) values
  ('proxy.lanes', '["vod"]'::jsonb,
   'Lanes that may use PROXY_URL (for the platforms in downloads.proxy_platforms): vod, live, discovery, chat. The proxy is paid per GB; live recording costs about 2.5 GB per stream-hour.'),
  ('proxy.max_attempts', '3'::jsonb,
   'Attempts of one YouTube video job through the proxy, each on a new random sticky port, after a bot check, a 403 on every window or a proxy error. Then the video waits (vod.blocked_backoff_s).'),
  ('proxy.cookies_mode', '"fallback"'::jsonb,
   'When YouTube cookies are sent through the proxy: fallback (one more attempt with cookies after proxy.max_attempts without), first (every attempt with cookies) or never.'),
  ('proxy.daily_budget_usd', '3'::jsonb,
   'USD of proxy traffic per UTC day. When reached, YouTube video jobs wait for the next day and the guardian alerts. 0 = no limit.'),
  ('proxy.usd_per_gb', '1.0'::jsonb,
   'Price of one GB of proxy traffic (DataImpulse residential, pay as you go).'),
  ('proxy.overhead_mb_per_call', '2.0'::jsonb,
   'Estimated MB per yt-dlp call through the proxy on top of the measured media bytes (watch page, manifests, player). Calibrate against the provider dashboard.'),
  ('proxy.cycle_gb', '5'::jsonb,
   'GB bought for the current billing cycle. After a top-up set it to the new total and proxy.cycle_started_at to now.'),
  ('proxy.cycle_started_at', '"2026-11-18T00:00:00+00:00"'::jsonb,
   'Start of the current billing cycle; traffic recorded since then counts against proxy.cycle_gb.'),
  ('proxy.low_traffic_share', '0.8'::jsonb,
   'Share of proxy.cycle_gb used (our own count) at which the guardian asks for a top-up.'),
  ('proxy.health_interval_s', '900'::jsonb,
   'Seconds between two guardian checks that the proxy answers (one tiny request each).')
on conflict (key) do nothing;
