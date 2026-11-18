"""`scoring_log`: one row per scoring decision with every sub-score and the config version that made it.

Writing the log must never stop the pipeline: a failed insert is logged and swallowed.
"""

from collections.abc import Mapping
from typing import Any

from psycopg.types.json import Jsonb

from sixstars import db
from sixstars.logging import get_logger
from sixstars.scoring import config

_log = get_logger("scoring")


def record(
    stage: str,
    *,
    decision: str | None = None,
    reason: str | None = None,
    sub_scores: Mapping[str, Any] | None = None,
    spike_id: str | None = None,
    clip_id: str | None = None,
    creator_id: str | None = None,
) -> int:
    """Insert one `scoring_log` row. Returns the config version recorded (0 when unknown)."""
    version = config.version()
    try:
        db.execute(
            """
            insert into scoring_log (stage, config_version, spike_id, clip_id, creator_id, decision, reason,
                                     sub_scores)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                stage,
                version,
                spike_id,
                clip_id,
                creator_id,
                decision,
                (reason or "")[:500] or None,
                Jsonb(dict(sub_scores or {})),
            ),
        )
    except Exception as exc:  # noqa: BLE001 - the log is for humans; it never blocks a decision
        _log.warning("scoring_log_failed", stage=stage, error=f"{type(exc).__name__}: {exc}"[:200])
    return version


def stamp(table: str, row_id: str) -> None:
    """Write the current config version on a `spikes` or `clips` row (`scoring_version`)."""
    if table not in ("spikes", "clips"):
        raise ValueError(f"cannot stamp {table}")
    try:
        db.execute(f"update {table} set scoring_version = %s where id = %s", (config.version(), row_id))  # noqa: S608
    except Exception as exc:  # noqa: BLE001 - see `record`
        _log.warning("scoring_stamp_failed", table=table, error=f"{type(exc).__name__}: {exc}"[:200])
