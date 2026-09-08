"""Cluster identity: the id and the shared join token, persisted once.

The token is what stops anything on the subnet from enlisting itself. It is
generated on the coordinator's first run, written to the data volume at 0600,
and printed so a human can carry it to the next node.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from control_plane import fsutil

log = logging.getLogger(__name__)

CLUSTER_FILE = "cluster.json"


@dataclass(frozen=True)
class ClusterIdentity:
    cluster_id: str
    token: str
    persisted: bool = False
    path: Path | None = None


def new_cluster_id() -> str:
    return f"c-{secrets.token_hex(2)}"


def new_token() -> str:
    return secrets.token_urlsafe(24)


def read_identity(data_dir: Path) -> ClusterIdentity | None:
    """Read a persisted cluster identity without ever creating one.

    ``load_or_create_identity`` mints and writes when nothing is on disk, which
    is right for a coordinator and wrong for a worker: a worker that minted an
    identity would be inventing a cluster nobody asked for. This is the read a
    worker needs to recover the permanent token it adopted on an earlier run.

    Returns None when there is no file, when it is unreadable, or when it holds
    no token -- all "we have nothing", none of them worth failing over.
    """
    path = Path(data_dir) / CLUSTER_FILE
    if not path.exists():
        return None
    try:
        stored = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        log.warning("could not read %s (%s)", path, exc)
        return None
    if not isinstance(stored, dict):
        log.warning("%s did not contain a JSON object", path)
        return None
    token = str(stored.get("token") or "")
    if not token:
        return None
    return ClusterIdentity(
        cluster_id=str(stored.get("cluster_id") or ""),
        token=token,
        persisted=True,
        path=path,
    )


def load_or_create_identity(
    data_dir: Path,
    cluster_id: str | None = None,
    token: str | None = None,
) -> ClusterIdentity:
    """Read the persisted identity, or mint and persist one.

    An explicit token from the environment always wins and is persisted, so
    that restarting with DERATE_TOKEN set does not silently keep an old one.
    An unwritable data volume is a warning, not a failure: the cluster still
    forms, the token just does not survive a restart.
    """
    path = Path(data_dir) / CLUSTER_FILE
    stored: dict = {}
    if path.exists():
        try:
            stored = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            log.warning("could not read %s (%s); minting a new identity", path, exc)
            stored = {}

    resolved_id = cluster_id or stored.get("cluster_id") or new_cluster_id()
    resolved_token = token or stored.get("token") or new_token()

    unchanged = (
        stored.get("cluster_id") == resolved_id
        and stored.get("token") == resolved_token
    )
    if unchanged:
        return ClusterIdentity(resolved_id, resolved_token, persisted=True, path=path)

    payload = {"cluster_id": resolved_id, "token": resolved_token}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create at 0600 before writing, so the token is never briefly readable.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle)
        fsutil.harden_path(path)
        return ClusterIdentity(resolved_id, resolved_token, persisted=True, path=path)
    except OSError as exc:
        log.warning(
            "could not persist cluster identity to %s (%s); the token will not "
            "survive a restart",
            path,
            exc,
        )
        return ClusterIdentity(resolved_id, resolved_token, persisted=False, path=path)


def banner(identity: ClusterIdentity, ui_url: str) -> str:
    """What the coordinator prints on first start.

    The token is still here: this is the coordinator's own stdout on the
    coordinator's own machine, which is the one place the permanent secret
    belongs. But it is no longer the thing you are told to carry -- the UI
    mints a one-hour token per install, and that is what the next machine
    should be given.
    """
    return (
        "\n"
        "  derate coordinator\n"
        f"  cluster:  {identity.cluster_id}\n"
        f"  token:    {identity.token}\n"
        f"  ui:       {ui_url}\n"
        "\n"
        "  Add another machine: open the UI, Settings -> Add a node, and run\n"
        "  the command it gives you over there. It installs, joins and is\n"
        "  admitted; there is nothing to click afterwards.\n"
        "\n"
        "  Same subnet, no UI: the same install command with no arguments\n"
        f"  finds this coordinator. From elsewhere, DERATE_TOKEN={identity.token}\n"
        "  joins as a candidate you then admit by hand.\n"
        "\n"
        "  Keep that token somewhere. It is what makes this machine\n"
        "  replaceable: start a coordinator anywhere with DERATE_TOKEN set to\n"
        "  it and every node presents itself again on its own. Keep this\n"
        "  machine's /data too if you can -- with the roster the nodes come\n"
        "  straight back as members; without it they arrive as candidates to\n"
        "  admit, one click each. Without the token they cannot arrive at all.\n"
    )
