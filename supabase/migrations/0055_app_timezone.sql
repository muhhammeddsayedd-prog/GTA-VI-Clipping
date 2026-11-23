-- App time zone (owner's request, 2026-11-21). Additive except for one retired setting.
--
-- app.timezone  the IANA zone every time shown to people uses: the admin console, Telegram messages and
--               the daily report (whose day now runs midnight to midnight in this zone; n8n WF06 asks
--               every hour and the API sends each local day once). Storage, caps, budgets, hourly
--               slots (platform_accounts.hourly_hours_utc), posting.reserve_release_hour_utc and the
--               API stay UTC. The YouTube quota day keeps discovery.youtube_quota_timezone.
--
-- reports.timezone is replaced by app.timezone and removed. Code that still reads it (an older deploy
-- running while this applies) falls back to its built-in "UTC".

insert into settings (key, value, description) values
  ('app.timezone', '"Africa/Cairo"'::jsonb,
   'IANA time zone (e.g. Africa/Cairo, Europe/London, UTC) for every time shown in the console, Telegram and the daily report, and for the daily report''s day. Limits, caps, budgets and stored times stay UTC.')
on conflict (key) do nothing;

delete from settings where key = 'reports.timezone';
