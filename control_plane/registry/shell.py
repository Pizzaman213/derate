"""An interactive shell on this machine, over a WebSocket.

This is a remote-exec hole, opened deliberately. ``procs.py`` opens with the
opposite intent -- "three rules keep this a narrow verb rather than a
remote-exec hole" -- and every one of those rules is bypassed by construction
here: a shell is not a bounded verb, it is every verb. That reversal is
recorded in ``00-architecture.md`` and shown on screen in Settings, because a
capability like this must not arrive quietly.

What makes it defensible is that nothing about it is on by default and none of
its gates is derived from a credential the network can obtain:

* ``DERATE_SHELL`` unset means the route is never registered. Not registered
  and refusing -- absent, so it cannot be probed for or enabled by a request.
* ``DERATE_SHELL_KEY`` is set out of band, by a human, and **no route mints it
  and no route returns it**. That matters specifically here: ``POST
  /api/enroll`` is unauthenticated and its token buys the permanent cluster
  token through ``POST /api/nodes/join``, so anything gated on the cluster
  token is gated on a secret the LAN can mint for itself. This one cannot be.
* The ``Origin`` header is checked before the handshake completes. WebSocket
  handshakes are exempt from CORS and are never preflighted, and nothing in
  this product validates ``Host``, so without this any page the operator visits
  could open a root shell -- and DNS rebinding would defeat same-origin anyway.

The session lands on the **host**, not in this container: ``nsenter`` into
PID 1's namespaces. That is what "control the device" means, and it is possible
without new privilege because the container already runs as root with
``--pid=host``. It is also why the blast radius is the machine and, through the
mounted SSH keys, the fleet.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import pty
import shutil
import signal
import struct
import termios
import time
from dataclasses import dataclass
from pathlib import Path

from . import shell_config as config

log = logging.getLogger(__name__)

#: Read at a time from the pty. One MTU-ish chunk; the pump does not try to
#: assemble lines, because a terminal has no lines -- it has bytes and escape
#: sequences, and holding a partial escape sequence back to look for a newline
#: is how you get a cursor move rendered as text.
READ_BYTES = 65536


def _session_id(pid: int) -> int | None:
    """Field 6 of /proc/<pid>/stat, the session id.

    Scanned back from the last ``)`` for the reason ``procs.py`` documents at
    length: field 2 is ``(comm)`` and may contain spaces and parentheses of its
    own, so splitting from the left is wrong for exactly the processes most
    likely to be interesting.
    """
    try:
        line = Path("/proc/%d/stat" % pid).read_text()
    except (OSError, ValueError):
        return None
    close = line.rfind(")")
    if close == -1:
        return None
    parts = line[close + 1 :].split()
    # After "(comm)" the fields are: state ppid pgrp session ...
    if len(parts) < 4:
        return None
    try:
        return int(parts[3])
    except ValueError:
        return None


def _session_members(sid: int) -> list[int]:
    """Every live pid in the session led by *sid*.

    A /proc walk rather than a process-group signal, because job control puts
    background jobs in their own groups and the session is the only thing that
    reliably spans them. Bounded by however many processes exist; on the node
    this runs on, that is the whole point -- the container shares the host PID
    namespace, so a leaked shell IS visible here and can be cleaned up.
    """
    members: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return members
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if _session_id(pid) == sid:
            members.append(pid)
    return members


class ShellRefused(Exception):
    """Refused before the socket is accepted, with a reason worth showing."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass
