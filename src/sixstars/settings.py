"""Runtime settings read from the Postgres `settings` table and cached for 30 seconds. Every threshold,
feature flag and limit that the docs mark as a setting is read through this module, never hard-coded.
Implemented in M01.
"""
