-- 0026: scoring overhaul, step 1 (docs/CHANGELOG-scoring.md, docs/taste-profile.md).
--
-- Additive only: two new tables for the flags and their history, one decision log, and a
-- `scoring_version` column on spikes and clips. Nothing is dropped, renamed or deleted.
--
-- Every new scoring behaviour reads its flag from `scoring_config`; every flag starts disabled, so this
-- migration changes no behaviour. Rolling back a change = setting its `enabled` to false (no redeploy).
-- Each change to `scoring_config` writes a row to `scoring_config_history`; the newest history `version`
-- is the scoring config version stamped on every spike, clip and `scoring_log` row.

create table if not exists scoring_config (
  key text primary key,
  value jsonb not null,
  description text not null default '',
  updated_at timestamptz not null default now()
);

create table if not exists scoring_config_history (
  version bigserial primary key,
  key text not null,
  old_value jsonb,
  new_value jsonb,
  changed_at timestamptz not null default now(),
  changed_by text not null default current_user
);

create or replace function scoring_config_track() returns trigger
language plpgsql as $$
begin
  if tg_op = 'DELETE' then
    insert into scoring_config_history (key, old_value, new_value) values (old.key, old.value, null);
    return old;
  end if;
  if tg_op = 'UPDATE' and new.value is not distinct from old.value then
    return new;
  end if;
  new.updated_at := now();
  insert into scoring_config_history (key, old_value, new_value)
  values (new.key, case when tg_op = 'UPDATE' then old.value end, new.value);
  return new;
end;
$$;

drop trigger if exists scoring_config_track on scoring_config;
create trigger scoring_config_track
  before insert or update or delete on scoring_config
  for each row execute function scoring_config_track();

-- Why every spike and clip passed or failed: one row per decision stage with all sub-scores.
create table if not exists scoring_log (
  id bigserial primary key,
  created_at timestamptz not null default now(),
  stage text not null,
  config_version bigint not null,
  spike_id uuid,
  clip_id uuid,
  creator_id uuid,
  decision text,
  reason text,
  sub_scores jsonb not null default '{}'::jsonb
);
create index if not exists scoring_log_spike_idx on scoring_log (spike_id) where spike_id is not null;
create index if not exists scoring_log_clip_idx on scoring_log (clip_id) where clip_id is not null;
create index if not exists scoring_log_stage_time_idx on scoring_log (stage, created_at desc);
create index if not exists scoring_log_creator_idx on scoring_log (creator_id, stage, created_at desc)
  where creator_id is not null;

alter table spikes add column if not exists scoring_version bigint;
alter table clips add column if not exists scoring_version bigint;

-- Per-channel percentiles read the creator's recent spikes.
create index if not exists spikes_creator_detected_idx on spikes (creator_id, detected_at desc);

-- Same access model as every other table: the service role only (RLS on, no policies).
alter table scoring_config enable row level security;
alter table scoring_config_history enable row level security;
alter table scoring_log enable row level security;

insert into scoring_config (key, value, description) values
  ('percentile.history',
   '{"lookback_days": 14, "min_history": 20, "max_rows": 500}'::jsonb,
   'How a spike is ranked against its channel: score_z percentile among the creator''s spikes of the last lookback_days (at most max_rows). With fewer than min_history of them, all creators'' spikes of the same platform are used instead.'),
  ('warmup.v2',
   '{"enabled": false, "warmup_s": 120, "prior_sessions": 5, "save_every_s": 300}'::jsonb,
   'Phase 1. Replaces spikes.warmup_s (900) with warmup_s, and uses the median of the creator''s last prior_sessions stored live chat baselines while a stream has too little chat history of its own. Live sessions save their baseline every save_every_s.'),
  ('cut.adaptive',
   '{"enabled": false, "min_s": 6, "max_s": 25, "target_min_s": 12, "target_max_s": 18, "lead_s": [1, 3], "tail_s": [1, 3], "anchor": true}'::jsonb,
   'Phase 1. The clip writer cuts between min_s and max_s (target_min_s..target_max_s by default; max_s only for a story with a setup and payoff), starting lead_s before the setup line and ending tail_s after the reaction fades, anchored on the loudest point of voice, chat and transcript. The gate minimum length follows min_s.'),
  ('fastpath.live',
   '{"enabled": false, "percentile": 97, "include_s_tier": true, "keep_reject_terms": ["giveaway", "command", "raid", "subscri", "gifted", "gift sub", "bits", "donation", "sponsor", "technical", "brb", "ad break"]}'::jsonb,
   'Phase 1. A live spike in the top (100 - percentile)% of its channel, or of provisional tier S, is not rejected by the chat-only P1 check and goes to the express render as tier S. A P1 reject whose reason contains one of keep_reject_terms (non-content spikes) still stands.'),
  ('boost.first_time',
   '{"enabled": false, "score_points": 1.0, "rank_points": 1.0, "phrases": ["first time", "first ever", "finally", "for the first time", "my first", "his first", "her first"], "event_types": {"car_theft": ["steal", "stole", "stealing", "carjack", "jacked", "grand theft"], "wanted_level": ["wanted level", "five stars", "six stars", "5 stars", "6 stars", "wanted"], "death": ["wasted", "i died", "we died", "he died", "she died", "killed me"], "police_chase": ["police chase", "cop chase", "the cops", "police", "chase"], "first_gun": ["gun", "pistol", "rifle", "shotgun", "glock", "ak"]}}'::jsonb,
   'Phase 1. Adds score_points to the gate score and rank_points to the posting rank when the transcript or chat contains a phrase, or the transcript/topic shows an event type this creator has no approved clip of yet.'),
  ('pass.strong_spike',
   '{"enabled": false, "percentile": 90, "min_score": 5}'::jsonb,
   'Phase 1. A clip whose spike is in the top (100 - percentile)% of its channel passes the hourly profile at min_score instead of gate.hourly.min_score.'),
  ('merge.keep_stronger',
   '{"enabled": false}'::jsonb,
   'Phase 1. Live: a later spike within spikes.merge_gap_s that is stronger than the one already emitted is clipped too, and the earlier one is cancelled when its render has not started (instead of dropping the stronger one).'),
  ('reactions.extended',
   '{"enabled": false, "extra_tokens": ["ICANT", "LULW", "OMEGALUL", "pepeLaugh", "KEKW*", "KEKL", "HAHA*", "AHAHA*", "XD", "???", "W", "L"], "burst_min": 3, "youtube_emoji": true}'::jsonb,
   'Phase 1. Adds extra_tokens to spikes.reaction_tokens; a message repeating one token burst_min or more times counts as a reaction; with youtube_emoji a YouTube custom emoji (:name:) counts as a reaction.')
on conflict (key) do nothing;
