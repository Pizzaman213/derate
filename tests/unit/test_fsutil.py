"""The SQLite stores are born private, and an old world-readable one is fixed.

auth.db holds admin argon2 hashes and the audit log, keys.db the key hashes,
and the telemetry journal every log line the node emits. Every JSON store
beside them was already 0600; these were created by SQLite at 0644.
"""

from __future__ import annotations

import os
import stat
import sqlite3
from pathlib import Path

import pytest

from control_plane import fsutil
from control_plane.auth import store as auth_store
from control_plane.inventory import db as inventory_db
from control_plane.keys import store as keys_store
from control_plane.limits import _db as limits_db
from control_plane.telemetry import archive as telemetry_archive
from control_plane.telemetry.journal import Journal

pytestmark = pytest.mark.skipif(
    not fsutil.MODES_ARE_ENFORCED, reason="this platform has no file modes"
)

CONNECTS = {
    "auth": auth_store.connect,
    "keys": keys_store.connect,
    "limits": limits_db.connect,
    "inventory": inventory_db.connect,
    "archive": telemetry_archive.connect,
}


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _assert_private(path: Path) -> None:
    assert _mode(path) == 0o600, f"{path.name} is {oct(_mode(path))}"
    for suffix in ("-wal", "-shm"):
        companion = path.with_name(path.name + suffix)
        if companion.exists():
            assert _mode(companion) == 0o600, f"{companion.name} is {oct(_mode(companion))}"


@pytest.fixture(autouse=True)
def _permissive_umask():
    """The umask most installs run under; 0644 is what it gives a new file."""
    old = os.umask(0o022)
    yield
    os.umask(old)


@pytest.mark.parametrize("name", sorted(CONNECTS))
def test_a_new_store_and_its_wal_are_private(tmp_path, name):
    path = tmp_path / "nested" / f"{name}.db"
    conn = CONNECTS[name](path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS probe(x)")
        conn.execute("INSERT INTO probe VALUES (1)")
        assert path.with_name(path.name + "-wal").exists(), "WAL mode is the case at risk"
        _assert_private(path)
    finally:
        conn.close()


@pytest.mark.parametrize("name", sorted(CONNECTS))
def test_a_world_readable_store_from_an_older_build_is_tightened(tmp_path, name):
    path = tmp_path / f"{name}.db"
    with sqlite3.connect(path) as old:
        old.execute("PRAGMA journal_mode=WAL")
        old.execute("CREATE TABLE IF NOT EXISTS probe(x)")
    os.chmod(path, 0o644)
    conn = CONNECTS[name](path)
    conn.close()
    _assert_private(path)


def test_the_journal_is_private(tmp_path):
    path = tmp_path / "telemetry" / "journal.db"
    journal = Journal(path, node_id="n")
    journal.start()
    try:
        journal.append("sample", {"ts": 1.0})
        assert journal.sync(5.0)
        _assert_private(path)
    finally:
        journal.close()
