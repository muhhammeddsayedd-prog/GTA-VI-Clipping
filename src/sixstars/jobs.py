"""Postgres job queue on the `jobs` table: enqueue, claim (`FOR UPDATE SKIP LOCKED` via `claim_job()`),
complete, fail and retry. This is the only mechanism for background work; there is no Celery or Redis.
Implemented in M01.
"""
