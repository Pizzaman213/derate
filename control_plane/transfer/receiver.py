"""The receiving end of a copy. Every node agent runs one on `TRANSFER_PORT`.

A session is opened over the agent's authenticated HTTP API with a validated
manifest; only then will a raw stream carrying that session's id and a MAC over
a fresh nonce be accepted. Chunks land in `<final>.derate-partial` files at
their offsets, so any number of streams can fill one file at once, and nothing
appears under its real name until `commit` has checked every byte arrived and
every blob hashes to its name.

Mode and mtime are carried over so sparkrun's own `rsync -a --size-only`,
which still runs at launch, finds nothing to send. When the agent runs as root
(the shipped container does) every file, directory and link it creates is
chowned to the cache directory's owner, because that rsync runs as the user.

**Nothing below the cache folder is trusted.** The agent runs as root and the
folder belongs to the user, so a symlink planted anywhere in it (`blobs ->
/etc`) would otherwise be written, chmodded and chowned through. Each session
opens its landing directory once and reaches every name beneath it relative to
an open directory descriptor, following no link (`_Dir`): a link where a folder
belongs refuses the copy, and a link where a file belongs is not that file.
"""

from __future__ import annotations

import errno
import logging
import os
import secrets
import shutil
import socket
import socketserver
import stat
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from control_plane.registry import modelcache

from .fabric import LinkAddress, link_addresses
from .protocol import (
    ACK_OK,
    ACK_REFUSED,
    CHUNK_HEADER,
    END_OF_STREAM,
    FOLDER_RE,
    FREE_SPACE_RESERVE,
    MAGIC,
    MAX_SESSIONS,
    MAX_STREAMS,
    PARTIAL_SUFFIX,
    RECV_BUF_BYTES,
    SESSION_IDLE_S,
    TRANSFER_PORT,
    FileEntry,
    Manifest,
    ManifestError,
    expected_digest,
    handshake_mac,
    new_digest,
)

log = logging.getLogger(__name__)

_SESSION_ID_LEN = 16
_SOCKET_TIMEOUT_S = 120.0
#: The whole handshake (hello, then the MAC) must arrive within this. Until a
#: stream has proved the session's secret it holds one of `MAX_STREAMS` slots,
#: and the full I/O timeout would let 32 idle connections lock copies out.
_AUTH_TIMEOUT_S = 10.0
_HASH_BLOCK = 16 << 20

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
#: Directory-relative, no-follow operations (Linux, macOS). Every name a copy
#: writes is resolved against a held directory descriptor; a platform without
#: them (Windows) does not run the receiver at all -- a path-checking fallback
#: is a check-then-use race against whoever else can write the cache.
_AT = bool(_O_NOFOLLOW and _O_DIRECTORY) and {
    os.open, os.mkdir, os.rename, os.unlink, os.symlink, os.stat, os.readlink, os.utime,
} <= os.supports_dir_fd
UNSUPPORTED = (
    "copies are not received on this platform: it has no directory-relative, "
    "no-follow file operations, so a link planted in the cache could redirect a write"
)


class TransferRefused(Exception):
    def __init__(self, status: int, sentence: str) -> None:
        super().__init__(sentence)
        self.status = status
        self.sentence = sentence


def _not_a_folder(rel: str) -> TransferRefused:
    return TransferRefused(
        409, f"{rel} is a link or not a folder on this node, so the copy will not write through it"
    )


