"""The inference engines derate can launch, one module each.

Adding an engine should be one new module here plus one line in ``ENGINES``.
It is not that today -- ``tests/unit/test_engine_registry.py`` measures the gap
and is meant to close it -- but that is the shape this package exists to reach,
and it is the shape ``providers/kinds.py`` already has for upstreams: *"Adding
OpenRouter should be a display name and a key reference. Everything else in
this table is the reason it can be."*

**Two senses of one word.** ``control_plane/runtimes/`` is the inference server
derate *ships* (the tts server, which runs inside a model container). This
package is the engines derate *launches*, which includes that one. The wire
keeps saying ``runtime`` -- ``Deployment.runtime``, the API payloads, the UI
and ``docs/CONTRACTS.md`` are unchanged, because ``contracts/`` is frozen and a
cosmetic rename would cost a record migration for nothing.

**The import rule, which is the reason this is a top-level package rather than
``deploy/engines/``.** Nothing here may import ``deploy``, ``gateway``,
``resolver`` or ``registry`` -- only the standard library and ``contracts``.
``control_plane/deploy/__init__.py`` eagerly pulls in the manager, the sparkrun
adapter, the event bus, recipes, the store and the stub, so ``from
control_plane.deploy.flags import ...`` drags the whole launcher into whatever
asked for one constant. The tree already pays for that: every consumer outside
the package defers the import into a function body -- six sites in
``gateway/internal_api.py`` alone, plus ``capacity_api.py``,
``links/measure.py`` and ``node.py``. Nine deferred imports, none at module
level, and ``gateway/states.py`` exists only to restate a frozenset rather than
import one through that door.

``control_plane/redaction.py`` made the same move for the same reason and says
so in its own docstring. ``tests/unit/test_engine_registry.py`` enforces this
one rather than trusting it.
"""

from __future__ import annotations

from . import llamacpp, sglang, tts, vllm
from .spec import RUNTIME_CACHE_DIR, EngineSpec

#: Every engine derate can launch, keyed by the name that travels on the wire
#: as ``Deployment.runtime``. Insertion order is the order the UI offers them.
ENGINES: dict[str, EngineSpec] = {
    "vllm": vllm.SPEC,
    "sglang": sglang.SPEC,
    "tts": tts.SPEC,
    "llamacpp": llamacpp.SPEC,
}

#: The name list. Derived from the table rather than typed, because an engine
#: without a spec cannot be launched whatever else claims to know about it.
SUPPORTED: tuple[str, ...] = tuple(ENGINES)


def spec_for(engine: str) -> EngineSpec:
    """The spec for *engine*, or ``ValueError`` naming what there is.

    Same contract as ``deploy/flags.py::runtime_spec``, which delegates here.
    """
    try:
        return ENGINES[engine]
    except KeyError:
        raise ValueError(
            "unknown runtime %r; supported: %s" % (engine, ", ".join(SUPPORTED))
        ) from None


__all__ = ["ENGINES", "SUPPORTED", "RUNTIME_CACHE_DIR", "EngineSpec", "spec_for"]
