"""Fixtures for integration tests. They run against a throwaway Postgres named by `TEST_DATABASE_URL`
(local Docker or the CI service container), never against Supabase. When the variable is unset the
tests are skipped. Start a local database with:

    docker run -d --name sixstars-test-pg -e POSTGRES_PASSWORD=postgres -p 54329:5432 postgres:17
    TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54329/postgres uv run pytest

If the schema is missing, the session fixture creates the `storage.buckets` stub (plain Postgres has no
Supabase storage schema) and applies `supabase/migrations/*.sql` in order.
"""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from sixstars import config, db, settings

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "supabase" / "migrations"
STORAGE_STUB = """
create schema if not exists storage;
create table if not exists storage.buckets (id text primary key, name text, public bool);
"""
LAST_BOOTSTRAP_MIGRATION = "0002"
FORBIDDEN_HOST_PARTS = ("supabase.co", "supabase.com", "supabase.net")
MUTABLE_TABLES = "jobs, events, notify_outbox, heartbeats, api_usage"


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set: integration tests need a throwaway Postgres")
    if any(part in url for part in FORBIDDEN_HOST_PARTS):
        pytest.fail("TEST_DATABASE_URL points at Supabase: integration tests must use a throwaway database")
    return url


@pytest.fixture(scope="session")
def settings_snapshot(test_database_url: str) -> list[tuple[Any, ...]]:
    """Apply the migrations if needed and return the seeded `settings` rows."""
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        exists = conn.execute("select to_regclass('public.settings')").fetchone()
        fresh = exists is None or exists[0] is None
        if fresh:
            conn.execute(STORAGE_STUB)
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            # 0001 and 0002 create and seed the schema once. Every later migration must be idempotent
            # and is applied on each run, so a database migrated by an older checkout catches up.
            if fresh or path.name[:4] > LAST_BOOTSTRAP_MIGRATION:
                conn.execute(path.read_bytes())
        return conn.execute("select key, value, description from settings").fetchall()


@pytest.fixture(autouse=True)
def database(
    clean_env: None,
    test_database_url: str,
    settings_snapshot: list[tuple[Any, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Point the package at the test database and leave it clean for the next test."""
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    monkeypatch.setenv("APP_ENV", "test")
    config.env.cache_clear()
    db.close_pool()
    settings.invalidate()
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute(f"truncate {MUTABLE_TABLES} restart identity")
    yield
    db.close_pool()
    settings.invalidate()
    with psycopg.connect(test_database_url) as conn:
        conn.execute("delete from settings")
        with conn.cursor() as cur:
            cur.executemany(
                "insert into settings (key, value, description) values (%s, %s, %s)",
                [(key, Jsonb(value), description) for key, value, description in settings_snapshot],
            )
