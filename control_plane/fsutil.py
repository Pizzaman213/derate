"""Restricting a file to this account, on a filesystem that may not do modes.

Ten call sites write a secret and then ask for mode 0600: the cluster token,
enrollment tokens, the shell key, provider API keys, the settings file and the
roster. Two things about that are not portable, and they fail in opposite ways.

``os.fchmod`` **does not exist on Windows**. It is called before any content is
written and inside a ``try`` whose handler re-raises, so on Windows the API-key
store does not degrade -- it raises ``AttributeError`` and the write is lost.
That is a crash, not a hardening gap.

``os.chmod`` *does* exist on Windows and does almost nothing: it toggles the
read-only attribute and restricts nobody. Left alone it is worse than the
crash, because the file is written, the call returns, and every reader of this
code believes the mode took.

So: harden where we can, say so where we cannot, and never let either one be
silent. :func:`confidentiality` is what the product reports on screen, because
a key that other local users can read is a fact the operator has to be told
rather than something to leave in a comment.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: Paths already warned about. A node rewrites its settings and secrets on a
#: timer, and one honest warning is information where fifty an hour is noise.
_warned: set[str] = set()

#: Whether this platform can express "this file is mine alone" as a mode.
#: A capability test, not a platform name: it is true of Linux and macOS, false
#: of Windows, and it stays correct on whatever asks the question next.
MODES_ARE_ENFORCED = hasattr(os, "fchmod")


@dataclass(frozen=True)
class Confidentiality:
    """Whether files this process writes can be restricted to this account."""

    enforced: bool
    mechanism: str
    note: str


def confidentiality() -> Confidentiality:
    if MODES_ARE_ENFORCED:
        return Confidentiality(
            enforced=True,
            mechanism="posix-mode-0600",
            note="Secrets are written readable and writable only by this user.",
        )
    return Confidentiality(
        enforced=False,
        mechanism="none",
        note=(
            "This machine cannot restrict a file to your account by mode, so "
            "anyone who can log in here can read the stored provider keys and "
            "the cluster token. Supply keys through environment variables and "
            "store only the reference, or restrict the data directory yourself."
        ),
    )


def _warn_once(path: Path | str) -> None:
    key = str(path)
    if key in _warned:
        return
    _warned.add(key)
    log.warning(
        "%s could not be restricted to this account: this platform does not "
        "enforce file modes. Any user who can log in to this machine can read "
        "it. Prefer an environment variable for provider keys.",
        key,
    )


def harden_fd(fd: int, path: Path | str) -> bool:
    """Restrict an open file to this user. True when the mode was applied.

    *path* is for the message only; the mode goes on the descriptor, before any
    content is written, which is the property the existing call sites already
    rely on and the reason this takes an fd rather than a name.
    """
    if not MODES_ARE_ENFORCED:
        _warn_once(path)
        return False
    try:
        os.fchmod(fd, 0o600)
        return True
    except OSError as exc:
        # A filesystem that accepts the open but not the mode -- some network
        # and FAT mounts. Same honest outcome as a platform that has no modes.
        log.warning("could not set mode 0600 on %s: %s", path, exc)
        return False


def harden_path(path: Path | str) -> bool:
    """Same, for a file already in place. True when the mode was applied."""
    if not MODES_ARE_ENFORCED:
        _warn_once(path)
        return False
    try:
        os.chmod(path, 0o600)
        return True
    except OSError as exc:
        log.warning("could not set mode 0600 on %s: %s", path, exc)
        return False
