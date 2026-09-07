"""Whether first-run setup has been walked through. One fact, one file.

Deliberately **not** a fourth key in ``settings_store.py``. That module's
docstring reasons carefully about why exactly three keys belong to it: they are
preferences an operator sets, each with an env form and a documented
file > env > default precedence. This is none of those. It is a one-shot fact
about a cluster's history, no deployment would ever want to set it from the
environment, and putting it there would have meant adding a ``GatewaySettings``
field whose only honest default is "ask the cluster".

**The stored flag is not the whole answer, and must not be.** A cluster that is
already serving a model, or that has a provider configured, has plainly been
set up -- whatever this file says or fails to say. So the flag is only ever one
of the inputs to :func:`is_complete`, and the derived signals outrank it. That
ordering is what makes the failure modes survivable in the direction that
matters: a lost or unwritable file re-offers setup to an empty cluster, which
is a wizard someone dismisses, while the opposite mistake -- a working cluster
dropped back into onboarding -- is the one that reads as data loss.

The write mirrors ``settings_store.SettingsStore.save`` exactly: same mkstemp
in the target directory, same ``fsutil.harden_fd`` before any content reaches
the file, same ``os.replace``. A crash between the two leaves the previous file
intact, never a half-written one.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from control_plane import fsutil
from control_plane.paths import data_dir

log = logging.getLogger("gateway.setup_state")

#: Bumped when the on-disk shape changes. Present from the first version,
#: because a config file without one is a migration you cannot write later.
SCHEMA_VERSION = 1

FILENAME = "setup.json"


def setup_path(root: Path | None = None) -> Path:
    return (root or data_dir()) / FILENAME


@dataclass(frozen=True)
class Completion:
    """Why the answer is what it is.

    The reason travels with the verdict because these are not interchangeable:
    "somebody finished the wizard" and "this cluster is already serving a
    model" both mean *do not onboard*, but only the first is a thing a person
    did, and only the second survives the data directory being wiped. A support
    question about a wizard that will not go away is answered by this string.
    """

    completed: bool
    reason: str


class SetupStore:
    """Reads and writes the one-flag file. Owns no derivation."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or setup_path()

    def load(self) -> bool:
        """Has someone finished the wizard? A bad file reads as "no".

        Same posture as ``SettingsStore.load`` and ``ProviderStore.load``: an
        unreadable file logs and yields the neutral answer rather than failing
        startup. Here the neutral answer is False, which costs a dismissable
        wizard on a cluster that is otherwise empty -- and nothing at all on one
        that is not, because :func:`is_complete` will have already said so.
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("setup file unreadable, ignoring it: %s: %s", self.path, exc)
            return False

        if not isinstance(raw, dict):
            log.warning("setup file is not an object, ignoring it: %s", self.path)
            return False
        if raw.get("version") != SCHEMA_VERSION:
            log.warning(
                "setup file schema %r is not %d, ignoring it: %s",
                raw.get("version"), SCHEMA_VERSION, self.path,
            )
            return False
        return raw.get("completed") is True

    def mark_complete(self) -> None:
        """Record that the wizard was finished. Atomic, 0600."""
        text = json.dumps(
            {"version": SCHEMA_VERSION, "completed": True}, indent=2, sort_keys=True
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".setup-")
        try:
            fsutil.harden_fd(fd, self.path)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def reset(self) -> None:
        """Forget it, so the wizard is offered again. Missing file is fine."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def is_complete(
    *,
    flag: bool,
    deployments: int,
    providers: int,
) -> Completion:
    """Is this cluster past first-run?

    Derived signals are checked BEFORE the stored flag, so the reason names the
    strongest evidence rather than the first thing that happened to be true. A
    cluster serving a model is set up whether or not anyone ever clicked
    through, and saying so is more useful than saying a file existed.
    """
    if deployments > 0:
        return Completion(
            True,
            "this cluster is serving %d model%s"
            % (deployments, "" if deployments == 1 else "s"),
        )
    if providers > 0:
        return Completion(
            True,
            "this cluster has %d provider%s configured"
            % (providers, "" if providers == 1 else "s"),
        )
    if flag:
        return Completion(True, "setup was completed on this coordinator")
    return Completion(False, "nothing is configured on this coordinator yet")
