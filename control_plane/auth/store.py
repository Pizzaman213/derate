"""``auth.db``: admins, sessions, login attempts and the audit log.

The connect pattern is ``inventory/db.py::connect`` verbatim (pragma order
included: ``auto_vacuum`` must precede ``journal_mode=WAL`` or SQLite ignores
it silently). What differs is the answer to a schema-version mismatch: the
inventory throws its file away because every row is re-derivable, and nothing
here is -- an admin's password hash and the audit log exist nowhere else. A
file written by another schema is therefore refused (``AuthUnavailable``, so
every gated route answers 503) rather than reset.

This module is SQL only. The rules -- lockout, expiry, last-admin refusal,
what gets audited, the audit hash chain -- live in ``service.py``. There is
deliberately no function that deletes from ``audit``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from control_plane.contracts.auth import AuthUnavailable, ChainReport
from control_plane.fsutil import private_sqlite

SCHEMA_VERSION = 4
#: Every version this build migrates in place.
READABLE_VERSIONS = ("1", "2", "3", "4")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS admins (
    username      TEXT PRIMARY KEY,
    email         TEXT,
    password_hash TEXT NOT NULL,
    created_at    REAL NOT NULL,
    created_by    TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    sid_hash     TEXT PRIMARY KEY,
    username     TEXT NOT NULL,
    created_at   REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    expires_at   REAL NOT NULL,
    ip           TEXT,
    user_agent   TEXT,
    login_ip     TEXT
);
CREATE INDEX IF NOT EXISTS sessions_username ON sessions(username);
CREATE TABLE IF NOT EXISTS login_attempts (
    id       INTEGER PRIMARY KEY,
    ts       REAL NOT NULL,
    username TEXT NOT NULL,
    ip       TEXT,
    ok       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS login_attempts_ts ON login_attempts(ts);
CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    before_json TEXT,
    after_json  TEXT,
    ip          TEXT,
    request_id  TEXT,
    user_agent  TEXT,
    session_id  TEXT,
    status      INTEGER,
    prev_hash   TEXT,
    row_hash    TEXT
);
CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts);
"""

#: Columns added after v1, per table, in the order ``ALTER TABLE`` adds them.
_ADDED_COLUMNS = {
    "admins": (("email", "TEXT"),),
    "sessions": (("user_agent", "TEXT"), ("login_ip", "TEXT")),
    "audit": (
        ("request_id", "TEXT"),
        ("user_agent", "TEXT"),
        ("session_id", "TEXT"),
        ("status", "INTEGER"),
        ("prev_hash", "TEXT"),
        ("row_hash", "TEXT"),
    ),
}


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    private_sqlite(path)
    conn = sqlite3.connect(
        str(path), timeout=30.0, isolation_level=None, check_same_thread=False
    )
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


def open_database(path: Path | str) -> tuple[sqlite3.Connection, threading.RLock]:
    """Connect and apply the schema; raise ``AuthUnavailable`` on any failure.

    Fail closed: a path that is a directory, a parent that cannot be created,
    a file that is not SQLite, or one written by another schema version all
    leave the caller with no store, never with an empty one.
    """
    try:
        conn = connect(path)
        conn.executescript(_SCHEMA)
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is not None and str(row["value"]) not in READABLE_VERSIONS:
            conn.close()
            raise AuthUnavailable(
                f"auth store {path} is schema v{row['value']}, this build reads "
                f"v{SCHEMA_VERSION}; refusing to open it rather than discard admins"
            )
        _migrate(conn, stamped=row is not None)
    except (OSError, sqlite3.Error) as exc:
        raise AuthUnavailable(f"the auth store at {path} could not be opened: {exc}") from exc
    return conn, threading.RLock()


