"""How we get a command to run, here or on the other node.

Measurement is inherently a two-machine operation, and the two machines are
reached differently: one by fork, one by ssh. Everything that shells out goes
through this seam, which is also what makes the whole component testable without
a pair of Sparks on the desk.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)

DEFAULT_SSH_OPTS = (
    "-o",
    "BatchMode=yes",  # never sit at a password prompt inside a probe
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "ConnectTimeout=5",
)


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def combined(self) -> str:
        return self.stdout + "\n" + self.stderr


class CommandRunner(Protocol):
    def run(self, argv: list[str], timeout: float = 60.0, env: dict[str, str] | None = None) -> CommandResult: ...
    def run_on(self, host: str, argv: list[str], timeout: float = 60.0) -> CommandResult: ...
    def which(self, name: str) -> str | None: ...
    def read_text(self, path: str) -> str | None: ...
    def glob(self, pattern: str) -> list[str]: ...


class SubprocessRunner:
    """The real one. Local commands fork; remote commands go over ssh."""

    def __init__(self, ssh: str = "ssh", ssh_opts: tuple[str, ...] = DEFAULT_SSH_OPTS) -> None:
        self._ssh = ssh
        self._ssh_opts = ssh_opts

    def run(self, argv: list[str], timeout: float = 60.0, env: dict[str, str] | None = None) -> CommandResult:
        merged = {**os.environ, **(env or {})}
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=merged,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                argv=tuple(argv),
                returncode=-1,
                stdout=_as_text(exc.stdout),
                stderr=_as_text(exc.stderr),
                duration_s=time.monotonic() - started,
                timed_out=True,
            )
        except (OSError, ValueError) as exc:
            # A missing binary is an ordinary outcome here, not an incident.
            return CommandResult(
                argv=tuple(argv),
                returncode=127,
                stdout="",
                stderr=str(exc),
                duration_s=time.monotonic() - started,
            )
        return CommandResult(
            argv=tuple(argv),
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            duration_s=time.monotonic() - started,
        )

    def run_on(self, host: str, argv: list[str], timeout: float = 60.0) -> CommandResult:
        remote = " ".join(shlex.quote(a) for a in argv)
        return self.run([self._ssh, *self._ssh_opts, host, remote], timeout=timeout)

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def read_text(self, path: str) -> str | None:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return None

    def glob(self, pattern: str) -> list[str]:
        import glob as _glob

        return sorted(_glob.glob(pattern))


class BackgroundCommand:
    """A long-lived helper process, such as an `ib_write_bw` server.

    Used with `with`, so a probe that fails partway through does not leave a
    listener bound on the peer.
    """

    def __init__(self, runner: CommandRunner, host: str | None, argv: list[str], settle_s: float = 1.0) -> None:
        self._runner = runner
        self._host = host
        self._argv = argv
        self._settle_s = settle_s
        self._thread: threading.Thread | None = None
        self.result: CommandResult | None = None

    def __enter__(self) -> BackgroundCommand:
        def _run() -> None:
            if self._host is None:
                self.result = self._runner.run(self._argv, timeout=120.0)
            else:
                self.result = self._runner.run_on(self._host, self._argv, timeout=120.0)

        self._thread = threading.Thread(target=_run, name="link-bg-cmd", daemon=True)
        self._thread.start()
        # Give the server side a moment to bind before the client dials it.
        time.sleep(self._settle_s)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._thread is not None:
            self._thread.join(timeout=10.0)
            if self._thread.is_alive():
                log.warning("background probe helper %s did not exit", self._argv[0])


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
