"""Cluster identity: the id and the shared join token, persisted once.

The token is what stops anything on the subnet from enlisting itself. It is
generated on the coordinator's first run, written to the data volume at 0600,
and printed so a human can carry it to the next node. It admits a node that is
not a member yet, and nothing else: a member re-joins only with its own agent
token, which the coordinator mints at random when the node becomes a member and
hands over in the acknowledged join answer. A worker's ``cluster.json`` keeps
that agent token and *not* the cluster token (``save_agent_credential``), so a
compromised worker holds a credential for itself and no other machine. The
coordinator's cluster token can be rotated (``POST /api/cluster/token/rotate``)
because no member depends on it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
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
    # A worker's own credential, delivered by the coordinator in an acked
    # join answer. Only ever set when read off a worker's cluster.json.
    agent_token: str | None = None


def agent_token(cluster_token: str, node_id: str) -> str:
    """The agent token of a member that has not re-joined since per-member
    tokens existed: ``HMAC(cluster_token, "agent\\n" + node_id)``.

    Kept only for that migration window. Every worker held the cluster token,
    so anyone holding one could derive every other node's value -- which is
    why a member is handed a random token of its own on its first re-join
    (``Registry._mint_agent_token``), after which this value opens nothing.
    """
    return hmac.new(
        cluster_token.encode(), b"agent\n" + node_id.encode(), hashlib.sha256
    ).hexdigest()


def new_cluster_id() -> str:
    return f"c-{secrets.token_hex(2)}"


def new_token() -> str:
    return secrets.token_urlsafe(24)


def read_identity(data_dir: Path) -> ClusterIdentity | None:
    """Read a persisted cluster identity without ever creating one.

    ``load_or_create_identity`` mints and writes when nothing is on disk, which
    is right for a coordinator and wrong for a worker: a worker that minted an
    identity would be inventing a cluster nobody asked for. This is the read a
    worker needs to recover the agent token it adopted on an earlier run (or,
    on a worker from before per-member tokens, the cluster token it held).

    Returns None when there is no file, when it is unreadable, or when it holds
    neither token -- all "we have nothing", none of them worth failing over.
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
    agent = str(stored.get("agent_token") or "") or None
    if not token and not agent:
        return None
    return ClusterIdentity(
        cluster_id=str(stored.get("cluster_id") or ""),
        token=token,
        persisted=True,
        path=path,
        agent_token=agent,
    )


def save_agent_credential(data_dir: Path, cluster_id: str, agent_token: str) -> bool:
    """Rewrite a worker's ``cluster.json`` as ``{cluster_id, agent_token}``.

    The cluster token is dropped from the file on purpose: once this member
    holds its own credential it has no further use for the cluster token, and
    a file that kept it would hand a compromised worker every other member's
    pre-migration token and the power to join new machines. Atomic (mkstemp,
    fsync, replace) and 0600 from creation, as ``roster.save_roster`` does.
    Returns False -- with a warning -- when the data volume is unwritable.
    """
    path = Path(data_dir) / CLUSTER_FILE
    payload = {"cluster_id": cluster_id, "agent_token": agent_token}
    tmp_path: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), prefix=".cluster-", suffix=".tmp"
        )
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        fsutil.harden_path(tmp_path)
        os.replace(tmp_path, path)
        return True
    except OSError as exc:
        log.warning(
            "could not persist this node's agent token to %s (%s); it will not "
            "survive a restart",
            path,
            exc,
        )
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return False


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
        "  Keep this machine's data directory. Members re-join with their\n"
        "  own agent tokens, which live in registry.json beside cluster.json:\n"
        "  a coordinator started anywhere with both files has every node\n"
        "  present itself again on its own. Without registry.json a member\n"
        "  cannot re-join by itself; run its install command again.\n"
    )
