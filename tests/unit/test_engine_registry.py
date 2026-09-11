"""The engine package: faithful to the launcher's table, and independent of it.

Two different jobs, and the first one is temporary.

``control_plane/engines/`` currently holds a COPY of ``deploy/flags.py``'s
``RUNTIMES``. That duplication is deliberate and short-lived -- the tree it is
being lifted out of is shared with other sessions, and a 700-line cut landing
in the middle of somebody else's edit loses work -- but a copy nothing checks
is exactly what this project's single-source rule exists to prevent. So the
copy is pinned, field for field, until ``flags.py`` delegates and the copy
stops existing.

The second job is permanent: this package may not import the launcher, and
that is the whole reason it is top-level rather than ``deploy/engines/``.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path

import pytest

from control_plane.deploy.flags import RUNTIMES, SUPPORTED_RUNTIMES
from control_plane.engines import ENGINES, SUPPORTED, spec_for

_ROOT = Path(__file__).resolve().parents[2]

#: Packages the engine table must not drag in. ``deploy`` is the one that
#: matters -- its ``__init__`` pulls the manager, the sparkrun adapter and the
#: event bus -- and the other three are there so the rule cannot be satisfied
#: by routing through a neighbour.
_FORBIDDEN = (
    "control_plane.deploy",
    "control_plane.gateway",
    "control_plane.resolver",
    "control_plane.registry",
)


def test_the_two_tables_name_the_same_engines():
    assert set(SUPPORTED) == set(SUPPORTED_RUNTIMES)


@pytest.mark.parametrize("name", sorted(SUPPORTED))
@pytest.mark.parametrize("field", [f.name for f in dataclasses.fields(spec_for("vllm"))])
def test_every_field_of_the_copy_still_matches_the_original(name, field):
    """Field by field, not ``==``: the two dataclasses are different types.

    A parametrized assertion per field so a drift report names the field that
    drifted rather than printing two 40-field records side by side.
    """
    assert getattr(ENGINES[name], field) == getattr(RUNTIMES[name], field)


def test_the_lookup_refuses_an_unknown_engine_the_way_the_launcher_does():
    with pytest.raises(ValueError) as engines_said:
        spec_for("tensorrt")
    assert "unknown runtime 'tensorrt'" in str(engines_said.value)


def test_the_engine_package_does_not_import_the_launcher():
    """The firewall rule, enforced rather than documented.

    A subprocess because ``sys.modules`` in this one is already full of
    everything the rest of the suite imported -- asking the question in-process
    would answer about pytest's imports, not about this package's.
    """
    probe = (
        "import sys, control_plane.engines;"
        "print(':'.join(sorted(m for m in sys.modules"
        " if m.startswith(%r))))" % (_FORBIDDEN,)
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_ROOT, capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    leaked = [m for m in done.stdout.strip().split(":") if m]
    assert not leaked, (
        "control_plane.engines pulled in %s. Nothing here may import the "
        "launcher: see the package docstring for what that costs." % leaked
    )
