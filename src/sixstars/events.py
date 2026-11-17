"""Writer for the `events` table. Every important state change in the system (spike found, clip rendered,
gate verdict, post published, takedown handled) records a row here for audit and reporting. Implemented in
M01.
"""

from collections.abc import Mapping
from typing import Any, Literal

from psycopg.types.json import Jsonb

from sixstars import db
from sixstars.config import env

Level = Literal["debug", "info", "warn", "error", "critical"]


def emit(
    type: str,  # noqa: A002
    level: Level = "info",
    entity_type: str | None = None,
    entity_id: str | int | None = None,
    message: str | None = None,
    data: Mapping[str, Any] | None = None,
    service: str | None = None,
) -> int:
    """Insert one row into `events` and return its id.

    `type` is a dotted name such as `spike.detected` or `post.published`. `entity_type` and `entity_id`
    point at the row the event is about (for example `clip` and the clip id). `service` defaults to
    `SERVICE_NAME`.
    """
    row = db.fetch_one(
        """
        insert into events (type, level, service, entity_type, entity_id, message, data)
        values (%s, %s, %s, %s, %s, %s, %s)
        returning id
        """,
        (
            type,
            level,
            service or env().SERVICE_NAME,
            entity_type,
            str(entity_id) if entity_id is not None else None,
            message,
            Jsonb(dict(data)) if data is not None else None,
        ),
    )
    assert row is not None
    return int(row["id"])
