"""Facts that are not shapes, and the one place each of them lives.

``contracts/`` holds the shapes: a dataclass says what a node is, an enum says
what states a deployment can be in. This module holds the other half -- facts
*derived* from those shapes that several components need and that therefore
got written down more than once.

The classic is the terminal-state set. ``DeploymentState`` is frozen and
correct, but "which of those states mean it is over" is a judgement about the
enum rather than part of it, so four modules each formed their own answer.
``gateway/internal_api.py`` was honest about it -- its ``_TERMINAL_STATES``
carries a comment explaining the copy and ``test_modelcache.py`` asserts the
two stay equal. That assertion is the pattern this module generalises: name the
canonical home once, list every site that restates it, and let a test do the
comparing.

Two lists per fact, because they fail differently:

``copies``
    Resolvable ``module:attr`` names. ``tests/test_single_source.py`` imports
    each and asserts it equals the canonical value. These cannot drift silently.

``restated_at``
    Sites that inline the fact as an expression -- ``if dep.state not in
    (STOPPED, FAILED)`` -- so there is no attribute to compare. A test cannot
    hold these; listing them means a person changing the canonical value can
    grep for what else has to move. Prefer converting one of these into an
    import over leaving it here.

Adding a row is cheap and is the point. If you find yourself re-deriving a fact
that already has a home, add the site rather than the derivation.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DerivedFact:
    """One fact, its home, and everywhere else it currently appears."""

    name: str
    #: ``module:attr`` -- the definition every other site must agree with.
    canonical: str
    #: Why this lives outside ``contracts/``. A fact with no answer here
    #: probably belongs in ``contracts/`` instead.
    why: str
    #: ``module:attr`` sites a test can compare against the canonical value.
    copies: tuple[str, ...] = ()
    #: ``path:line`` sites that inline the fact and cannot be compared.
    restated_at: tuple[str, ...] = ()
    #: How a copy has to match. ``"value"`` is equality. ``"members"`` compares
    #: them as sets, for the case where the canonical form is a tuple of names
    #: and the copy is a table keyed by them -- the fact they share is which
    #: names exist, and requiring the same container would be requiring the
    #: copy to stop being a table.
    compare: str = "value"


FACTS: tuple[DerivedFact, ...] = (
    DerivedFact(
        name="deployment_terminal_states",
        canonical="control_plane.deploy.fsm:TERMINAL",
        why=(
            "a judgement about DeploymentState rather than part of it, and it "
            "belongs beside the transition table that enforces it. The gateway "
            "cannot import it -- the deploy package __init__ pulls the manager, "
            "the sparkrun adapter and the event bus into a request module -- so "
            "gateway/states.py holds the gateway's one spelling and this holds "
            "the two together"
        ),
        copies=("control_plane.gateway.states:TERMINAL",),
        restated_at=(),
    ),
    DerivedFact(
        name="deployment_serving_states",
        canonical="control_plane.deploy.fsm:SERVING",
        why="same as the terminal set: derived from the enum, enforced by the fsm",
        copies=("control_plane.gateway.states:SERVING",),
        compare="members",
    ),
    DerivedFact(
        name="runtime_names",
        canonical="control_plane.deploy.flags:SUPPORTED_RUNTIMES",
        why=(
            "the launcher owns which runtimes exist, because a runtime without "
            "a RuntimeSpec cannot be launched whatever else claims to know it"
        ),
        copies=("control_plane.resolver.support:RUNTIMES",),
        compare="members",
    ),
    DerivedFact(
        name="binary_byte_formatter",
        canonical="control_plane.humanize:binary_bytes",
        why=(
            "a rendering concern, not a contract, and the fit calculator that "
            "wrote it needs it in the same sentence as a refusal. Kept out of "
            "planner/comm.py on purpose: that one formats transfer volumes "
            "beside decimal GB/s bandwidths and is right to stay decimal"
        ),
        copies=("control_plane.fit.calculator:_gib",),
    ),
    DerivedFact(
        name="redacted_placeholder",
        canonical="control_plane.redaction:REDACTED",
        why=(
            "it belongs with the scrubber that writes it, and that scrubber is "
            "no longer inside the provider package -- logfiles.py redacts on "
            "every node, and the worker path may not import providers"
        ),
        copies=(
            "control_plane.providers.config:REDACTED",
            "control_plane.gateway.serialize:REDACTED",
        ),
    ),
    DerivedFact(
        name="default_agent_port",
        canonical="control_plane.registry.config:DEFAULT_AGENT_PORT",
        why="the node agent's own default, read by everything that dials one",
        copies=(),
        restated_at=(
            "control_plane/gateway/settings.py",
            "control_plane/gateway/enroll_api.py",
            "control_plane/registry/agent.py",
            "install.sh",
            "compose.yaml",
        ),
    ),
    DerivedFact(
        name="default_coordinator_port",
        canonical="control_plane.registry.config:DEFAULT_COORDINATOR_PORT",
        why="the coordinator's own default, same reason",
        copies=(),
        restated_at=(
            "control_plane/gateway/main.py",
            "control_plane/deploy/sparkrun.py",
            "install.sh",
            "compose.yaml",
        ),
    ),
    DerivedFact(
        name="node_roles",
        canonical="control_plane.registry.config:ROLE_COORDINATOR",
        why="the role a node takes is the registry's question to answer",
        copies=(),
    ),
)


def by_name(name: str) -> DerivedFact:
    for fact in FACTS:
        if fact.name == name:
            return fact
    raise KeyError(f"no derived fact named {name!r}; known: {[f.name for f in FACTS]}")
