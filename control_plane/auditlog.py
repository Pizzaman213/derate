"""Audit rows off the database: an append-only JSONL mirror, and a remote sink.

The ``audit`` table in ``auth.db`` is the record, and its hash chain says when
a row in it was changed. Neither helps once the file itself is gone or
rewritten end to end, so every row is written twice more as it is inserted:

``AuditMirror``
    one JSON object per line in ``<logs dir>/audit/audit-YYYYMMDD.jsonl``
    (UTC day). The folder is 0700, each file 0600 and opened ``O_APPEND``, and
    every line is ``fsync``-ed -- the volume is a few rows a minute. Files older
    than ``DERATE_AUDIT_LOG_DAYS`` are deleted at startup and at each day
    rollover; nothing else in the product deletes them.

``AuditSink``
    the same lines, sent off the box: ``udp://host:port`` or
    ``tcp://host:port`` as RFC 5424 syslog (facility auth, severity notice;
    TCP octet-counted), or ``http(s)://...`` as NDJSON POSTs of up to 100
    lines every 2 s with ``Authorization: Bearer $DERATE_AUDIT_SINK_TOKEN``.
    One daemon thread drains a bounded queue; on overflow the oldest line is
    dropped and the next line sent says how many (``"dropped": n``). A
    heartbeat every 60 s carries the chain head, so a silenced sink is visible
    at the receiver and the head is anchored somewhere an intruder on this box
    cannot rewrite. Best effort: it never blocks or fails an audit write.

Standard library only, like ``redaction.py``: every node imports it.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import socket
import threading
import time
import tempfile
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from control_plane import fsutil

log = logging.getLogger("auditlog")

#: ``DERATE_AUDIT_LOG_DAYS``'s default; envspec declares the same string.
AUDIT_LOG_DAYS_DEFAULT = "400"
SINK_QUEUE_MAX = 10000
SINK_BATCH_MAX = 100
SINK_FLUSH_S = 2.0
SINK_HEARTBEAT_S = 60.0
SINK_TIMEOUT_S = 5.0
SINK_BACKOFF_MIN_S = 1.0
SINK_BACKOFF_MAX_S = 30.0
#: RFC 5424 PRI: facility 4 (auth) * 8 + severity 5 (notice).
SYSLOG_PRI = 37

_FILE_PREFIX = "audit-"
_FILE_SUFFIX = ".jsonl"
#: The chain-head anchor beside the mirror files: ``{id, row_hash, rows,
#: ts_iso}`` of the newest audit row, replaced atomically after every write.
HEAD_NAME = "HEAD"


def ts_iso(ts: float) -> str:
    """RFC 3339, UTC, millisecond precision: ``2026-11-18T09:15:02.123Z``."""
    moment = datetime.fromtimestamp(ts, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d")


def hostname() -> str:
    try:
        return socket.gethostname() or "-"
    except OSError:
        return "-"


def audit_log_days(env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    raw = env.get("DERATE_AUDIT_LOG_DAYS", "400")  # == AUDIT_LOG_DAYS_DEFAULT
    try:
        return max(1, int(raw))
    except ValueError:
        log.warning("DERATE_AUDIT_LOG_DAYS=%r is not a whole number; keeping %s days",
                    raw, AUDIT_LOG_DAYS_DEFAULT)
        return int(AUDIT_LOG_DAYS_DEFAULT)


class AuditMirror:
    """Append-only JSONL copy of the audit table. ``write`` never raises."""

    def __init__(
        self,
        directory: Path | str,
        *,
        days: int | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.directory = Path(directory)
        self.days = audit_log_days() if days is None else days
        self._clock = clock
        self._lock = threading.Lock()
        self._host = hostname()
        self._current_day: str | None = None

    def path_for(self, ts: float) -> Path:
        return self.directory / f"{_FILE_PREFIX}{_day(ts)}{_FILE_SUFFIX}"

    def _ensure_dir(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if fsutil.MODES_ARE_ENFORCED:
            os.chmod(self.directory, 0o700)

    def prune(self, now: float | None = None) -> list[Path]:
        """Delete mirror files whose UTC day is older than ``days``."""
        now = self._clock() if now is None else now
        cutoff = _day(now - self.days * 86400)
        removed: list[Path] = []
        try:
            for path in self.directory.glob(f"{_FILE_PREFIX}*{_FILE_SUFFIX}"):
                stamp = path.name[len(_FILE_PREFIX):-len(_FILE_SUFFIX)]
                if len(stamp) == 8 and stamp.isdigit() and stamp < cutoff:
                    path.unlink()
                    removed.append(path)
        except OSError as exc:
            log.error("audit mirror: pruning %s failed: %s", self.directory, exc)
        return removed

    @property
    def head_path(self) -> Path:
        return self.directory / HEAD_NAME

    def write(self, row: Mapping, *, rows: int | None = None) -> bool:
        """Append one row as one line, then -- when *rows* (the table's row
        count with this row in it) is given -- rewrite ``HEAD``. False (and an
        error logged) on failure."""
        line = dict(row)
        line.setdefault("ts_iso", ts_iso(float(line.get("ts") or self._clock())))
        line.setdefault("host", self._host)
        data = (json.dumps(line, sort_keys=True, separators=(",", ":"), default=str) + "\n").encode()
        ts = float(line.get("ts") or self._clock())
        with self._lock:
            try:
                day = _day(ts)
                if day != self._current_day:
                    self._ensure_dir()
                    self._current_day = day
                    self.prune(ts)
                path = self.path_for(ts)
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    fsutil.harden_fd(fd, path)
                    os.write(fd, data)
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError as exc:
                log.error("audit mirror: could not append to %s: %s", self.directory, exc)
                return False
            if rows is not None and line.get("id") is not None and line.get("row_hash"):
                return self._write_head(line, rows)
            return True

    def _write_head(self, line: Mapping, rows: int) -> bool:
        """Atomically replace ``HEAD`` with the newest row's anchor."""
        head = {
            "id": int(line["id"]),
            "row_hash": line["row_hash"],
            "rows": int(rows),
            "ts_iso": line["ts_iso"],
        }
        data = json.dumps(head, sort_keys=True, separators=(",", ":")).encode()
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(prefix=".HEAD.", dir=self.directory)
            try:
                if fsutil.MODES_ARE_ENFORCED:
                    os.fchmod(fd, 0o600)
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, self.head_path)
            return True
        except OSError as exc:
            log.error("audit mirror: could not write %s: %s", self.head_path, exc)
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return False


