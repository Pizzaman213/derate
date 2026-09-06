"""Registry failure modes. Each one is a thing an operator can act on."""

from __future__ import annotations


class RegistryError(Exception):
    """Base for everything this package raises."""


class JoinRejected(RegistryError):
    """A join attempt was refused. Agent G maps this to HTTP 403.

    Carries no detail about the token itself. A rejected joiner is told that it
    was rejected, not why, so a wrong-token probe cannot be used as an oracle.
    """

    def __init__(self, reason: str = "join rejected") -> None:
        super().__init__(reason)
        self.reason = reason


class BridgeNetworkError(RegistryError):
    """Container is on a bridge network. mDNS cannot work. Fail loudly."""


class NodeNotFound(RegistryError):
    """No member or candidate with that node_id."""


class ProbeFailed(RegistryError):
    """A remote agent could not be reached or did not answer with a profile.

    Note that the *local* hardware probe never raises: an unprobeable GPU is a
    node the planner skips, not a crash. This is only for remote HTTP probes.
    """
