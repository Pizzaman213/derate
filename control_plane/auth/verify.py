"""``python3 -m control_plane.auth.verify [--db PATH]``: check the audit hash chain.

Opens ``auth.db`` read-only (it never migrates, locks or writes), recomputes
every hash-chained row and prints one line. Exit 0 when the chain holds, 1 at
the first row that does not hash to what was written or does not link to the
row before it, 2 when the file cannot be read.

Rows written before schema v3 carry no hash and are not counted. The newest
row is then checked against ``<audit dir>/HEAD`` (default ``<logs dir>/audit``),
which the audit mirror rewrites after every row: fewer rows than it recorded,
or a different hash at its id, is BROKEN at that id -- the deletion of the
newest rows the chain alone cannot see. Rewriting both files end to end is
what the remote sink's heartbeat ``head_hash`` is for -- compare the two.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from control_plane import auditlog, paths
from control_plane.contracts.auth import ChainReport

from . import store


def verify_file(path: Path | str, audit_dir: Path | str | None = None) -> ChainReport:
    """The chain report for the ``auth.db`` at *path*, opened read-only,
    anchored on ``HEAD`` under *audit_dir* when given."""
    head = auditlog.read_head(audit_dir) if audit_dir is not None else None
    conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        have = {r["name"] for r in conn.execute("PRAGMA table_info(audit)")}
        if "row_hash" not in have:
            return ChainReport(ok=True, rows=0, first_bad_id=None, head_hash=None, anchor="none")
        report = store.walk_chain(conn.execute("SELECT * FROM audit ORDER BY id"))
        return store.anchor_chain(report, conn, head)
    finally:
        conn.close()


def describe(report: ChainReport) -> str:
    if report.ok:
        return (
            f"chain: ok ({report.rows} rows, head {report.head_hash or '-'}, "
            f"anchor {report.anchor or 'none'})"
        )
    if report.first_bad_id is None:
        return "chain: BROKEN: the HEAD anchor is unreadable"
    return (
        f"chain: BROKEN at audit row {report.first_bad_id} "
        f"({report.rows} rows verified before it, last good head {report.head_hash or '-'})"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m control_plane.auth.verify", description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=None, help="auth.db to check (default: <data dir>/auth.db)")
    parser.add_argument(
        "--audit-dir", type=Path, default=None,
        help="directory holding the mirror's HEAD anchor (default: <logs dir>/audit)",
    )
    args = parser.parse_args(argv)
    path = args.db or (paths.data_dir() / "auth.db")
    audit_dir = args.audit_dir or (paths.logs_dir() / "audit")
    try:
        report = verify_file(path, audit_dir)
    except sqlite3.Error as exc:
        print(f"chain: cannot read {path}: {exc}", file=sys.stderr)
        return 2
    print(describe(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
