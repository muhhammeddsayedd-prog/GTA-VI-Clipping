"""Database access layer: the psycopg 3 connection pool, small query helpers and transaction context
managers. All SQL in the system goes through this module; there is no ORM. Implemented in M01.

Connections are autocommit, so every helper call is its own statement unless it runs inside
`transaction()`. Inside a `transaction()` block the helpers called from the same thread or task
automatically use the connection of that transaction. Rows are returned as dictionaries.
"""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from sixstars.config import env

Row = dict[str, Any]
Params = Sequence[Any] | Mapping[str, Any] | None
Connection = psycopg.Connection[tuple[Any, ...]]

POOL_MIN_SIZE = 1
POOL_TIMEOUT_S = 30.0
CONNECT_TIMEOUT_S = 15

_pool: ConnectionPool | None = None
_tx_conn: ContextVar[Connection | None] = ContextVar("sixstars_tx_conn", default=None)


def pool() -> ConnectionPool:
    """Return the process-wide connection pool, opening it on first use."""
    global _pool
    if _pool is None:
        settings = env()
        new_pool = ConnectionPool(
            conninfo=settings.require("DATABASE_URL"),
            min_size=POOL_MIN_SIZE,
            max_size=max(POOL_MIN_SIZE, settings.DB_POOL_MAX),
            kwargs={"autocommit": True, "connect_timeout": CONNECT_TIMEOUT_S},
            timeout=POOL_TIMEOUT_S,
            check=ConnectionPool.check_connection,
            name="sixstars",
            open=False,
        )
        new_pool.open()
        _pool = new_pool
    return _pool


def close_pool() -> None:
    """Close the pool (process shutdown and tests). The next helper call opens a new one."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def _connection() -> Iterator[Connection]:
    """Yield the connection of the current transaction, or borrow one from the pool."""
    current = _tx_conn.get()
    if current is not None:
        yield current
        return
    with pool().connection() as conn:
        yield conn


def fetch_one(sql: str, params: Params = None) -> Row | None:
    """Run a query and return the first row as a dict, or None when there is no row."""
    with _connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def fetch_all(sql: str, params: Params = None) -> list[Row]:
    """Run a query and return every row as a dict."""
    with _connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def execute(sql: str, params: Params = None) -> int:
    """Run a statement that returns no rows; return the number of rows it affected."""
    with _connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


@contextmanager
def transaction() -> Iterator[Connection]:
    """Run the block in one database transaction: commit on success, roll back on any exception.

    `fetch_one`, `fetch_all` and `execute` called inside the block use this transaction. A nested
    `transaction()` becomes a savepoint.
    """
    current = _tx_conn.get()
    if current is not None:
        with current.transaction():
            yield current
        return
    with pool().connection() as conn:
        token = _tx_conn.set(conn)
        try:
            with conn.transaction():
                yield conn
        finally:
            _tx_conn.reset(token)


def ping() -> bool:
    """Health check: True when the database answers a trivial query."""
    row = fetch_one("select 1 as ok")
    return row is not None and row["ok"] == 1


def heartbeat(name: str, info: Mapping[str, Any] | None = None) -> None:
    """Record that service or trigger `name` is alive now (upsert into `heartbeats`)."""
    execute(
        """
        insert into heartbeats (name, last_seen, info) values (%s, now(), %s)
        on conflict (name) do update set last_seen = excluded.last_seen, info = excluded.info
        """,
        (name, Jsonb(dict(info)) if info is not None else None),
    )