class _Dir:
    """A directory at or below a copy's landing directory, reached following no link.

    Held open, and every name below it is resolved against the descriptor.
    """

    def __init__(self, path: Path, fd: int, rel: str) -> None:
        self.path = path
        self.fd: int | None = fd
        self.rel = rel

    @classmethod
    def root(cls, path: Path) -> _Dir:
        """*path* itself is trusted: the hub, or an extra folder already resolved."""
        return cls(path, os.open(path, os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC), "")

    def close(self) -> None:
        fd, self.fd = self.fd, None
        if fd is not None:
            os.close(fd)

    def __enter__(self) -> _Dir:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _name(self, name: str) -> str:
        return name

    def lstat(self, name: str) -> os.stat_result | None:
        try:
            return os.stat(self._name(name), dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def child(self, name: str) -> _Dir | None:
        """The subfolder *name*, None when absent; refused when it is a link or a file."""
        rel = f"{self.rel}/{name}" if self.rel else name
        try:
            fd = os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=self.fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            # ELOOP: a symlink (EMLINK on FreeBSD); ENOTDIR: anything else.
            if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
                raise _not_a_folder(rel) from None
            raise
        return _Dir(self.path / name, fd, rel)

    def make(self, name: str, owner: tuple[int, int] | None) -> _Dir:
        try:
            os.mkdir(self._name(name), 0o755, dir_fd=self.fd)
            made = True
        except FileExistsError:
            made = False
        sub = self.child(name)
        if sub is None:
            raise FileNotFoundError(errno.ENOENT, "removed as it was created", str(self.path / name))
        if made and owner is not None:
            os.fchown(sub.fd, *owner)
        return sub

    def walk(self, parts: Sequence[str], *, create: bool, owner: tuple[int, int] | None = None) -> _Dir | None:
        """The folder *parts* below this one (a new descriptor), or None when absent."""
        assert parts, "walk needs at least the model folder"
        here: _Dir | None = self
        try:
            for name in parts:
                assert here is not None
                nxt = here.make(name, owner) if create else here.child(name)
                if here is not self:
                    here.close()
                here = nxt
                if here is None:
                    return None
        except BaseException:
            if here is not None and here is not self:
                here.close()
            raise
        return here

    def create(self, name: str, owner: tuple[int, int] | None) -> int:
        """A new, empty regular file *name*, read-write. Whatever had the name
        (a partial from an earlier attempt, or anything planted) is unlinked
        rather than opened: O_EXCL never follows a link."""
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC
        try:
            fd = os.open(self._name(name), flags, 0o644, dir_fd=self.fd)
        except FileExistsError:
            os.unlink(self._name(name), dir_fd=self.fd)
            fd = os.open(self._name(name), flags, 0o644, dir_fd=self.fd)
        if owner is not None:
            os.fchown(fd, *owner)
        return fd

    def reopen(self, name: str) -> int:
        """The existing regular file *name*, read-only, never through a link."""
        fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=self.fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise FileNotFoundError(errno.ENOENT, "not a regular file", str(self.path / name))
        return fd

    def unlink(self, name: str) -> None:
        os.unlink(self._name(name), dir_fd=self.fd)

    def rename(self, src: str, dst: str) -> None:
        os.rename(src, dst, src_dir_fd=self.fd, dst_dir_fd=self.fd)

    def readlink(self, name: str) -> str:
        return os.readlink(self._name(name), dir_fd=self.fd)

    def symlink(
        self, target: str, name: str, *, owner: tuple[int, int] | None, mtime: float | None = None
    ) -> None:
        os.symlink(target, self._name(name), dir_fd=self.fd)
        if mtime is not None and os.utime in os.supports_follow_symlinks:
            os.utime(self._name(name), (mtime, mtime), dir_fd=self.fd, follow_symlinks=False)
        if owner is not None:
            os.chown(self._name(name), *owner, dir_fd=self.fd, follow_symlinks=False)


@dataclass
class _Session:
    session_id: str
    secret: bytes
    manifest: Manifest
    #: The directory the model folder sits in (the hub, or an extra cache
    #: folder), held open for the session's life.
    root: _Dir
    owner: tuple[int, int] | None
    have: frozenset[int]
    done: dict[int, dict[int, int]] = field(default_factory=dict)
    #: Open partials, only for files still being written: closed (`_release`)
    #: as soon as the last chunk lands and no stream holds it.
    fds: dict[int, int] = field(default_factory=dict)
    #: Streams between taking a file's descriptor and finishing their chunk.
    holding: dict[int, int] = field(default_factory=dict)
    lock: threading.Condition = field(default_factory=threading.Condition)
    received: int = 0
    writers: int = 0
    last_activity: float = field(default_factory=time.monotonic)
    state: str = "open"
    #: Per-file fsync-and-hash jobs, started the moment a file's last chunk
    #: lands so verification overlaps the streaming instead of following it.
    checks: dict[int, Future] = field(default_factory=dict)
    pool: ThreadPoolExecutor | None = None
    #: Files already moved to their final name (verified blobs, see _check).
    placed: set[int] = field(default_factory=set)
    #: The hub path to link to `dest` at commit, when `dest` is in an extra
    #: cache folder (`modelcache.extra_cache_dirs`); None when it lands in place.
    link: Path | None = None

    @property
    def dest(self) -> Path:
        return self.root.path / self.manifest.folder

    def where(self, path: str) -> tuple[list[str], str]:
        """(folders from the landing directory down, file name) for a manifest path."""
        head, _, name = path.rpartition("/")
        return [self.manifest.folder, *(head.split("/") if head else [])], name

    def wanted(self) -> list[int]:
        return [i for i in range(len(self.manifest.files)) if i not in self.have]


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = os.name != "nt"
    daemon_threads = True
    # Every stream of a copy connects at once; the default backlog of 5 drops SYNs.
    request_queue_size = MAX_STREAMS

    def __init__(self, address: tuple[str, int], receiver: TransferReceiver) -> None:
        self.receiver = receiver
        self.slots = threading.BoundedSemaphore(MAX_STREAMS)
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, _Handler)

    def server_bind(self) -> None:
        if self.address_family == socket.AF_INET6:
            # One socket for both families: a copy may arrive over an IPv6
            # link-local address (a bare cable) or an IPv4 subnet.
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server: _Server = self.server  # type: ignore[assignment]
        if not server.slots.acquire(blocking=False):
            return  # over the cap: closing is the answer
        try:
            server.receiver._serve(self.request)
        except OSError:
            return
        finally:
            server.slots.release()


