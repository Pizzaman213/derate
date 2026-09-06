"""Persisted roster: which nodes are members or candidates, across a restart.

Registry.__init__ used to start every process with an empty ``_members`` and
``_candidates`` dict, so a coordinator restart forgot every admitted worker --
the roster only ever lived in memory. This is the durable half: profiles and
agent URLs survive, live telemetry does not (it means nothing minutes later,
and the health/telemetry loops repopulate it within one round anyway).

Written on every membership change via tempfile + fsync + os.replace, so a
crash mid-write never leaves a half-written ``registry.json`` for the next
start to trip over. (``identity.py`` persists the cluster token with a plain
truncating ``os.open`` -- no tempfile, no fsync, no rename -- so this module
is the more careful of the two, not a copy of an existing pattern.)
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

ROSTER_FILE = "registry.json"


def _empty() -> dict:
    return {"members": {}, "candidates": {}}


def load_roster(data_dir: Path) -> dict:
    """Read the persisted roster. Absent or corrupt: warn and start empty."""
    path = Path(data_dir) / ROSTER_FILE
    if not path.exists():
        return _empty()
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        log.warning("could not read %s (%s); starting with an empty roster", path, exc)
        return _empty()
    if not isinstance(raw, dict):
        log.warning("%s did not contain a JSON object; starting with an empty roster", path)
        return _empty()
    members = raw.get("members")
    candidates = raw.get("candidates")
    return {
        "members": members if isinstance(members, dict) else {},
        "candidates": candidates if isinstance(candidates, dict) else {},
    }


def save_roster(data_dir: Path, members: dict, candidates: dict) -> None:
    """Atomically overwrite the roster file. Best-effort: a failure only warns.

    An unwritable data volume should not take the process down -- the roster
    just does not survive the next restart, same tradeoff identity.py makes
    for the cluster token.
    """
    path = Path(data_dir) / ROSTER_FILE
    payload = {"members": members, "candidates": candidates}
    tmp_path: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), prefix=".registry-", suffix=".tmp"
        )
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except OSError as exc:
        log.warning(
            "could not persist registry roster to %s (%s); it will not survive "
            "a restart",
            path,
            exc,
        )
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
