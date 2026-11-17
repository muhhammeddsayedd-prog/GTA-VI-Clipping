-- Twitch Clips as a discovery source (decision D110). Additive and idempotent: one new table, new
-- settings; existing rows and values are never changed.
--
-- Chat-spike detection on long streams approved 0 of about 45 rendered clips. Twitch viewers already clip
-- the best moments, and Helix lists those clips per game with view counts (Get Clips). Each discovery
-- pass reads the top clips of env.target_game, keeps the ones that fit, and queues a
-- viewer_clip.process job per clip: download the short MP4, render it through the clip factory on the
-- hourly lane, and let the quality gate judge it like every other clip. The on-screen credit names the
-- broadcaster, never the viewer who made the clip.
--
-- Why a table of its own and not rows in `videos`: every row in `videos` belongs to the VOD lane. Rows
-- with status new count against discovery.max_vod_backlog, and a row left in processing is picked up by
-- the lane's recovery, which queues a vod.process job for it (chat replay, spike scan). A viewer clip in
-- that table would be scanned as if it were a long stream.
--
-- clips.id of the rendered clip equals viewer_clips.id, so a retried job finds the same clip row.
-- vod_id / vod_offset_s are the broadcast's video and the clip's start in it, when Twitch has them: the
-- clip row is then stored under that video (clips.source_id = vod_id, start_s / end_s on the video's
-- clock), which is what the gate's source-range duplicate check compares.

create table if not exists viewer_clips (
  id               uuid primary key default gen_random_uuid(),
  platform         text not null default 'twitch' check (platform in ('twitch')),
  source_id        text not null,                 -- Twitch clip id (slug)
  creator_id       uuid not null references creators(id),   -- the broadcaster
  url              text not null,
  title            text,
  clipped_by       text,                          -- the viewer who made the clip; never credited
  language         text,
  view_count       bigint,                        -- when discovery saw it
  duration_s       numeric,
  clip_created_at  timestamptz,
  vod_id           text,                          -- broadcast video id, null when Twitch has none
  vod_offset_s     numeric,                       -- start of the clip in that video
  rank_score       numeric,
  rank_parts       jsonb,
  status           text not null default 'new'
                   check (status in ('new','processing','done','skipped','failed')),
  skip_reason      text,
  attempts         int not null default 0,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  unique (platform, source_id)
);
create index if not exists idx_viewer_clips_status on viewer_clips (status, created_at);
create index if not exists idx_viewer_clips_vod on viewer_clips (vod_id) where vod_id is not null;
create index if not exists idx_viewer_clips_creator on viewer_clips (creator_id, created_at desc);
drop trigger if exists trg_viewer_clips_updated on viewer_clips;
create trigger trg_viewer_clips_updated before update on viewer_clips
  for each row execute function set_updated_at();
alter table viewer_clips enable row level security;

insert into settings (key, value, description) values
  ('twitch_clips.enabled', 'true'::jsonb,
   'Viewer-made Twitch Clips of env.target_game are a discovery source (D110). false: the source does nothing. Needs the Twitch keys; without them it does nothing either.'),
  ('twitch_clips.lookback_h', '24'::jsonb,
   'Each pass reads the most viewed clips made in the last this many hours.'),
  ('twitch_clips.read_per_pass', '100'::jsonb,
   'Clips read from Twitch per pass, most viewed first (100 per request; one request costs one rate-limit point).'),
  ('twitch_clips.max_queued_per_pass', '3'::jsonb,
   'Clips queued for rendering per discovery pass, at most.'),
  ('twitch_clips.max_per_creator_per_pass', '1'::jsonb,
   'Clips of one streamer queued per discovery pass, at most, so one streamer cannot fill the queue.'),
  ('twitch_clips.min_views', '10'::jsonb,
   'A clip with fewer views on Twitch than this is not queued. Measured 2026-11-17 over 24 hours: GTA V top clips 1,749 / 1,527 / 1,164 views, GTA VI (before release) top 387 with a thin tail; see the notes file for the counts at each threshold.'),
  ('twitch_clips.daily_cap', '48'::jsonb,
   'Clips queued from this source in any 24 hours, at most (two per hourly posting slot).'),
  ('twitch_clips.max_backlog', '6'::jsonb,
   'No more clips are queued while this many wait to be rendered (status new or processing).'),
  ('twitch_clips.min_age_min', '15'::jsonb,
   'A clip younger than this many minutes waits for a later pass: Twitch writes its position in the broadcast (vod_offset, needed for the duplicate check) some minutes after the clip is made, and the view count needs time to mean something.'),
  ('twitch_clips.rate_floor_h', '1'::jsonb,
   'Views per hour are computed over at least this many hours, so a clip a few minutes old does not outrank everything.'),
  ('twitch_clips.render_without_moment', 'false'::jsonb,
   'true: a viewer clip is rendered and judged by the gate even when the clip writer (P2), reading the transcript alone, says there is no moment in it. A writer reject with a safety flag or a spoiler always stands. false: the writer''s reject closes the clip before rendering, as on every other lane.')
on conflict (key) do nothing;