class Session:
    """One live pty and the process group behind it."""

    pid: int
    fd: int

    def resize(self, cols: int, rows: int) -> None:
        """Tell the pty its new size, so curses programs redraw correctly.

        Without this, `top` and `vim` keep drawing at 80x24 inside whatever the
        browser actually shows, which reads as a rendering bug rather than a
        missing ioctl.
        """
        cols = max(1, min(int(cols), 1000))
        rows = max(1, min(int(rows), 1000))
        try:
            fcntl.ioctl(
                self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0)
            )
        except OSError:
            # A closed or half-dead pty. The read loop notices; this does not
            # need to be the thing that reports it.
            pass

    def write(self, data: bytes) -> None:
        try:
            os.write(self.fd, data)
        except OSError:
            pass

    def close(self) -> None:
        """End the session and everything it started.

        The SESSION, not the process group, and that distinction is the whole
        of this method. ``pty.fork`` calls ``setsid``, so the shell leads a new
        session -- but an interactive shell does job control, which puts every
        background job in a process group **of its own**. A ``killpg`` on the
        leader's group therefore reaches the shell and misses ``sleep 300 &``
        entirely, which is exactly the process that must not outlive the tab.
        Closing a root shell has to leave nothing behind or it is not closed.

        So: hang up the terminal, let the shell do the polite thing with its
        own jobs, then sweep the session by session id and SIGKILL whatever is
        still there. The sweep is the part that is actually guaranteed.
        """
        # 1. Close the master. The kernel sends SIGHUP to the foreground
        #    process group of the session, which is what a closing terminal
        #    means and what a job-control shell knows how to propagate.
        try:
            os.close(self.fd)
        except OSError:
            pass
        # 2. And directly, for a shell that is not in the foreground group.
        try:
            os.killpg(self.pid, signal.SIGHUP)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        # 3. Give the polite path a moment to work before the blunt one.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if not _session_members(self.pid):
                break
            time.sleep(0.05)
        # 4. Whatever is still in this session ignored SIGHUP or never saw it.
        #    A process that ignores a hangup is the one that most needs to go.
        for pid in _session_members(self.pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            os.waitpid(self.pid, 0)
        except (ChildProcessError, OSError):
            pass


def command() -> list[str]:
    """The argv for a host shell.

    ``nsenter -t 1`` enters PID 1's namespaces -- mount, uts, ipc, net, pid --
    which is the difference between a prompt in this container's filesystem and
    a prompt on the machine. It needs no capability the agent does not already
    have: the container runs as root in the host PID namespace, so /proc/1/ns
    is readable and enterable.

    Falls back to a plain shell when nsenter is missing or PID 1 is this
    container's own init (a node NOT run with --pid=host). The caller shows
    which of the two it got, because they are very different things to be
    holding and the operator must not have to guess.
    """
    if host_reachable():
        return [
            "nsenter",
            "--target",
            "1",
            "--mount",
            "--uts",
            "--ipc",
            "--net",
            "--pid",
            "--",
            config.shell_binary(),
            "-l",
        ]
    return [config.shell_binary(), "-l"]


def host_reachable() -> bool:
    """Whether nsenter can put us on the host.

    Both halves are required and neither is inferable from the other: the
    binary has to exist, and PID 1 has to be somebody else's init rather than
    ours -- a container started without --pid=host has a PID 1, it is just the
    entrypoint, and entering its namespaces would be a loop back to here.
    """
    if shutil.which("nsenter") is None:
        return False
    try:
        return os.stat("/proc/1/ns/pid").st_ino != os.stat("/proc/self/ns/pid").st_ino
    except OSError:
        return False


def open_session(cols: int = 80, rows: int = 24) -> Session:
    """Fork a pty running the shell, and return the parent's side of it.

    Everything that can be done before the fork is done before the fork, and
    that is not tidiness. The node agent is multi-threaded -- the telemetry
    sampler, the journal writer, asyncio -- and ``forkpty`` gives the child only
    the calling thread. Any lock another thread happened to be holding (malloc,
    logging, the import lock) is held forever in the child, so a child that
    allocates before it execs can deadlock instead of starting a shell. Python
    warns about exactly this.

    So the argv is built, the binary is resolved and the environment is
    materialised up here, and the child's whole life is one ``execve``.
    """
    argv = command()
    # Resolved here rather than by execvp in the child: a PATH search allocates.
    binary = shutil.which(argv[0]) or argv[0]
    env = dict(os.environ)
    env.setdefault("TERM", "xterm-256color")
    # So a shell can tell, and so `ps` on the node shows why it exists.
    env["DERATE_SHELL_SESSION"] = "1"

    pid, fd = pty.fork()
    if pid == 0:
        # Child. pty.fork() has already made this a session leader with the pty
        # as its controlling terminal. Do nothing else.
        try:
            os.execve(binary, argv, env)
        except BaseException:  # noqa: BLE001 - the last thing this pid does
            os._exit(127)
    session = Session(pid=pid, fd=fd)
    session.resize(cols, rows)
    # Non-blocking: the reader runs on the event loop and a pty with nothing to
    # say must not stall every other connection in the process.
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    return session


def check_key(presented: str | None) -> None:
    """Refuse anything but the configured key. Raises, never returns False."""
    import secrets as _secrets

    key = config.shell_key()
    if not key:
        # Fails closed. A shell enabled with no key configured is not "open to
        # everyone", it is misconfigured, and saying so beats granting it.
        raise ShellRefused(1008, "No shell key is configured on this node.")
    if not presented or not _secrets.compare_digest(key, presented):
        raise ShellRefused(1008, "The shell key is wrong or was not presented.")


def check_origin(origin: str | None) -> None:
    """Refuse a handshake from a page we did not serve.

    This is the only Origin check in the product, and it exists because a
    WebSocket is the one request shape that CORS does not cover: no preflight,
    no `Access-Control-Allow-Origin` negotiation, nothing. A same-origin page
    sends its own origin and is allowed; anything else has to be named.
    """
    allowed = config.shell_origins()
    if not allowed:
        # Absent means same-origin only, which a browser signals by sending the
        # page's own origin -- and we cannot know ours here. Rather than guess,
        # an operator who reaches the UI on a name we would not recognise sets
        # the variable. A missing Origin (curl, a native client) is allowed:
        # the key is what gates those, and refusing them would break every
        # non-browser caller for no gain against a browser attack.
        return
    if origin is None:
        return
    if origin not in allowed:
        raise ShellRefused(1008, f"Origin {origin} may not open a shell on this node.")


async def pump_out(session: Session, send, on_close) -> None:
    """pty -> socket, until the shell exits.

    Reads through the event loop rather than a thread so that a session sitting
    idle costs nothing, and so closing the socket actually stops the reader --
    a blocking os.read on a pty is not interruptible.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    def on_readable() -> None:
        try:
            data = os.read(session.fd, READ_BYTES)
        except BlockingIOError:
            return
        except OSError:
            # The child exited and the pty hung up. EIO is the normal way that
            # arrives, not an error worth logging.
            queue.put_nowait(None)
            return
        queue.put_nowait(data or None)

    loop.add_reader(session.fd, on_readable)
    try:
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            await send(chunk)
    finally:
        try:
            loop.remove_reader(session.fd)
        except (OSError, ValueError):
            pass
        on_close()
