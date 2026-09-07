"""Which build of derate this is.

Exists because of a real incident. A Raspberry Pi joined the cluster, its
container image predated the CPU probe, and the roster read *"device class is
not recognized; cannot confirm this hardware is eligible to join the pool"* --
a sentence about the hardware, for a fault that was entirely in the software.
The machine was fine. Nothing anywhere could say which build was running, so
"this node is three commits behind" and "this node is a mystery box" rendered
identically, and the only way to tell them apart was to ssh in.

``registry/serde.py`` opens with "These payloads cross a version boundary".
This module is what finally lets a payload say *which* version.

**Never raises, and never guesses.** Every reader here is best-effort and the
absence of an answer is itself the answer -- `""`, which renders as "unknown
build" rather than as a fabricated one. A wrong build id is worse than no build
id: it would send someone looking at the wrong commit.

Resolution order, most trustworthy first:

1. ``DERATE_BUILD`` -- stamped into the image at build time from the git SHA.
   This is the only source that is exact for a shipped container.
2. ``git rev-parse`` against the source tree, for a developer running from a
   checkout, suffixed ``+dirty`` when the tree has uncommitted changes. A
   developer's build is not reproducible from a SHA alone and saying so is the
   point.
3. Nothing. A build we cannot identify says so.

The package version from ``pyproject.toml`` is deliberately *not* a fallback.
It has been ``0.1.0`` for the life of the project and would answer the question
"which build" with a string that is true of every build ever made -- which is
the failure this module exists to end, in a more confident voice.
"""

from __future__ import annotations

import logging
import os
import subprocess
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

#: Stamped into the image by the Dockerfile. See docker/build.sh.
BUILD_ENV_VAR = "DERATE_BUILD"

#: How much of a git SHA to keep. Enough to paste into `git show` and enough to
#: be unambiguous in this repo; short enough to sit in a UI cell.
SHA_LENGTH = 12

_GIT_TIMEOUT_S = 2.0


def _from_env() -> str:
    return (os.environ.get(BUILD_ENV_VAR) or "").strip()


def _git(*args: str) -> str | None:
    """One git command against this checkout, or None.

    Shaped like ``probe.run_nvidia_smi``: evidence, not a platform test. No git
    binary, no repository, a timeout, or a non-zero exit are all the same
    answer -- we could not find out.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent.parent), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        log.debug("git %s failed: %s", args, exc)
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _from_git() -> str:
    head = _git("rev-parse", "HEAD")
    if not head:
        return ""
    build = head[:SHA_LENGTH]
    # `--quiet` exits 1 when there is anything staged or unstaged. A developer
    # tree almost always has something, and a SHA alone would misrepresent it
    # as a build somebody else could reproduce.
    if _git("diff", "--quiet", "HEAD") is None:
        build += "+dirty"
    return build


@lru_cache(maxsize=1)
def build_id() -> str:
    """This build, or ``""`` when it cannot be established.

    Cached: it cannot change inside a process, and it is read on every
    ``/agent/profile`` and every health round. Nothing here should shell out to
    git once per request.
    """
    return _from_env() or _from_git()


def describe_build(build: str | None) -> str:
    """A build id as a sentence fragment, for a reason line.

    Kept here rather than in the UI so the server and the terminal say the same
    thing about the same node.
    """
    return build.strip() if (build or "").strip() else "an unidentified build"


def same_build(a: str | None, b: str | None) -> bool:
    """Whether two nodes are running the same build.

    Two *unknowns* are NOT the same build. This is the whole point: an absent
    id means we could not find out, and answering "no skew" from two absences
    would be the confident wrong answer that started this. Skew is only ever
    reported between two builds we can actually name.
    """
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return True  # not comparable, so not a reportable difference
    return a == b


def skew_note(local: str | None, remote: str | None, node_id: str) -> str | None:
    """Why a node may be reporting something the coordinator would not, or None.

    Returned only when both builds are known and they differ. Deliberately does
    not assert that the skew *caused* whatever is wrong -- it names a fact an
    operator can act on and lets them draw the line, which is the same
    conservatism ``serialize._eligibility`` applies to an unknown device class.
    """
    local, remote = (local or "").strip(), (remote or "").strip()
    if not local or not remote or local == remote:
        return None
    return (
        f"{node_id} is running build {remote}; this coordinator is on {local}. "
        "A node on an older build can report hardware this coordinator would "
        "identify, so re-run the installer on that machine before treating "
        "what it says about itself as final."
    )
