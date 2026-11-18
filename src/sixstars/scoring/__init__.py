"""Scoring overhaul (docs/taste-profile.md, docs/CHANGELOG-scoring.md): feature flags in `scoring_config`,
the decision log `scoring_log`, per-channel spike percentiles and the "first time" boost.

Every behaviour added by the overhaul is off unless its flag in `scoring_config` has `"enabled": true`, so
rolling a change back is one `update scoring_config` and never a redeploy.
"""
