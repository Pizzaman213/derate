"""``keys.db``: the SQLite file behind :class:`~control_plane.keys.service.KeyService`.

Two tables, ``orgs`` and ``api_keys``, plus ``meta`` (``schema_version`` and
the id of the default org). The connect pattern is
``inventory/db.py::connect``'s, pragma order included: ``auto_vacuum`` has to
precede ``journal_mode=WAL`` or SQLite ignores it without saying so.

Rows become contract dataclasses here (:func:`row_to_org`,
:func:`row_to_key`) so nothing above this file reads a column name. The
``secret_hash`` column is deliberately absent from :class:`ApiKey`: nothing
that holds a key row can leak the hash.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from control_plane.contracts.auth import ApiKey, AuthUnavailable, KeyStatus, Org
from control_plane.contracts.routing import RoutingPolicy
from control_plane.fsutil import private_sqlite

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS orgs (
    org_id                  TEXT PRIMARY KEY,
    name                    TEXT NOT NULL,
    created_at              REAL NOT NULL,
    created_by              TEXT NOT NULL,
    allowed_models_json     TEXT,
    limits_json             TEXT NOT NULL DEFAULT '{}',
    default_key_limits_json TEXT NOT NULL DEFAULT '{}',
    default_key_expiry_days INTEGER,
    routing_policy          TEXT,
    deleted_at              REAL
);
CREATE TABLE IF NOT EXISTS api_keys (
    key_id                     TEXT PRIMARY KEY,
    secret_hash                TEXT NOT NULL,
    name                       TEXT NOT NULL,
    org_id                     TEXT NOT NULL,
    created_by                 TEXT NOT NULL,
    created_at                 REAL NOT NULL,
    expires_at                 REAL,
    status                     TEXT NOT NULL,
    disabled_at                REAL,
    disabled_by                TEXT,
    disabled_reason            TEXT,
    last_used_at               REAL,
    last_used_ip               TEXT,
    notes                      TEXT NOT NULL DEFAULT '',
    is_admin_key               INTEGER NOT NULL DEFAULT 0,
    admin_username             TEXT,
    allowed_models_json        TEXT,
    routing_policy             TEXT,
    limits_json                TEXT NOT NULL DEFAULT '{}',
    cloud_fallback_local_model TEXT,
    deleted_at                 REAL
);
CREATE INDEX IF NOT EXISTS api_keys_org ON api_keys(org_id);
CREATE INDEX IF NOT EXISTS api_keys_admin ON api_keys(admin_username);
"""


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
    """Connect and apply the schema; :class:`AuthUnavailable` when that fails.

    A file written by a newer schema is refused rather than reset: this file
    holds every credential the gateway accepts, and silently dropping it would
    lock every client out.
    """
    try:
        conn = connect(path)
        conn.executescript(_SCHEMA)
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        elif int(row["value"]) > SCHEMA_VERSION:
            conn.close()
            raise AuthUnavailable(
                f"keys.db is schema version {row['value']}; this build reads {SCHEMA_VERSION}"
            )
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise AuthUnavailable(f"the key store at {path} could not be opened: {exc}") from exc
    return conn, threading.RLock()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return None if row is None else row["value"]


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def dump_list(value: list[str] | None) -> str | None:
    return None if value is None else json.dumps(list(value))


def dump_limits(value: dict[str, float] | None) -> str:
    return json.dumps(dict(value or {}), sort_keys=True)


def dump_policy(value: RoutingPolicy | None) -> str | None:
    return None if value is None else value.value


def _load_list(text: str | None) -> list[str] | None:
    return None if text is None else list(json.loads(text))


def _load_limits(text: str | None) -> dict[str, float]:
    return {k: float(v) for k, v in json.loads(text or "{}").items()}


def _load_policy(text: str | None) -> RoutingPolicy | None:
    return None if text is None else RoutingPolicy(text)


def row_to_org(row: sqlite3.Row) -> Org:
    return Org(
        org_id=row["org_id"],
        name=row["name"],
        created_at=row["created_at"],
        created_by=row["created_by"],
        allowed_models=_load_list(row["allowed_models_json"]),
        limits=_load_limits(row["limits_json"]),
        default_key_limits=_load_limits(row["default_key_limits_json"]),
        default_key_expiry_days=row["default_key_expiry_days"],
        routing_policy=_load_policy(row["routing_policy"]),
        deleted_at=row["deleted_at"],
    )


def row_to_key(row: sqlite3.Row) -> ApiKey:
    return ApiKey(
        key_id=row["key_id"],
        name=row["name"],
        org_id=row["org_id"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        status=KeyStatus(row["status"]),
        disabled_at=row["disabled_at"],
        disabled_by=row["disabled_by"],
        disabled_reason=row["disabled_reason"],
        last_used_at=row["last_used_at"],
        last_used_ip=row["last_used_ip"],
        notes=row["notes"],
        is_admin_key=bool(row["is_admin_key"]),
        admin_username=row["admin_username"],
        allowed_models=_load_list(row["allowed_models_json"]),
        routing_policy=_load_policy(row["routing_policy"]),
        limits=_load_limits(row["limits_json"]),
        cloud_fallback_local_model=row["cloud_fallback_local_model"],
        deleted_at=row["deleted_at"],
    )
