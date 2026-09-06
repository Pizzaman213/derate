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


def load_or_create_identity(
    data_dir: Path,
    cluster_id: str | None = None,
    token: str | None = None,
) -> ClusterIdentity:
    """Read the persisted identity, or mint and persist one.

    An explicit token from the environment always wins and is persisted, so
    that restarting with SPARKPLANE_TOKEN set does not silently keep an old one.
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
        os.chmod(path, 0o600)
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
    """What the coordinator prints on first start."""
    return (
        "\n"
        "  sparkplane coordinator\n"
        f"  cluster:  {identity.cluster_id}\n"
        f"  token:    {identity.token}\n"
        f"  ui:       {ui_url}\n"
        "\n"
        "  Start another node with the same command on this subnet, or pass\n"
        f"  SPARKPLANE_TOKEN={identity.token} to join from elsewhere.\n"
    )