def _migrate(conn: sqlite3.Connection, *, stamped: bool) -> None:
    """Bring a v1 or v2 file (or a fresh one) to the current schema, in place.

    v1 -> v2 adds ``admins.email``. Rows written before it keep ``NULL``; the
    unique index allows any number of those. The index is created here rather
    than in ``_SCHEMA`` because on a v1 file the ``CREATE TABLE IF NOT EXISTS``
    is a no-op and an index on the missing column would fail first.

    v2 -> v3 adds the request context and the hash chain to ``audit`` and
    ``user_agent`` to ``sessions``. Audit rows written before v3 keep ``NULL``
    hashes: the chain begins at the first v3 row, and ``verify_chain`` counts
    only the rows it can vouch for rather than inventing hashes for the rest.

    v3 -> v4 adds ``sessions.login_ip``. From v4 ``ip`` and ``user_agent`` are
    where the session was last seen from (``touch_session``) and ``login_ip`` is
    where it signed in. Before v4 nothing ever updated ``ip``, so a v3 row's
    ``ip`` *is* its login address and is copied across rather than left NULL.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        for table, columns in _ADDED_COLUMNS.items():
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, kind in columns:
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
        conn.execute("CREATE INDEX IF NOT EXISTS audit_request ON audit(request_id)")
        conn.execute("UPDATE sessions SET login_ip = ip WHERE login_ip IS NULL")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS admins_email ON admins(email COLLATE NOCASE)"
        )
        if stamped:
            conn.execute(
                "UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),)
            )
        else:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def dump(value: dict | None) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, default=str)


def load(text: str | None) -> dict | None:
    return None if text is None else json.loads(text)


#: What a v3 audit row's hash covers, in this order. ``id`` is not in it: a
#: deleted row shows as the next row's ``prev_hash`` not matching.
CHAIN_COLUMNS = (
    "ts",
    "actor",
    "action",
    "target_kind",
    "target_id",
    "before_json",
    "after_json",
    "ip",
    "request_id",
    "user_agent",
    "session_id",
    "status",
)


def chain_hash(prev_hash: str, row: Mapping[str, Any]) -> str:
    """``sha256(prev_hash + "\\n" + json([the CHAIN_COLUMNS values]))``, hex."""
    payload = json.dumps(
        [row[name] for name in CHAIN_COLUMNS],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256((prev_hash + "\n" + payload).encode("utf-8")).hexdigest()


def walk_chain(rows: Iterable[Mapping[str, Any]]) -> ChainReport:
    """Verify audit rows given in ``id`` order.

    Rows before the first hashed one were written before v3 and are skipped.
    After it, every row must carry a hash, its ``prev_hash`` must be the row
    before's ``row_hash`` (``""`` for the first), and its ``row_hash`` must be
    what its own columns hash to. The first row that fails any of those is
    ``first_bad_id``; ``rows`` counts the chained rows that passed.
    """
    prev: str | None = None
    checked = 0
    for row in rows:
        stored = row["row_hash"]
        if stored is None:
            if prev is None:
                continue
            return ChainReport(ok=False, rows=checked, first_bad_id=row["id"], head_hash=prev)
        expected_prev = prev or ""
        if (row["prev_hash"] or "") != expected_prev or chain_hash(expected_prev, row) != stored:
            return ChainReport(ok=False, rows=checked, first_bad_id=row["id"], head_hash=prev)
        prev = stored
        checked += 1
    return ChainReport(ok=True, rows=checked, first_bad_id=None, head_hash=prev)


def anchor_chain(
    report: ChainReport, conn: sqlite3.Connection, head: Mapping[str, Any] | None
) -> ChainReport:
    """*report* checked against the ``HEAD`` anchor the audit mirror keeps.

    ``walk_chain`` cannot see the newest rows being deleted: what is left
    still links. ``HEAD`` records the newest row's id and hash and the row
    count at the time it was written, so the table having fewer rows, or a
    different hash at that id, is BROKEN at ``HEAD.id``. No head (no mirror,
    or nothing written yet) leaves *report* as it was, ``anchor="none"``.
    """
    from dataclasses import replace

    if head is None:
        return replace(report, anchor="none")
    report = replace(report, anchor="head-file")
    if not report.ok:
        return report
    try:
        head_id = int(head["id"])
        head_rows = int(head["rows"])
        head_hash = str(head["row_hash"])
    except (KeyError, TypeError, ValueError):
        return replace(report, ok=False, first_bad_id=None)
    count = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
    row = conn.execute("SELECT row_hash FROM audit WHERE id=?", (head_id,)).fetchone()
    if count < head_rows or row is None or row[0] != head_hash:
        return replace(report, ok=False, first_bad_id=head_id)
    return report
