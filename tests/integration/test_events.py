"""Integration tests for `sixstars.events`."""

from sixstars import db, events


def test_emit_minimal() -> None:
    event_id = events.emit("test.happened")
    row = db.fetch_one("select * from events where id = %s", (event_id,))
    assert row is not None
    assert row["type"] == "test.happened"
    assert row["level"] == "info"
    assert row["service"] == "sixstars"
    assert row["data"] is None
    assert row["created_at"].utcoffset() is not None


def test_emit_full() -> None:
    event_id = events.emit(
        "post.published",
        level="warn",
        entity_type="clip",
        entity_id=42,
        message="hello",
        data={"account": "tiktok_main", "n": 1},
        service="posting",
    )
    row = db.fetch_one("select * from events where id = %s", (event_id,))
    assert row is not None
    assert (row["level"], row["entity_type"], row["entity_id"]) == ("warn", "clip", "42")
    assert row["message"] == "hello"
    assert row["data"] == {"account": "tiktok_main", "n": 1}
    assert row["service"] == "posting"