def read_head(directory: Path | str) -> dict | None:
    """The ``HEAD`` anchor under *directory*, or None when there is none.
    An unreadable or malformed file is returned as ``{}`` -- present but
    useless, which ``store.anchor_chain`` reports as broken, never as absent."""
    path = Path(directory) / HEAD_NAME
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.error("audit mirror: could not read %s: %s", path, exc)
        return {}
    try:
        head = json.loads(text)
    except ValueError:
        return {}
    return head if isinstance(head, dict) else {}


class AuditSink:
    """Best-effort remote copy. ``send`` never blocks and never raises."""

    SCHEMES = ("udp", "tcp", "http", "https")

    def __init__(
        self,
        url: str,
        *,
        token: str | None = None,
        queue_size: int = SINK_QUEUE_MAX,
        heartbeat_s: float = SINK_HEARTBEAT_S,
        flush_s: float = SINK_FLUSH_S,
    ) -> None:
        parts = urlsplit(url)
        if parts.scheme not in self.SCHEMES or not parts.hostname:
            raise ValueError(
                f"audit sink {url!r} is not udp://host:port, tcp://host:port or http(s)://..."
            )
        if parts.scheme in ("udp", "tcp") and not parts.port:
            raise ValueError(f"audit sink {url!r} needs a port")
        self.url = url
        self.scheme = parts.scheme
        self.host = parts.hostname
        self.port = parts.port
        self.token = token or None
        self.heartbeat_s = heartbeat_s
        self.flush_s = flush_s
        self.dropped = 0
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, queue_size))
        self._pending_dropped = 0
        self._dropped_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._heartbeat: Callable[[], tuple[str | None, int]] | None = None
        self._sock: socket.socket | None = None
        self._retry_at = 0.0
        self._backoff = SINK_BACKOFF_MIN_S
        self._hostname = hostname()

    @property
    def label(self) -> str:
        """``scheme://host[:port]`` -- never the path, query or token."""
        return f"{self.scheme}://{self.host}" + (f":{self.port}" if self.port else "")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AuditSink | None":
        env = os.environ if env is None else env
        url = (env.get("DERATE_AUDIT_SINK", "") or "").strip()
        if not url:
            log.info("audit sink: none")
            return None
        try:
            sink = cls(url, token=(env.get("DERATE_AUDIT_SINK_TOKEN", "") or "").strip() or None)
        except ValueError as exc:
            log.error("audit sink: none (%s)", exc)
            return None
        log.info("audit sink: %s", sink.label)
        return sink

    # -- producer side --------------------------------------------------------

    def _note_dropped(self, n: int) -> None:
        with self._dropped_lock:
            self.dropped += n
            self._pending_dropped += n

    def send(self, line: Mapping) -> None:
        item = dict(line)
        while True:
            try:
                self._queue.put_nowait(item)
                return
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    self._note_dropped(1)
                except queue.Empty:
                    pass

    def start(self, heartbeat: Callable[[], tuple[str | None, int]] | None = None) -> None:
        """Start the sender thread; *heartbeat* returns ``(head_hash, rows)``."""
        self._heartbeat = heartbeat
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="audit-sink", daemon=True)
        self._thread.start()

    def close(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # -- consumer side --------------------------------------------------------

    def _take(self, timeout: float) -> dict | None:
        try:
            item = self._queue.get(timeout=max(0.01, timeout))
        except queue.Empty:
            return None
        with self._dropped_lock:
            if self._pending_dropped:
                item = {**item, "dropped": self._pending_dropped}
                self._pending_dropped = 0
        return item

    def heartbeat_line(self) -> dict:
        head, rows = (None, 0)
        if self._heartbeat is not None:
            try:
                head, rows = self._heartbeat()
            except Exception as exc:  # the store may be closed or unavailable
                log.warning("audit sink: heartbeat could not read the chain head: %s", exc)
        now = time.time()
        return {
            "kind": "heartbeat",
            "ts": now,
            "ts_iso": ts_iso(now),
            "host": self._hostname,
            "head_hash": head,
            "rows": rows,
        }

    def _run(self) -> None:
        batch: list[dict] = []
        next_flush = time.monotonic() + self.flush_s
        next_beat = time.monotonic() + self.heartbeat_s
        while not self._stop.is_set():
            wait = self._retry_at - time.monotonic()
            if wait > 0:  # backing off: what deliver() re-queued waits with the rest
                self._stop.wait(min(wait, self.flush_s))
                continue
            now = time.monotonic()
            item = self._take(min(next_flush, next_beat) - now)
            if item is not None:
                batch.append(item)
            now = time.monotonic()
            if now >= next_beat:
                batch.append(self.heartbeat_line())
                next_beat = now + self.heartbeat_s
            if batch and (
                self.scheme not in ("http", "https")
                or len(batch) >= SINK_BATCH_MAX
                or now >= next_flush
            ):
                self.deliver(batch)
                batch = []
            if now >= next_flush:
                next_flush = now + self.flush_s
        while True:  # drain what is left on close
            item = self._take(0.01)
            if item is None:
                break
            batch.append(item)
        if batch:
            self.deliver(batch)

    def syslog_message(self, line: Mapping) -> bytes:
        body = json.dumps(line, sort_keys=True, separators=(",", ":"), default=str)
        stamp = line.get("ts_iso") or ts_iso(time.time())
        host = line.get("host") or self._hostname
        return f"<{SYSLOG_PRI}>1 {stamp} {host} derate {os.getpid()} audit - {body}".encode()

    def _requeue(self, lines: list[dict]) -> None:
        """Put *lines* back at the head of the queue, in order. Heartbeats are
        not kept -- the next one supersedes them. Past the queue's size the
        oldest go, counted as dropped like any other overflow."""
        keep = [line for line in lines if line.get("kind") != "heartbeat"]
        if not keep:
            return
        overflow = 0
        with self._queue.mutex:
            q = self._queue.queue
            q.extendleft(reversed(keep))
            while len(q) > self._queue.maxsize:
                q.popleft()
                overflow += 1
            self._queue.unfinished_tasks += len(keep) - overflow
            self._queue.not_empty.notify()
        if overflow:
            self._note_dropped(overflow)

    def deliver(self, lines: list[dict]) -> bool:
        """Send *lines* now. False on failure, and the lines go back to the
        head of the queue for the next attempt after the backoff."""
        if time.monotonic() < self._retry_at:
            self._requeue(lines)
            return False
        try:
            if self.scheme == "udp":
                self._send_udp(lines)
            elif self.scheme == "tcp":
                self._send_tcp(lines)
            else:
                self._send_http(lines)
        except (OSError, ValueError) as exc:
            self._requeue(lines)
            log.warning("audit sink %s: delivery failed, retrying in %.0fs: %s",
                        self.label, self._backoff, exc)
            self._retry_at = time.monotonic() + self._backoff
            self._backoff = min(SINK_BACKOFF_MAX_S, self._backoff * 2)
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
            return False
        self._backoff = SINK_BACKOFF_MIN_S
        return True

    def _send_udp(self, lines: list[dict]) -> None:
        if self._sock is None:
            family = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_DGRAM)[0][0]
            self._sock = socket.socket(family, socket.SOCK_DGRAM)
        for line in lines:
            self._sock.sendto(self.syslog_message(line), (self.host, self.port))

    def _send_tcp(self, lines: list[dict]) -> None:
        if self._sock is None:
            self._sock = socket.create_connection((self.host, self.port), timeout=SINK_TIMEOUT_S)
        for line in lines:
            msg = self.syslog_message(line)
            self._sock.sendall(str(len(msg)).encode() + b" " + msg)

    def _send_http(self, lines: list[dict]) -> None:
        body = "".join(
            json.dumps(line, sort_keys=True, separators=(",", ":"), default=str) + "\n"
            for line in lines
        ).encode()
        headers = {"content-type": "application/x-ndjson"}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=SINK_TIMEOUT_S) as response:  # noqa: S310
            if response.status >= 300:
                raise OSError(f"HTTP {response.status}")
