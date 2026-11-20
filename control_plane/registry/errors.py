"""Registry failure modes. Each one is a thing an operator can act on."""

from __future__ import annotations


class RegistryError(Exception):
    """Base for everything this package raises."""


class JoinRejected(RegistryError):
    """A join attempt was refused. The gateway maps this to HTTP 403.

    Carries no detail about the token itself. A rejected joiner is told that it
    was rejected, not why, so a wrong-token probe cannot be used as an oracle.
    """

    def __init__(self, reason: str = "join rejected") -> None:
        super().__init__(reason)
        self.reason = reason


class MembersNotMigrated(RegistryError):
    """The cluster token cannot be rotated while a member still depends on it.

    A member that has not re-joined since per-member agent tokens existed (or
    was admitted and has not collected its token yet) presents the cluster
    token on its next join; rotating it now would lock that member out.
    """

    def __init__(self, node_ids: list[str]) -> None:
        super().__init__(", ".join(node_ids))
        self.node_ids = list(node_ids)


class BridgeNetworkError(RegistryError):
    """Container is on a bridge network. mDNS cannot work. Fail loudly."""


class NodeNotFound(RegistryError):
    """No member or candidate with that node_id."""


class ProbeFailed(RegistryError):
    """A remote agent could not be reached or did not answer with a profile.

    Note that the *local* hardware probe never raises: an unprobeable GPU is a
    node the planner skips, not a crash. This is only for remote HTTP probes.
    """
