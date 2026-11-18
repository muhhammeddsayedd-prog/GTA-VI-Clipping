-- Multiple design styles, one per platform account (development-plan/M18 Phase A, decisions D112-D114).
-- Idempotent.
--
-- 1. factory.styles: the catalogue of styles. Phase A has two, both with the facecam on top (layout C,
--    D112): cam_top_viral (the docs/27 look: 2-word captions, punch-ins, zoom cuts, sound effects) and
--    cam_top_title (a title that stays for the whole clip and no spoken captions, like the owner's
--    references R02-R04/R07). Without this setting the factory keeps the single factory.style.
-- 2. factory.style_director: how the style of each account is picked.
-- 3. clips: style_id and layout_kind of the first (reference) render, style_plan (the style of every
--    account, the director's reasons).
-- 4. clip_renders: one row per style rendered for a clip; the reference render plus one per other style
--    in the plan (job clip.variants). posts.render_id: the file that was posted.

insert into settings (key, value, description) values
  ('factory.styles',
   '{
      "cam_top_viral": {"enabled": true, "weight": 1, "lanes": ["spike", "hourly"], "accounts": "*",
        "rules": {"facecam_top": true, "caption_words": 2, "caption_chars": 14, "caption_font": "Anton", "caption_size": 150, "caption_caps": true, "hook_mode": "title_bar", "punchin_max": 4, "punchin_zoom": 1.25, "punchin_len_s": 1.2, "punchin_min_gap_s": 2.5, "punchin_db": 5, "zoomcut_every_s": 2.5, "zoomcut_scale": 1.08, "sfx_gain_db": -10, "cut_sfx": true, "payoff_fx": true, "compress": true}},
      "cam_top_title": {"enabled": true, "weight": 1, "lanes": ["spike", "hourly"], "accounts": "*",
        "rules": {"facecam_top": true, "captions": false, "caption_words": 0, "hook_mode": "title_bar", "title_whole_clip": true, "punchin_max": 0, "zoomcut_every_s": 0, "cut_sfx": false, "payoff_fx": false, "compress": true, "sfx_gain_db": -10}}
    }'::jsonb,
   'Design styles (development-plan/M18, docs/27 section 5). Per style: enabled, weight (share in the weighted pick), lanes (spike/hourly), accounts ("*" or a list of platform_accounts ids) and rules (the factory.style keys plus facecam_top: facecam on top (layout C); captions: false for a title-only style; title_whole_clip: the title bar stays for the whole clip). Missing: the single factory.style for every clip.'),
  ('factory.style_director',
   '{"no_repeat_n": 1, "diverse_across_accounts": true}'::jsonb,
   'How the style of each platform account is picked (development-plan/M18). no_repeat_n: skip the styles of the account''s last n clips when another is allowed. diverse_across_accounts: give the accounts of one clip different styles when possible.')
on conflict (key) do nothing;

alter table clips add column if not exists style_id text;
alter table clips add column if not exists layout_kind text;
alter table clips add column if not exists style_plan jsonb;

create table if not exists clip_renders (
  id               uuid primary key default gen_random_uuid(),
  clip_id          uuid not null references clips(id) on delete cascade,
  style_id         text not null,
  layout_kind      text,
  is_reference     boolean not null default false,
  status           text not null default 'pending'
                   check (status in ('pending', 'ready', 'failed', 'skipped')),
  local_path       text,
  storage_path     text,
  file_size_bytes  bigint,
  duration_s       numeric,
  render_ms        int,
  checks           jsonb,
  error            text,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  unique (clip_id, style_id)
);
create index if not exists idx_clip_renders_clip on clip_renders (clip_id);

drop trigger if exists trg_clip_renders_updated on clip_renders;
create trigger trg_clip_renders_updated before update on clip_renders
  for each row execute function set_updated_at();

alter table clip_renders enable row level security;

alter table posts add column if not exists render_id uuid references clip_renders(id);
