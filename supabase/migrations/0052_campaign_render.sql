-- Clipping campaigns PR 3 (2026-11-23): render profiles and brief_match (D118). Additive and idempotent.
-- A profile's logo PNG lives in the row (render_profiles.logo_png, logo_path = 'db:<sha256>'), written to a
-- cache file when a render needs it. clips.brief_result keeps the brief_match outcome of a campaign clip
-- (a clip that fails its brief is made as a Normal clip in the brand look, never dropped).
alter table render_profiles add column if not exists logo_png bytea;
alter table clips add column if not exists brief_result jsonb;
