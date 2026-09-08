"""Which deployment states mean what, spelled without importing the launcher.

``control_plane.deploy.fsm`` owns this judgement and is where it belongs: it
sits beside the transition table that enforces it. The gateway cannot simply
import it, and the reason is not laziness -- ``from control_plane.deploy import
fsm`` runs the package ``__init__``, which pulls the deployment manager, the
sparkrun adapter and the event bus into a request-handling module to obtain one
frozenset.

So the gateway keeps its own spelling, which four modules had each arrived at
separately: ``internal_api`` as a named frozenset, ``targets`` as both a tuple
and an inline pair, ``gpu_procs`` as a complement. Four spellings of one fact,
and only the first had anything checking it.

One spelling now, and ``tests/unit/test_single_source.py`` compares it to
``deploy.fsm`` through ``control_plane/contracts/derived.py``, so the copy that
has to exist cannot quietly stop matching the original.
"""

from __future__ import annotations

from control_plane.contracts import DeploymentState

#: Over. Nothing is being served and whatever it held is fair game -- a process
#: still running under one of these is the orphan the kill verb exists for.
TERMINAL: frozenset[DeploymentState] = frozenset(
    {DeploymentState.FAILED, DeploymentState.STOPPED}
)

#: Able to answer a request. DEGRADED is in: degraded is not failure, it is a
#: deployment serving more slowly than it should, and routing around it
#: entirely would take a working model out of rotation.
SERVING: frozenset[DeploymentState] = frozenset(
    {DeploymentState.READY, DeploymentState.DEGRADED}
)

#: Everything that is not over. The complement is spelled from TERMINAL rather
#: than listed, so a new state joins this set by default -- a state nobody has
#: classified yet is far more likely to be live than finished.
LIVE: frozenset[DeploymentState] = frozenset(s for s in DeploymentState if s not in TERMINAL)