def _recv_exactly(sock: socket.socket, n: int, deadline: float | None = None) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            sock.settimeout(remaining)
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


class TransferReceiver:
    def __init__(
        self,
        server: _Server,
        cache_dir: Callable[[], Path | None],
        addresses: Callable[[], Sequence[LinkAddress]],
    ) -> None:
        self._server = server
        self._cache_dir = cache_dir
        self._addresses = addresses
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=server.serve_forever, name="transfer-receiver", daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def capacity(self, folder: str | None = None, needed: int = 0) -> dict[str, Any]:
        """Free space where copies land, so the coordinator can plan before it asks.

        Without *folder*, `free`/`total` are the roomiest folder's, because a
        copy lands in an extra cache folder when the hub's own filesystem is
        short. With it, they are the filesystem that folder would land on --
        where it already is, or for a new one the first place with room for
        *needed* bytes, by `open`'s own rule -- and `landing` names that
        directory. `dirs` has every folder and `refused` every
        `DERATE_EXTRA_CACHE_DIRS` entry not used.
        """
        cache = self._cache_dir()
        if cache is None:
            raise TransferRefused(503, "this node has no model cache directory")
        if folder is not None and not FOLDER_RE.match(folder):
            raise TransferRefused(400, "folder must be a model cache folder, models--owner--name")
        hub = Path(cache)
        extras, refused = modelcache.extra_cache_dirs(cache)
        dirs = [{"path": str(cache), "kind": "default", **_usage(hub)}]
        dirs += [{"path": str(d), "kind": "extra", **_usage(d)} for d in extras]
        if folder is None:
            best = max(dirs, key=lambda d: d["free"])
            return {"cache": str(cache), "free": best["free"], "total": best["total"],
                    "dirs": dirs, "refused": list(refused)}
        where = _choose(hub, folder, needed)
        usage = _usage(where)
        return {"cache": str(cache), "free": usage["free"], "total": usage["total"],
                "dirs": dirs, "refused": list(refused), "landing": str(where.parent)}

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)
        with self._lock:
            ids = list(self._sessions)
        for sid in ids:
            self.abort(sid)

    # -- the HTTP half -------------------------------------------------------

    def open(self, body: dict[str, Any]) -> dict[str, Any]:
        self._sweep_idle()
        cache = self._cache_dir()
        if cache is None:
            raise TransferRefused(503, "this node has no model cache directory")
        try:
            manifest = Manifest.from_json(body)
        except ManifestError as exc:
            raise TransferRefused(400, str(exc)) from exc
        folder = manifest.folder
        hub = Path(cache)
        with self._lock:
            if any(s.manifest.folder == folder for s in self._sessions.values()):
                raise TransferRefused(409, f"a copy of {folder} is already being received here")
            if len(self._sessions) >= MAX_SESSIONS:
                raise TransferRefused(409, f"this node is already receiving {MAX_SESSIONS} copies")
            hub.mkdir(parents=True, exist_ok=True)
            dest, link, have = _landing(hub, manifest)
            owner = None
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                st = os.stat(cache)
                owner = (st.st_uid, st.st_gid)
            session = _Session(
                session_id=secrets.token_hex(_SESSION_ID_LEN // 2),
                secret=secrets.token_bytes(32),
                manifest=manifest,
                root=_Dir.root(dest.parent),
                owner=owner,
                have=frozenset(have),
                link=link,
            )
            self._sessions[session.session_id] = session
        return {
            "session_id": session.session_id,
            "secret": session.secret.hex(),
            "port": self.port,
            "addresses": [asdict(a) for a in self._addresses()],
            "have": have,
        }

    def commit(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise TransferRefused(404, "no such copy session")
        files = session.manifest.files
        with session.lock:
            if session.state != "open":
                raise TransferRefused(409, "this copy is already being committed")
            session.state = "committing"
            while session.writers:
                session.lock.wait(timeout=1.0)
            wanted = session.wanted()
            incomplete = [
                i for i in wanted
                if files[i].size and sum(session.done.get(i, {}).values()) != files[i].size
            ]
            if incomplete:
                session.state = "open"
                raise TransferRefused(
                    409, f"{len(incomplete)} of {len(wanted)} files are incomplete; the copy did not finish"
                )
        try:
            result = self._finish(session, wanted)
        except BaseException as exc:
            self._abandon(session)
            if isinstance(exc, OSError):
                raise TransferRefused(500, f"could not finish {session.manifest.folder}: {exc}") from exc
            raise
        session.root.close()
        return result

    def _finish(self, session: _Session, wanted: list[int]) -> dict[str, Any]:
        """Caller aborts the session on any exception."""
        files = session.manifest.files
        with session.lock:
            for i in wanted:
                if files[i].size == 0 and i not in session.checks and i not in session.fds:
                    self._open_partial(session, i)  # no chunk ever opened an empty file
                self._start_check(session, i)
                self._release(session, i)
            checks = {i: session.checks[i] for i in wanted}
        outcomes = {i: f.result() for i, f in checks.items()}
        self._stop_checks(session)
        results = [(i, ok) for i, ok in outcomes.items() if ok is not None]
        for i, ok in results:
            if not ok:
                raise TransferRefused(
                    422, f"{files[i].path} arrived corrupted: its content does not hash to its name"
                )
        for i in wanted:
            if i in session.placed:
                continue
            self._place(session, i)
        for link in session.manifest.links:
            parts, name = session.where(link.path)
            d = session.root.walk(parts, create=True, owner=session.owner)
            assert d is not None
            with d:
                st = d.lstat(name)
                if st is not None:
                    if stat.S_ISLNK(st.st_mode):
                        if d.readlink(name) == link.target:
                            continue
                    elif stat.S_ISDIR(st.st_mode):
                        raise TransferRefused(409, f"{link.path} is a directory here")
                    d.unlink(name)
                d.symlink(link.target, name, owner=session.owner, mtime=link.mtime)
        if session.link is not None:
            self._link(session)
        with self._lock:
            self._sessions.pop(session.session_id, None)
        return {
            "folder": session.manifest.folder,
            "files": len(files),
            "skipped": len(session.have),
            "bytes": session.received,
            "verified": len(results),
            "location": str(session.dest),
        }

    @staticmethod
    def _link(session: _Session) -> None:
        """Point `hub/<folder>` at the extra folder the copy landed in."""
        link = session.link
        assert link is not None
        text = modelcache.link_text(link.parent, session.dest)
        with _Dir.root(link.parent) as hub:
            st = hub.lstat(link.name)
            if st is not None:
                if stat.S_ISLNK(st.st_mode) and hub.readlink(link.name) == text:
                    return
                raise TransferRefused(
                    409, f"{session.manifest.folder} appeared in {link.parent} while it was being "
                         f"copied into {session.dest.parent}; it was not linked",
                )
            hub.symlink(text, link.name, owner=session.owner)

    def abort(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        if not self._stop(session):
            # A commit is running on this session's directory; it discards
            # what it leaves itself (`_abandon`) rather than racing this thread.
            self._discard(session)
        return True

    def _abandon(self, session: _Session) -> None:
        with self._lock:
            self._sessions.pop(session.session_id, None)
        self._stop(session)
        self._discard(session)

    @staticmethod
    def _stop(session: _Session) -> bool:
        """Mark *session* aborted once no write is in flight; True if it was committing."""
        with session.lock:
            committing = session.state == "committing"
            session.state = "aborted"
            while session.writers:
                session.lock.wait(timeout=1.0)
            session.lock.notify_all()
        return committing

    def _discard(self, session: _Session) -> None:
        """Stop the checks, close every descriptor and remove every partial."""
        # Before the fds close: a check may be fsyncing one of them.
        self._stop_checks(session)
        with session.lock:
            fds, session.fds = session.fds, {}
        for fd in fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        files = session.manifest.files
        for i in session.wanted():
            if i in session.placed:
                continue
            parts, name = session.where(files[i].path)
            try:
                d = session.root.walk(parts, create=False)
                if d is None:
                    continue
                with d:
                    d.unlink(name + PARTIAL_SUFFIX)
            except FileNotFoundError:
                pass
            except TransferRefused:
                pass  # a link where a folder was: nothing of ours is behind it
            except OSError as exc:
                log.warning("could not remove %s: %s", session.dest / (files[i].path + PARTIAL_SUFFIX), exc)
        session.root.close()

    def _sweep_idle(self) -> None:
        now = time.monotonic()
        with self._lock:
            stale = [
                s.session_id for s in self._sessions.values()
                if s.state == "open" and now - s.last_activity > SESSION_IDLE_S
            ]
        for sid in stale:
            log.info("abandoning idle copy session %s", sid)
            self.abort(sid)

    # -- the raw-stream half -------------------------------------------------

    def _serve(self, sock: socket.socket) -> None:
        deadline = time.monotonic() + _AUTH_TIMEOUT_S
        hello = _recv_exactly(sock, len(MAGIC) + _SESSION_ID_LEN, deadline)
        if hello is None or hello[: len(MAGIC)] != MAGIC:
            return
        sid = hello[len(MAGIC):].decode("ascii", errors="replace")
        with self._lock:
            session = self._sessions.get(sid)
        if session is None:
            sock.sendall(ACK_REFUSED)
            return
        nonce = secrets.token_bytes(32)
        sock.sendall(nonce)
        mac = _recv_exactly(sock, 32, deadline)
        if mac is None or not secrets.compare_digest(mac, handshake_mac(session.secret, nonce)):
            return
        sock.settimeout(_SOCKET_TIMEOUT_S)
        sock.sendall(ACK_OK)
        buf = bytearray(RECV_BUF_BYTES)
        self._receive(sock, session, memoryview(buf))

    def _receive(self, sock: socket.socket, session: _Session, view: memoryview) -> None:
        files = session.manifest.files
        while True:
            header = _recv_exactly(sock, CHUNK_HEADER.size)
            if header is None:
                return
            index, offset, length = CHUNK_HEADER.unpack(header)
            if index == END_OF_STREAM:
                return
            if (
                session.state != "open"
                or index >= len(files)
                or index in session.have
                or offset + length > files[index].size
            ):
                sock.sendall(ACK_REFUSED)
                return
            fd: int | None = None
            refused = False
            with session.lock:
                if session.state != "open":
                    refused = True
                elif index not in session.checks:
                    fd = session.fds.get(index)
                    if fd is None:
                        try:
                            fd = self._open_partial(session, index)
                        except TransferRefused as exc:
                            log.warning("refusing a copy of %s: %s", session.manifest.folder, exc.sentence)
                            refused = True
                    if fd is not None:
                        session.holding[index] = session.holding.get(index, 0) + 1
            if refused:
                sock.sendall(ACK_REFUSED)
                return
            if fd is None:
                # Every byte of this file is here and its check owns it: a
                # resend after a lost ack. Read it; writing would reopen a
                # partial that may be placed already. It is acked only while
                # the copy is open and the check has not found the file bad --
                # an ack after a failed hash would tell the sender the bytes
                # the receiver threw away had landed.
                if not _drain(sock, view, length):
                    return
                with session.lock:
                    session.last_activity = time.monotonic()
                    good = session.state == "open" and _check_not_failed(session.checks[index])
                if not good:
                    sock.sendall(ACK_REFUSED)
                    return
                sock.sendall(ACK_OK)
                continue
            try:
                if not self._write_chunk(sock, session, fd, view, offset, length):
                    return
            finally:
                with session.lock:
                    session.holding[index] -= 1
                    self._release(session, index)
                    session.lock.notify_all()
            with session.lock:
                previous = session.done.setdefault(index, {}).get(offset, 0)
                session.done[index][offset] = length
                session.received += length - previous
                session.last_activity = time.monotonic()
                if sum(session.done[index].values()) == files[index].size:
                    self._start_check(session, index)
                    self._release(session, index)
            sock.sendall(ACK_OK)

    @staticmethod
    def _write_chunk(
        sock: socket.socket, session: _Session, fd: int, view: memoryview, offset: int, length: int
    ) -> bool:
        """Receive *length* bytes into *fd* at *offset*; False on EOF or abort."""
        pos, remaining = offset, length
        while remaining:
            want = min(remaining, RECV_BUF_BYTES)
            got = 0
            while got < want:
                n = sock.recv_into(view[got:want])
                if n == 0:
                    return False  # EOF mid-chunk: no ack, the sender retries
                got += n
            with session.lock:
                if session.state != "open":
                    return False
                session.writers += 1
            try:
                written = 0
                while written < got:
                    written += os.pwrite(fd, view[written:got], pos + written)
            finally:
                with session.lock:
                    session.writers -= 1
                    session.lock.notify_all()
            pos += got
            remaining -= got
        return True

    # -- verification --------------------------------------------------------

    def _start_check(self, session: _Session, index: int) -> None:
        """Queue file *index*'s fsync and hash, once. Caller holds session.lock."""
        if index in session.checks:
            return
        if session.pool is None:
            session.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="transfer-verify")
        session.checks[index] = session.pool.submit(self._check, session, index)

    @staticmethod
    def _release(session: _Session, index: int) -> None:
        """Close file *index*'s write descriptor once every byte is in and no
        stream still holds it. Caller holds session.lock. A queued check does
        not keep it: checks wait for a worker, and a copy of thousands of small
        files would otherwise hold one descriptor per file finished but not
        yet hashed."""
        if index in session.checks and not session.holding.get(index) and index in session.fds:
            os.close(session.fds.pop(index))

    @classmethod
    def _check(cls, session: _Session, index: int) -> bool | None:
        """Durable, then hashed: True/False against the blob's name, None for a non-blob.

        The partial is reopened read-only through the session's directory
        descriptors, following no link. A blob that hashes to its name is
        moved into place at once. It can never be wrong, and a copy cancelled
        later keeps it: the next attempt finds a regular file of the right size
        and does not send it again.
        """
        entry = session.manifest.files[index]
        with session.lock:
            # A resend of a chunk already written may still be writing the
            # same bytes; hashing waits until it is done.
            while session.holding.get(index) and not (session.state == "aborted" and not session.writers):
                session.lock.wait(timeout=1.0)
            if session.state == "aborted":
                return None
        parts, name = session.where(entry.path)
        d = session.root.walk(parts, create=False)
        if d is None:
            raise FileNotFoundError(errno.ENOENT, "its folder was removed", str(session.dest / entry.path))
        with d:
            fd = d.reopen(name + PARTIAL_SUFFIX)
        try:
            # On the descriptor, before the file has its real name.
            os.fchmod(fd, entry.mode)
            os.utime(fd, (entry.mtime, entry.mtime))
            os.fsync(fd)
            want = expected_digest(entry)
            ok = None if want is None else _digest_fd(fd, want[0], entry.size) == want[1]
        finally:
            os.close(fd)
        if ok:
            cls._place(session, index)
        return ok

    @staticmethod
    def _place(session: _Session, index: int) -> None:
        entry = session.manifest.files[index]
        parts, name = session.where(entry.path)
        d = session.root.walk(parts, create=False)
        if d is None:
            raise FileNotFoundError(errno.ENOENT, "its folder was removed", str(session.dest / entry.path))
        with d:
            d.rename(name + PARTIAL_SUFFIX, name)
        with session.lock:
            session.placed.add(index)

    @staticmethod
    def _stop_checks(session: _Session) -> None:
        pool, session.pool = session.pool, None
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)

    # -- files ---------------------------------------------------------------

    @staticmethod
    def _open_partial(session: _Session, index: int) -> int:
        """Caller holds session.lock (or owns the session exclusively)."""
        entry: FileEntry = session.manifest.files[index]
        parts, name = session.where(entry.path)
        d = session.root.walk(parts, create=True, owner=session.owner)
        assert d is not None
        with d:
            fd = d.create(name + PARTIAL_SUFFIX, session.owner)
        try:
            os.ftruncate(fd, entry.size)
        except OSError:
            os.close(fd)
            raise
        session.fds[index] = fd
        return fd


def _drain(sock: socket.socket, view: memoryview, length: int) -> bool:
    """Read and discard *length* bytes; False on EOF."""
    while length:
        n = sock.recv_into(view[: min(length, len(view))])
        if n == 0:
            return False
        length -= n
    return True


def _check_not_failed(check: Future) -> bool:
    """A file's check is still running, or found it good (True) or not a blob (None)."""
    if not check.done():
        return True
    return check.exception() is None and check.result() is not False


def _present(where: Path, files: Sequence[FileEntry]) -> list[int]:
    """Indices of *files* already at *where* as a regular file of the same size.

    Only a content-addressed blob is skipped on size: its name is its hash, so
    the same name and size is the same file. Everything else -- `refs/main`
    holds a 40-character commit whatever the revision, `trees/*`, the
    `.no_exist` markers, a blob with no digest in its name -- is always sent,
    or a revision refresh would leave the node on the old commit.

    No link is followed: a symlink named like a blob is not the blob (it is
    replaced when the real one arrives), and a link where a folder belongs
    refuses the copy.
    """
    try:
        base = _Dir.root(where.parent)
    except FileNotFoundError:
        return []
    opened: dict[str, _Dir | None] = {}
    have: list[int] = []
    try:
        for i, entry in enumerate(files):
            if expected_digest(entry) is None:
                continue
            head, _, name = entry.path.rpartition("/")
            if head not in opened:
                opened[head] = base.walk([where.name, *(head.split("/") if head else [])], create=False)
            d = opened[head]
            st = d.lstat(name) if d is not None else None
            if st is not None and stat.S_ISREG(st.st_mode) and st.st_size == entry.size:
                have.append(i)
    finally:
        for d in opened.values():
            if d is not None:
                d.close()
        base.close()
    return have


def _on_disk(path: Path) -> Path:
    """*path*, or its nearest existing ancestor: a folder not created yet lands on
    its parent's filesystem."""
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def _usage(path: Path) -> dict[str, int]:
    usage = shutil.disk_usage(_on_disk(path))
    return {"free": usage.free, "total": usage.total}


def _candidates(hub: Path, folder: str) -> list[tuple[Path, Path | None]]:
    """Where *folder* may land, in order: (folder path, hub path to link to it or None).

    The hub's own folder wins whenever it has room, so an extra cache folder is
    used only when it is needed. A folder already in the hub -- or already
    linked into an extra folder -- is filled where it is, never split across two
    disks. A copy with no hub folder yet goes to the first extra folder with
    room, preferring one that already holds a partial copy from an earlier attempt.
    """
    default = hub / folder
    extras, _ = modelcache.extra_cache_dirs(hub)
    if default.is_symlink():
        real = modelcache.linked_folder(hub, folder, extras)
        if real is None:
            raise TransferRefused(400, f"{folder} is a symlink here")
        if not real.is_dir():
            raise TransferRefused(
                409, f"{folder} is linked to {real}, which is not there; is the disk it lives on mounted?"
            )
        return [(real, None)]
    if os.path.lexists(default) or not extras:
        return [(default, None)]
    held = [d / folder for d in extras if (d / folder).is_dir() and not (d / folder).is_symlink()]
    rest = [d / folder for d in extras if d / folder not in held]
    return [(p, default) for p in held] + [(default, None)] + [(p, default) for p in rest]


def _held_bytes(where: Path) -> int:
    """Bytes of regular files already in the folder *where*, following no link:
    what an earlier, interrupted copy left there."""
    total = 0
    for top, _, names in os.walk(where, followlinks=False):
        for name in names:
            try:
                st = os.lstat(os.path.join(top, name))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
    return total


_Short = tuple[Path, int, int]  # (folder path, bytes still needed, free bytes)


def _select(
    hub: Path, folder: str, needed_at: Callable[[Path], int]
) -> tuple[tuple[Path, Path | None] | None, list[_Short]]:
    """The one landing rule, for `open` and `capacity` alike: the first
    candidate (`_candidates`' order) where the bytes *needed_at* it fit with
    `FREE_SPACE_RESERVE` kept, as (folder path, hub link); else None and
    every candidate's shortfall."""
    short: list[_Short] = []
    for where, link in _candidates(hub, folder):
        needed = needed_at(where)
        free = _usage(where)["free"]
        if needed <= free - FREE_SPACE_RESERVE:
            return (where, link), short
        short.append((where, needed, free))
    return None, short


def _first_short(hub: Path, folder: str, short: list[_Short]) -> _Short:
    """The shortfall a refusal names: the hub's, else the first candidate's."""
    default = hub / folder
    return next((s for s in short if s[0] == default), short[0])


def _choose(hub: Path, folder: str, needed: int) -> Path:
    """The folder path a copy of *needed* bytes would land in, by `open`'s
    rule: bytes already in a candidate folder are credited against *needed*,
    as `open` credits the files it finds there. With room nowhere, the
    folder whose shortage `open` would name."""
    fit, short = _select(hub, folder, lambda where: max(0, needed - _held_bytes(where)))
    return fit[0] if fit is not None else _first_short(hub, folder, short)[0]


def _landing(hub: Path, manifest: Manifest) -> tuple[Path, Path | None, list[int]]:
    """(folder the copy lands in, hub path to link to it or None, indices already there)."""
    folder = manifest.folder
    present: dict[Path, list[int]] = {}

    def needed_at(where: Path) -> int:
        present[where] = _present(where, manifest.files)
        skip = set(present[where])
        return sum(e.size for i, e in enumerate(manifest.files) if i not in skip)

    fit, short = _select(hub, folder, needed_at)
    if fit is not None:
        return fit[0], fit[1], present[fit[0]]
    first = _first_short(hub, folder, short)
    where, needed, free = first
    sentence = (
        f"{folder} needs {needed / 2**30:.1f} GiB and {where.parent} has "
        f"{free / 2**30:.1f} GiB free; 10 GiB is kept in reserve"
    )
    others = [s for s in short if s is not first]
    if others:
        sentence += ", and no extra cache folder has room either (" + ", ".join(
            f"{w.parent}: {f / 2**30:.1f} GiB free" for w, _, f in others
        ) + ")"
    raise TransferRefused(507, sentence)


def _digest_fd(fd: int, kind: str, size: int) -> str:
    """The digest of the partial as written, read through its own descriptor:
    no name is looked up between the bytes landing and the hash."""
    h = new_digest(kind, size)
    pos = 0
    while pos < size:
        block = os.pread(fd, min(_HASH_BLOCK, size - pos), pos)
        if not block:
            break
        h.update(block)
        pos += len(block)
    return h.hexdigest()


def start_transfer_receiver(
    *,
    host: str | None = None,
    port: int = TRANSFER_PORT,
    cache_dir: Callable[[], Path | None] = modelcache.cache_dir,
    addresses: Callable[[], Sequence[LinkAddress]] = link_addresses,
) -> TransferReceiver:
    """Listen on `host`, or by default on every address of both families
    (`::` dual-stack), falling back to IPv4 alone where IPv6 is switched off.

    Raises OSError (ENOSYS) on a platform without directory-relative,
    no-follow file operations: the node agent then runs without a receiver
    and says why, rather than receiving through a raceable path check."""
    if not _AT:
        log.warning("%s", UNSUPPORTED)
        raise OSError(errno.ENOSYS, UNSUPPORTED)
    if host is not None:
        server = _Server((host, port), receiver=None)  # type: ignore[arg-type]
    else:
        try:
            server = _Server(("::", port), receiver=None)  # type: ignore[arg-type]
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                raise
            server = _Server(("0.0.0.0", port), receiver=None)  # type: ignore[arg-type]
    receiver = TransferReceiver(server, cache_dir, addresses)
    server.receiver = receiver
    cache = cache_dir()
    extras, refused = modelcache.extra_cache_dirs(cache) if cache is not None else ((), ())
    for sentence in refused:
        log.warning("%s", sentence)
    if extras:
        log.info("copies that do not fit in %s land in: %s", cache, ", ".join(map(str, extras)))
    return receiver
