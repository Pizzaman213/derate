"""``python3 -m control_plane.forensics``: one timeline out of every record derate keeps.

Reads, never writes (SQLite is opened ``?mode=ro``):

* ``auth.db`` -- ``audit`` (every admin action, refusal and login), ``sessions``
  (live sessions: when, from where, which browser) and ``login_attempts``
  (the last lockout window);
* ``telemetry/archive.db`` -- ``requests``, one row per ``/v1`` request
  attempt: key, client address, user agent, model, status, request id; and
  ``access``, one row per HTTP request the gateway answered (reads and open
  paths included) and per socket: address and port, credential, path, query;
* ``logs/audit/*.jsonl`` -- the append-only mirror of the audit table. A
  mirror line is shown only when ``auth.db`` no longer holds the same row
  (missing, or a different hash): that difference is the finding.

and prints one merged, time-ordered timeline, filtered by ``--ip``, ``--key``,
``--actor`` and ``--request-id`` (all of them that are given must match),
then a summary: the distinct addresses, keys, sessions and user agents seen,
first and last sighting per address, and the audit hash chain's verdict.

What it cannot say is in ``docs/FORENSICS.md``: request and response bodies
are never stored, shell keystrokes are never recorded, audit rows from before
schema v3 are unchained, and an address is the proxy's unless
``DERATE_TRUSTED_PROXIES`` named it.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from control_plane import paths
from control_plane import auditlog
from control_plane.auditlog import ts_iso
from control_plane.auth import store as auth_store
from control_plane.contracts.auth import KEY_PREFIX
from control_plane.redaction import printable

#: The key prefix on rows written before 2026-11-22; never minted, never
#: parsed as a credential. Read wherever a stored actor or id is read.
LEGACY_KEY_PREFIX = "drt_"
_KEY_PREFIXES = (KEY_PREFIX, LEGACY_KEY_PREFIX)


def _key_prefix_of(value: str) -> str | None:
    """Which of the two key prefixes *value* starts with, or None."""
    return next((p for p in _KEY_PREFIXES if value.startswith(p)), None)


@dataclass
class Event:
    ts: float
    source: str
    who: str
    ip: str | None
    what: str
    request_id: str | None = None
    user_agent: str | None = None
    session_id: str | None = None
    #: Key ids this event concerns (the caller's, or the key acted on).
    keys: tuple[str, ...] = ()
    #: Raw actor/username for --actor.
    actor: str | None = None

    @property
    def ts_iso(self) -> str:
        return ts_iso(self.ts)


@dataclass
class Filters:
    since: float
    until: float | None = None
    ip: str | None = None
    key: str | None = None
    actor: str | None = None
    request_id: str | None = None

    def match(self, e: Event) -> bool:
        if e.ts < self.since or (self.until is not None and e.ts >= self.until):
            return False
        if self.ip is not None and e.ip != self.ip:
            return False
        if self.key is not None and self.key not in e.keys:
            return False
        if self.actor is not None and self.actor not in (e.actor, e.who):
            return False
        if self.request_id is not None and e.request_id != self.request_id:
            return False
        return True


@dataclass
class Report:
    events: list[Event]
    chain: auth_store.ChainReport | None
    mirror_mismatches: int = 0
    notes: list[str] = field(default_factory=list)


def parse_time(value: str) -> float:
    """Epoch seconds, or RFC 3339 (``Z`` or an offset; naive means UTC)."""
    try:
        return float(value)
    except ValueError:
        pass
    text = value.strip().replace("Z", "+00:00").replace("z", "+00:00")
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def key_id_of(value: str) -> str:
    """``derate-<id>``, ``derate-<id>_<secret>``, the legacy ``drt_`` forms or
    a bare id -> the bare id. Never keeps a secret: only the id segment is
    returned."""
    value = value.strip()
    prefix = _key_prefix_of(value)
    if prefix:
        value = value[len(prefix):]
    return value.split("_", 1)[0]


def _open_ro(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _get(row: sqlite3.Row, name: str, have: set[str]):
    return row[name] if name in have else None


def _audit_keys(row: sqlite3.Row, have: set[str]) -> tuple[str, ...]:
    keys = []
    if row["target_kind"] == "key":
        keys.append(row["target_id"])
    actor = row["actor"] or ""
    if _key_prefix_of(actor):
        keys.append(key_id_of(actor))
    # A refused key is folded per address; the id it presented is in ``after``.
    raw = _get(row, "after_json", have)
    if raw and any(p in raw for p in _KEY_PREFIXES):
        try:
            presented = json.loads(raw).get("presented")
        except (ValueError, AttributeError):
            presented = None
        if isinstance(presented, str) and _key_prefix_of(presented):
            keys.append(key_id_of(presented))
    return tuple(keys)


def audit_events(conn: sqlite3.Connection) -> Iterator[Event]:
    have = _columns(conn, "audit")
    for r in conn.execute("SELECT * FROM audit ORDER BY id"):
        status = _get(r, "status", have)
        what = f"{r['action']} {r['target_kind']}:{r['target_id']}"
        if status is not None:
            what += f" -> {status}"
        yield Event(
            ts=r["ts"],
            source="audit",
            who=r["actor"],
            ip=r["ip"],
            what=what,
            request_id=_get(r, "request_id", have),
            user_agent=_get(r, "user_agent", have),
            session_id=_get(r, "session_id", have),
            keys=_audit_keys(r, have),
            actor=r["actor"],
        )


def session_events(conn: sqlite3.Connection) -> Iterator[Event]:
    have = _columns(conn, "sessions")
    for r in conn.execute("SELECT * FROM sessions"):
        yield Event(
            ts=r["created_at"],
            source="session",
            who=f"session:{r['username']}",
            ip=r["ip"],
            what=f"session {r['sid_hash'][:12]} live, last seen {ts_iso(r['last_seen_at'])}, "
            f"expires {ts_iso(r['expires_at'])}",
            user_agent=_get(r, "user_agent", have),
            session_id=r["sid_hash"][:12],
            actor=r["username"],
        )


def attempt_events(conn: sqlite3.Connection) -> Iterator[Event]:
    for r in conn.execute("SELECT * FROM login_attempts ORDER BY id"):
        yield Event(
            ts=r["ts"],
            source="login",
            who=r["username"],
            ip=r["ip"],
            what="login attempt " + ("ok" if r["ok"] else "failed"),
            actor=r["username"],
        )


def _local_node(conn: sqlite3.Connection) -> str | None:
    """The node whose journal is the gateway's own: the one writer of
    ``requests`` and ``access`` rows. Recorded by the archive on every trusted
    ingest; on an archive from before that, the node with the most ``access``
    rows (the gateway writes one per request it answers)."""
    try:
        row = conn.execute("SELECT v FROM meta WHERE k='local_node'").fetchone()
    except sqlite3.Error:
        row = None
    if row:
        return row["v"]
    if not _columns(conn, "access"):
        return None
    row = conn.execute(
        "SELECT node_id FROM access GROUP BY node_id ORDER BY COUNT(*) DESC LIMIT 1"
    ).fetchone()
    return row["node_id"] if row else None


def _foreign(r: sqlite3.Row, have: set[str], local: str | None) -> str:
    """`` node=<id>`` when a row was not written by the gateway's own node --
    a member's journal is not allowed to write one (N-6), so it is either from
    before that rule or a fabrication, and the reader must see which node."""
    node = r["node_id"] if "node_id" in have else None
    return f" node={node}" if node and node != local else ""


def request_events(conn: sqlite3.Connection) -> Iterator[Event]:
    have = _columns(conn, "requests")
    local = _local_node(conn)
    for r in conn.execute("SELECT * FROM requests ORDER BY ts"):
        key_id = _get(r, "key_id", have)
        model = _get(r, "model_requested", have) or _get(r, "served_name", have) or "-"
        what = f"/v1 {model} -> {r['status'] if r['status'] is not None else '-'}"
        for extra in ("error_code", "error_class"):
            value = _get(r, extra, have)
            if value:
                what += f" {value}"
        what += _foreign(r, have, local)
        yield Event(
            ts=r["ts"],
            source="v1",
            who=f"key:{KEY_PREFIX}{key_id}" if key_id else "-",
            ip=_get(r, "client_ip", have),
            what=what,
            request_id=r["request_id"],
            user_agent=_get(r, "user_agent", have),
            keys=(key_id,) if key_id else (),
            actor=_get(r, "key_name", have),
        )


def access_events(conn: sqlite3.Connection) -> Iterator[Event]:
    """One per HTTP request the gateway answered (reads included) and per
    socket on close: address and source port, credential, path, query,
    status. Absent on an archive from before the ``access`` table."""
    if not _columns(conn, "access"):
        return
    have = _columns(conn, "access")
    local = _local_node(conn)
    for r in conn.execute("SELECT * FROM access ORDER BY ts"):
        who = r["who"] or "-"
        actor = keys = None
        if who.startswith("session:"):
            actor = who[len("session:"):]
        elif who.startswith("key:") and _key_prefix_of(who[len("key:"):]):
            keys = (key_id_of(who[len("key:"):]),)
        target = r["path"] or ""
        if r["query"]:
            target += "?" + r["query"]
        status = r["status"] if r["status"] is not None else "-"
        port = r["client_port"] if r["client_port"] is not None else "-"
        what = f"{r['method']} {target} -> {status} port={port}"
        # The socket peer, when a trusted proxy named a different client.
        peer = r["peer_ip"] if "peer_ip" in r.keys() else None
        if peer and peer != r["client_ip"]:
            what += f" via {peer}"
        n = r["count"] if "count" in have else None
        if n and n > 1:
            what += f" x{n}"
        what += _foreign(r, have, local)
        yield Event(
            ts=r["ts"],
            source="access",
            who=who,
            ip=r["client_ip"],
            what=what,
            request_id=r["request_id"],
            user_agent=r["user_agent"],
            keys=keys or (),
            actor=actor,
        )


_AGENT_DETAIL = ("folder", "bytes_freed", "pid", "name", "signal_sent", "since", "suppressed")


def agent_events(conn: sqlite3.Connection) -> Iterator[Event]:
    """What a node agent did, or refused, on an ``X-Derate-Token`` route --
    journalled on the node itself (``registry/agent.py``), so a cluster-token
    holder talking to an agent directly leaves this even though the
    coordinator never saw the request. ``ip`` is the peer that asked."""
    if not _columns(conn, "events"):
        return
    for r in conn.execute("SELECT * FROM events WHERE source='agent' ORDER BY ts"):
        try:
            body = json.loads(r["body"] or "{}")
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        detail = " ".join(
            f"{k}={body[k]}" for k in _AGENT_DETAIL if body.get(k) not in (None, "")
        )
        what = f"{r['type'] or body.get('type') or '-'} {body.get('method') or ''} {body.get('path') or ''}"
        yield Event(
            ts=r["ts"],
            source="agent",
            who=f"node:{r['node_id']}",
            ip=body.get("peer"),
            what=" ".join((what + " " + detail).split()),
        )


def mirror_lines(directory: Path) -> Iterator[dict]:
    for path in sorted(directory.glob("audit-*.jsonl")):
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    line = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(line, dict) and line.get("kind", "audit") == "audit":
                    yield line


def mirror_events(lines: Iterable[dict], db_hashes: dict[int, str | None]) -> Iterator[Event]:
    """Mirror lines whose row ``auth.db`` no longer holds as written."""
    for line in lines:
        row_id = line.get("id")
        if row_id in db_hashes and db_hashes[row_id] == line.get("row_hash"):
            continue
        reason = "missing from auth.db" if row_id not in db_hashes else "differs from auth.db"
        actor = line.get("actor") or "-"
        keys = (line.get("target_id"),) if line.get("target_kind") == "key" else ()
        if _key_prefix_of(actor):
            keys = (*keys, key_id_of(actor))
        yield Event(
            ts=float(line.get("ts") or 0),
            source="mirror!",
            who=actor,
            ip=line.get("ip"),
            what=f"{line.get('action')} {line.get('target_kind')}:{line.get('target_id')} "
            f"(audit row {row_id} {reason})",
            request_id=line.get("request_id"),
            user_agent=line.get("user_agent"),
            session_id=line.get("session_id"),
            keys=tuple(k for k in keys if k),
            actor=actor,
        )


def investigate(data_dir: Path, filters: Filters, *, logs_dir: Path | None = None) -> Report:
    events: list[Event] = []
    notes: list[str] = []
    chain = None
    db_hashes: dict[int, str | None] = {}

    auth = _open_ro(data_dir / "auth.db")
    if auth is None:
        notes.append(f"no auth.db under {data_dir}")
    else:
        try:
            events.extend(audit_events(auth))
            events.extend(session_events(auth))
            events.extend(attempt_events(auth))
            if "row_hash" in _columns(auth, "audit"):
                rows = auth.execute("SELECT * FROM audit ORDER BY id").fetchall()
                # What each row hashes to *now*: an edit that left row_hash
                # alone still differs from the hash the mirror recorded.
                db_hashes = {r["id"]: auth_store.chain_hash(r["prev_hash"] or "", r) for r in rows}
                head = auditlog.read_head((logs_dir or data_dir / "logs") / "audit")
                chain = auth_store.anchor_chain(auth_store.walk_chain(rows), auth, head)
            else:
                db_hashes = {r["id"]: None for r in auth.execute("SELECT id FROM audit")}
        finally:
            auth.close()

    archive = _open_ro(data_dir / "telemetry" / "archive.db")
    if archive is None:
        notes.append(f"no telemetry/archive.db under {data_dir}")
    else:
        try:
            events.extend(request_events(archive))
            events.extend(access_events(archive))
            events.extend(agent_events(archive))
        finally:
            archive.close()

    mirror_dir = (logs_dir or data_dir / "logs") / "audit"
    mismatches = 0
    if mirror_dir.is_dir():
        for event in mirror_events(mirror_lines(mirror_dir), db_hashes):
            mismatches += 1
            events.append(event)
    else:
        notes.append(f"no audit mirror at {mirror_dir}")

    picked = sorted((e for e in events if filters.match(e)), key=lambda e: (e.ts, e.source))
    return Report(events=picked, chain=chain, mirror_mismatches=mismatches, notes=notes)


def summary(report: Report) -> dict:
    ips: dict[str, list[float]] = {}
    keys, sessions, agents = set(), set(), set()
    for e in report.events:
        if e.ip:
            seen = ips.setdefault(e.ip, [e.ts, e.ts])
            seen[0], seen[1] = min(seen[0], e.ts), max(seen[1], e.ts)
        keys.update(k for k in e.keys if k)
        if e.session_id:
            sessions.add(e.session_id)
        if e.user_agent:
            agents.add(e.user_agent)
    chain = report.chain
    return {
        "events": len(report.events),
        "ips": {ip: {"first": ts_iso(a), "last": ts_iso(b)} for ip, (a, b) in sorted(ips.items())},
        "keys": sorted(keys),
        "sessions": sorted(sessions),
        "user_agents": sorted(agents),
        "mirror_mismatches": report.mirror_mismatches,
        "chain": None if chain is None else asdict(chain),
    }


def chain_line(report: Report) -> str:
    chain = report.chain
    if chain is None:
        return "chain: unavailable (no v3 auth.db)"
    if chain.ok:
        return f"chain: ok ({chain.rows} rows, anchor {chain.anchor or 'none'})"
    if chain.first_bad_id is None:
        return "chain: BROKEN: the HEAD anchor is unreadable"
    return f"chain: BROKEN at audit row {chain.first_bad_id} ({chain.rows} rows verified before it)"


def render_text(report: Report, out) -> None:
    # Every field a client or a member could have chosen goes through
    # printable(): a newline in a username or a path must not write a forged
    # line into the report, nor an escape sequence reach the terminal.
    for e in report.events:
        print(
            f"{e.ts_iso}  {e.source:<8} {printable(e.who):<28} {printable(e.ip or '-'):<16} "
            f"{printable(e.what, 4096)}  {printable(e.request_id or '-')}",
            file=out,
        )
    info = summary(report)
    print("", file=out)
    print(f"events: {info['events']}", file=out)
    print(f"ips ({len(info['ips'])}):", file=out)
    for ip, seen in info["ips"].items():
        print(f"  {printable(ip)}  first {seen['first']}  last {seen['last']}", file=out)
    print(f"keys ({len(info['keys'])}): {', '.join(KEY_PREFIX + k for k in info['keys']) or '-'}", file=out)
    print(f"sessions ({len(info['sessions'])}): {printable(', '.join(info['sessions'])) or '-'}", file=out)
    print(f"user agents ({len(info['user_agents'])}):", file=out)
    for agent in info["user_agents"]:
        print(f"  {agent!r}", file=out)
    if report.mirror_mismatches:
        print(f"mirror: {report.mirror_mismatches} audit rows differ from or are missing in auth.db", file=out)
    for note in report.notes:
        print(f"note: {note}", file=out)
    print(chain_line(report), file=out)


def render_jsonl(report: Report, out) -> None:
    for e in report.events:
        row = asdict(e)
        row["ts_iso"] = e.ts_iso
        row["keys"] = list(e.keys)
        print(json.dumps(row, sort_keys=True, default=str), file=out)
    print(json.dumps({"kind": "summary", **summary(report), "notes": report.notes}, sort_keys=True), file=out)


def main(argv: list[str] | None = None, out=None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(
        prog="python3 -m control_plane.forensics",
        description="One time-ordered timeline from auth.db, archive.db and the audit mirror.",
    )
    parser.add_argument("--since", required=True, help="RFC 3339 or epoch seconds")
    parser.add_argument("--until", default=None, help="RFC 3339 or epoch seconds (exclusive)")
    parser.add_argument("--ip", default=None)
    parser.add_argument("--key", default=None, help="derate-<id>, drt_<id> or the bare key id")
    parser.add_argument("--actor", default=None, help="admin username, or the key name on /v1 rows")
    parser.add_argument("--request-id", default=None)
    parser.add_argument("--data-dir", type=Path, default=None, help="default: the data dir this process would use")
    parser.add_argument("--logs-dir", type=Path, default=None, help="default: <data dir>/logs")
    parser.add_argument("--format", choices=("text", "jsonl"), default="text")
    args = parser.parse_args(argv)
    try:
        filters = Filters(
            since=parse_time(args.since),
            until=parse_time(args.until) if args.until else None,
            ip=args.ip,
            key=key_id_of(args.key) if args.key else None,
            actor=args.actor,
            request_id=args.request_id,
        )
    except ValueError as exc:
        parser.error(f"bad time: {exc}")
    data_dir = args.data_dir or paths.data_dir()
    logs_dir = args.logs_dir or (paths.logs_dir() if args.data_dir is None else None)
    report = investigate(data_dir, filters, logs_dir=logs_dir)
    (render_jsonl if args.format == "jsonl" else render_text)(report, out)
    if report.chain is not None and not report.chain.ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
