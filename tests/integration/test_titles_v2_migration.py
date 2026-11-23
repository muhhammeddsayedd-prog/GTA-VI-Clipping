"""Migration 0040 (title writer v2): idempotent, flag disabled, names seeded only where empty."""

from pathlib import Path

import psycopg

from sixstars import db

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "supabase" / "migrations" / "0040_titles_v2.sql"


def test_migration_is_idempotent_and_keeps_an_operator_name(test_database_url: str) -> None:
    db.execute(
        "insert into creators (platform, channel_id, handle, display_name) values "
        "('twitch', 't-kai', 'kaicenat', 'KaiCenat'), ('twitch', 't-jerma', 'jerma985', 'Jerma985') "
        "on conflict (platform, channel_id) do nothing"
    )
    db.execute("update creators set title_name = 'Jerma The Great' where handle = 'jerma985'")
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute(MIGRATION.read_bytes())
        conn.execute(MIGRATION.read_bytes())

    kai = db.fetch_one("select title_name, title_aliases from creators where handle = 'kaicenat'")
    assert kai is not None and kai["title_name"] == "Kai Cenat" and "kai cenat" in kai["title_aliases"]
    jerma = db.fetch_one("select title_name from creators where handle = 'jerma985'")
    assert jerma is not None and jerma["title_name"] == "Jerma The Great"

    flag = db.fetch_one("select value from scoring_config where key = 'titles.v2'")
    assert flag is not None and flag["value"]["enabled"] is False
    assert db.fetch_one("select count(*) as n from clip_titles") == {"n": 0}
    db.execute("delete from creators where channel_id in ('t-kai', 't-jerma')")
