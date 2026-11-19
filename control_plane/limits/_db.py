"""``limits.db``: the one file two services share.

``PricingStore`` and ``LimitService`` each hold their own connection to it and
each create only their own tables, idempotently. They agree on exactly one
thing, the ``meta`` table, and keep separate version keys in it
(``pricing_schema_version``, ``limits_schema_version``) so neither has to know
which of them opened the file first.

The connect pattern is ``inventory/db.py::connect`` verbatim, pragma order
included: ``auto_vacuum`` must precede ``journal_mode=WAL`` or SQLite ignores
it without saying so. Unlike the inventory, nothing here is re-derivable -- a
counter is the only record that a cycle's budget was spent -- so a file
written by another schema version is refused (``AuthUnavailable``) rather than
reset.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from control_plane.contracts.auth import AuthUnavailable
from control_plane.fsutil import private_sqlite

META_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    private_sqlite(path)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


def open_store(path: Path | str, ddl: str, version_key: str, version: int) -> sqlite3.Connection:
    """Connect, create ``meta`` plus ``ddl``'s tables, and check ``version_key``.

    Raises ``AuthUnavailable`` on any failure: a parent that cannot be created,
    a path that is a directory, a file that is not SQLite, or a version this
    build does not read. The caller is left with no store, never an empty one.
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = connect(path)
        conn.executescript(META_DDL + ddl)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES(?, ?)",
            (version_key, str(version)),
        )
        row = conn.execute("SELECT value FROM meta WHERE key=?", (version_key,)).fetchone()
        if str(row["value"]) != str(version):
            conn.close()
            raise AuthUnavailable(
                f"the limits store {path} has {version_key} {row['value']}, this build "
                f"reads {version}; refusing to open it rather than discard its counters"
            )
    except (OSError, sqlite3.Error) as exc:
        if conn is not None:
            conn.close()
        raise AuthUnavailable(f"the limits store at {path} could not be opened: {exc}") from exc
    return conn
