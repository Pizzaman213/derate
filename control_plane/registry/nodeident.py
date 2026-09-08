"""Node identity: this machine's own id, persisted once and then kept.

``identity.py`` is the *cluster*'s identity -- the id and the shared join token,
one per fleet. This is the other half and deliberately a separate file: the
node_id belongs to one machine, is not a secret, and outlives the cluster the
machine happens to be in.

Until this existed the id was re-derived on every boot from
``slugify_hostname(socket.gethostname())``, so the hostname *was* the identity.
Renaming a box, re-imaging it, or letting DHCP hand it a new name turned it into
a stranger: it arrived as a fresh candidate needing admission, and the machine it
used to be stayed in the roster forever as an unhealthy ghost -- taking its
label, its ``links.json`` pairs, its ``plan.node_ids`` and its position on the
cluster floor with it, all now pointing at a node that will never answer again.
Two machines that happened to share a hostname had the opposite problem and
collapsed onto one roster row.

The hostname is still where the id *comes from*; it is just no longer where the
id lives. Seeding once and persisting keeps the value every existing cluster
already has -- on first boot after an upgrade the file is absent and the seed is
exactly what the previous code would have computed -- while making it survive
everything that happens to a hostname afterwards.

**This is not a re-keying.** ``node_id`` remains the same slug string it always
was, so nothing downstream changes: the roster, ``links.json``, deployment
``plan.node_ids``, the telemetry journal's pinned id and the UI's saved floor
plan all keep working on the identifier they already hold.

One caveat worth knowing, and it predates this module:
``deploy/sparkrun.py::hosts_for`` falls back to using a ``node_id`` verbatim as
an SSH target for a node the registry does not know. After a rename that
fallback names the *old* hostname. It is only reached when the registry has no
entry at all -- for a member ``hosts_for`` prefers ``profile.address`` -- so a
node that is actually in the cluster is unaffected.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from pathlib import Path

from control_plane import fsutil

from .probe import slugify_hostname

log = logging.getLogger(__name__)

NODE_FILE = "node.json"


def seed_node_id(hostname: str | None = None) -> str:
    """The id a machine gets the first time it ever runs. Hostname-derived.

    Separate from the persisted read so a caller can see what the seed *would*
    be without writing anything.
    """
    return slugify_hostname(hostname if hostname is not None else socket.gethostname())


def load_or_create_node_id(data_dir: Path, override: str | None = None) -> str:
    """Read this machine's persisted node_id, or seed and persist one.

    Precedence is ``override`` (``DERATE_NODE_ID``) > the stored value > a fresh
    hostname seed. An explicit override always wins *and is written*, the same
    rule ``load_or_create_identity`` applies to ``DERATE_TOKEN``: restarting with
    the variable set must not silently keep the old value, and the operator who
    set it should not have to keep setting it.

    An unwritable data volume is a warning, not a failure. The node still runs
    and still has an id; that id just goes back to being hostname-derived on the
    next restart, which is exactly the old behaviour and no worse than it.
    """
    path = Path(data_dir) / NODE_FILE
    stored: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                stored = loaded
            else:
                log.warning("%s did not contain a JSON object; re-seeding", path)
        except (OSError, ValueError) as exc:
            log.warning("could not read %s (%s); re-seeding this node's id", path, exc)

    stored_id = str(stored.get("node_id") or "").strip()
    resolved = (override or "").strip() or stored_id or seed_node_id()

    if resolved == stored_id:
        return resolved

    if stored_id and resolved != stored_id:
        # Only reachable through an override: without one the stored value wins.
        log.info("node id changed by request: %s -> %s", stored_id, resolved)

    payload = {
        "node_id": resolved,
        # Kept across a rewrite: when this machine first identified itself is a
        # fact about the machine, not about the id it currently answers to.
        "created": stored.get("created") or time.time(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle)
        fsutil.harden_path(path)
    except OSError as exc:
        log.warning(
            "could not persist this node's id to %s (%s); it will be derived "
            "from the hostname again on the next restart",
            path,
            exc,
        )
    return resolved
