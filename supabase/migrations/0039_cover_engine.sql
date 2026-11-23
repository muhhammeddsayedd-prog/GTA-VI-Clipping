-- Cover engine C2 (2026-11-23): the covers table, the cover.* config keys and the two Storage buckets.
-- Spec docs/29a sections 6-8. Additive and idempotent. Every switch starts OFF in every environment
-- (cover.enabled false); staging turns them on by hand, production only through the docs/29a section 10
-- gate, one step at a time. Every change to a key is versioned by scoring_config_history; each covers row
-- carries the version it was made with.

-- 1. One row per cover attempt for one clip render on one platform account.
create table if not exists covers (
  id              uuid primary key default gen_random_uuid(),
  clip_id         uuid not null references clips(id) on delete cascade,
  render_id       uuid references clip_renders(id) on delete set null,   -- the style render the cover is for
  post_id         uuid references posts(id) on delete set null,          -- filled when posted
  account_id      text references platform_accounts(id),
  lane            text not null,                                          -- lane key: normal | spike | campaign:<slug>
  mode            text not null default 'full' check (mode in ('full', 'frame_only')),
  platform        text not null check (platform in ('youtube', 'instagram', 'tiktok')),
  arm             text not null,                                          -- e.g. 'split|quote'
  layout          text check (layout in ('split', 'face_led', 'moment_led', 'tiktok_native', 'plain')),
  line_text       text,
  line_style      text check (line_style in ('quote', 'label', 'none')),
  key_word        text,
  frame_ts_ms     int,                                                    -- clean-layer frame used
  rendered_ts_ms  int,                                                    -- rendered frame (TikTok timestamp, YouTube check)
  scores          jsonb not null default '{}'::jsonb,                     -- face, expression, sharpness, exposure, gameplay, ocr, vision, phash_min_dist
  phash           text,
  storage_path    text,                                                   -- covers-work/<clip_id>/...
  public_url      text,                                                   -- covers-public (Instagram cover_url) only
  status          text not null default 'draft' check (status in (
                    'draft', 'passed', 'failed', 'applied', 'apply_failed',
                    'skipped_deadline', 'fallback_plain', 'fallback_default')),
  attempt         int not null default 1 check (attempt >= 1),
  error           text,
  config_version  int not null,
  created_at      timestamptz not null default now(),
  applied_at      timestamptz
);
create index if not exists idx_covers_platform_created on covers (platform, created_at desc);
create index if not exists idx_covers_clip on covers (clip_id);
-- The diversity rule and the console grid read an account's latest covers.
create index if not exists idx_covers_account_created on covers (account_id, created_at desc);
alter table covers enable row level security;

-- 2. The config keys (docs/29a section 8). Defaults are the staging values except the master switch.
insert into scoring_config (key, value, description) values
  ('cover.enabled', 'false'::jsonb,
   'Cover engine master switch. Off: no cover work at all, every post goes out as today.'),
  ('cover.platforms', '["instagram", "tiktok", "youtube"]'::jsonb,
   'Platforms the cover engine runs for (production rollout: instagram first, then tiktok).'),
  ('cover.youtube_custom_enabled', 'false'::jsonb,
   'Send the composed cover to YouTube (thumbnail_url, then the set-thumbnail endpoint). Turned off by itself on a 403.'),
  ('cover.tiktok_image_enabled', 'false'::jsonb,
   'TikTok: send tiktok_cover_image_url instead of cover_timestamp. Off until the C7 test and the owner''s yes.'),
  ('cover.lane_modes', '{"normal": "full", "spike": "full", "*": "frame_only"}'::jsonb,
   'Per lane key (normal, spike, campaign:<slug>, * for the rest): full (line + mark), frame_only (best real frame, no line, no mark) or off. A campaign lane goes beyond frame_only only after the owner checked its brief.'),
  ('cover.brand_mark', '{"normal": true, "spike": true, "*": false}'::jsonb,
   'Per lane key: draw the small VI mark (assets/covers/brand/vi-mark.png).'),
  ('cover.weights', '{"expression": 0.35, "face_area": 0.20, "sharpness": 0.15, "exposure": 0.10, "gameplay": 0.20}'::jsonb,
   'Frame score weights (stage 2): expression, face area, sharpness, exposure, gameplay.'),
  ('cover.qa_pass_score', '7'::jsonb,
   'Vision QA pass threshold (0-10). A cover passes with every rule check, this total and no hard fail.'),
  ('cover.deadline_s', '{"live": 60, "normal": 300}'::jsonb,
   'Seconds the cover stage may take before the post goes out without it: live = spike lane, normal = everything else.'),
  ('cover.upscale_enabled', 'false'::jsonb,
   'Real-ESRGAN general 2x upscale of a facecam crop under 400 px tall (never a face model).'),
  ('cover.exploration', '0.2'::jsonb,
   'Bandit exploration share (stage 7), never below 0.2.')
on conflict (key) do nothing;

-- 3. Storage: work files stay private; covers-public serves the composed JPEG for Instagram's cover_url only.
insert into storage.buckets (id, name, public) values ('covers-work', 'covers-work', false)
on conflict (id) do nothing;
insert into storage.buckets (id, name, public) values ('covers-public', 'covers-public', true)
on conflict (id) do nothing;
