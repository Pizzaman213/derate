"""The deployment lifecycle state machine.

Straight from 00-architecture.md section 4.6:

    PLANNED  -> LAUNCHING -> READY -> DEGRADED -> READY
                          \\         \\          \\
                           -> FAILED  -> FAILED  -> STOPPING -> STOPPED
    READY    -> STOPPING -> STOPPED

Illegal transitions raise. They are bugs, not conditions: a manager that
"corrects" a bad transition hides the bug and ships a deployment record that
lies about what is running.
"""

from __future__ import annotations

from control_plane.contracts import DeploymentState as S

LEGAL: dict[S, frozenset[S]] = {
    S.PLANNED: frozenset({S.LAUNCHING}),
    S.LAUNCHING: frozenset({S.READY, S.FAILED}),
    S.READY: frozenset({S.DEGRADED, S.FAILED, S.STOPPING}),
    # DEGRADED -> FAILED is not in the frozen diagram at the top of this file
    # (it only draws DEGRADED -> READY and DEGRADED -> STOPPING); it is a
    # documented deviation adopted at integration -- see 00-architecture.md,
    # "Appendix: section 4 amendments", the "DEGRADED -> FAILED transition"
    # entry: forcing a STOPPING detour before a degraded deployment that died
    # outright reaches FAILED would misrecord a crash as an operator-requested
    # stop.
    S.DEGRADED: frozenset({S.READY, S.FAILED, S.STOPPING}),
    S.STOPPING: frozenset({S.STOPPED}),
    S.FAILED: frozenset(),
    S.STOPPED: frozenset(),
}

TERMINAL: frozenset[S] = frozenset({S.FAILED, S.STOPPED})

#: States in which a backend is expected to be answering requests.
SERVING: frozenset[S] = frozenset({S.READY, S.DEGRADED})

#: States we persist. A PLANNED deployment has not launched anything, so
#: there is nothing on disk worth reconciling against.
PERSISTED: frozenset[S] = frozenset(
    {S.LAUNCHING, S.READY, S.DEGRADED, S.STOPPING, S.FAILED, S.STOPPED}
)


class IllegalTransition(RuntimeError):
    """Raised on a transition the lifecycle does not allow."""

    def __init__(self, deployment_id: str, source: S, target: S) -> None:
        self.deployment_id = deployment_id
        self.source = source
        self.target = target
        allowed = sorted(s.value for s in LEGAL[source]) or ["(terminal)"]
        super().__init__(
            "deployment %s: illegal transition %s -> %s; legal from %s: %s"
            % (deployment_id, source.value, target.value, source.value, ", ".join(allowed))
        )


def can(source: S, target: S) -> bool:
    return target in LEGAL[source]


def check(deployment_id: str, source: S, target: S) -> None:
    """Raise unless source -> target is legal. A no-op self-transition is legal."""
    if source is target:
        return
    if not can(source, target):
        raise IllegalTransition(deployment_id, source, target)
