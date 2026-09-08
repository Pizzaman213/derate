"""The contracts, dumped. One command, one file, one thing to compare against.

Every shape in this package is already written down exactly once. The problem
was never the definitions -- it was the copies: a TypeScript union mirroring an
enum, a route table in a design document, a second byte-formatter in another
module. Each copy was correct when written and none of them are checked.

So this reflects rather than restates. ``dataclasses.fields()`` and
``Enum.__members__`` already know the answer, and a generator that asks them
cannot itself drift; the only thing that can go stale is the checked-in
``manifest.json``, which is exactly what ``tests/unit/test_contracts_manifest.py``
fails on. Nothing here is hand-written twice, because a manifest somebody has
to remember to update is the failure mode this exists to end -- ``00-architecture.md``
is the demonstration.

    python3 -m control_plane.contracts.manifest            # print it
    python3 -m control_plane.contracts.manifest --write     # rewrite manifest.json

The printed form is what ``ui/src/api/contracts.check.mjs`` reads, the same way
``keyfield.check.mjs`` already shells to Python rather than restating what the
Python answers.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import json
import pkgutil
import sys
from enum import Enum
from pathlib import Path
from typing import Any

MANIFEST_PATH = Path(__file__).with_name("manifest.json")

#: Modules in this package that describe the manifests rather than appear in
#: them. They live here because this is where the things they describe live,
#: but a generator's own module constants are not contracts -- ``routes.py``
#: exporting ``ROUTES_PATH`` put a developer's absolute filesystem path into
#: the manifest on the first run.
_NOT_CONTRACTS = {"manifest", "derived", "routes", "document"}


def _encode(value: Any) -> Any:
    """A stable, JSON-safe rendering of a contract value.

    Sets are sorted because a frozenset's iteration order is not a fact about
    the contract, and a manifest that reordered between runs would fail its own
    freshness test on machines that agree about everything that matters.
    """
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset)):
        return sorted((_encode(v) for v in value), key=repr)
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return {str(k.value if isinstance(k, Enum) else k): _encode(v) for k, v in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _encode(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, type):
        return value.__name__
    if callable(value):
        # Named, never repr'd: a function's repr carries its memory address, so
        # a manifest holding one would differ on every run and its freshness
        # test would fail for a reason that has nothing to do with contracts.
        module = getattr(value, "__module__", "?")
        return f"<function {module}.{getattr(value, '__qualname__', value)}>"
    return repr(value)


def _type_name(annotation: Any) -> str:
    """The annotation as written. ``from __future__ import annotations`` means
    these arrive as strings already, which is the spelling we want to pin: a
    contract that changes ``int`` to ``int | None`` has changed."""
    if isinstance(annotation, str):
        return annotation
    return getattr(annotation, "__name__", None) or str(annotation)


def _describe_dataclass(obj: type) -> dict:
    fields = []
    for f in dataclasses.fields(obj):
        has_default = (
            f.default is not dataclasses.MISSING
            or f.default_factory is not dataclasses.MISSING  # type: ignore[misc]
        )
        entry = {"name": f.name, "type": _type_name(f.type), "optional": has_default}
        if f.default is not dataclasses.MISSING:
            entry["default"] = _encode(f.default)
        fields.append(entry)
    return {"kind": "dataclass", "module": obj.__module__, "fields": fields}


def _describe_enum(obj: type[Enum]) -> dict:
    return {
        "kind": "enum",
        "module": obj.__module__,
        "base": "str" if issubclass(obj, str) else "int" if issubclass(obj, int) else "enum",
        "members": {name: _encode(member.value) for name, member in obj.__members__.items()},
    }


def _contract_modules() -> list[str]:
    package = importlib.import_module("control_plane.contracts")
    names = [
        info.name
        for info in pkgutil.iter_modules(package.__path__)
        if info.name not in _NOT_CONTRACTS and not info.name.startswith("_")
    ]
    return sorted(names)


def reflect_contracts() -> dict:
    """Every type and module constant under ``control_plane/contracts/``."""
    types: dict[str, Any] = {}
    constants: dict[str, Any] = {}

    for mod_name in _contract_modules():
        module = importlib.import_module(f"control_plane.contracts.{mod_name}")
        for name in dir(module):
            if name.startswith("_"):
                continue
            obj = getattr(module, name)
            declared_here = getattr(obj, "__module__", None) == module.__name__

            if isinstance(obj, type) and issubclass(obj, Enum) and declared_here:
                types[name] = _describe_enum(obj)
            elif isinstance(obj, type) and dataclasses.is_dataclass(obj) and declared_here:
                types[name] = _describe_dataclass(obj)
            elif isinstance(obj, type) and declared_here and hasattr(obj, "__protocol_attrs__"):
                # A port. Its methods are the contract, not its fields.
                types[name] = {
                    "kind": "protocol",
                    "module": obj.__module__,
                    "methods": sorted(obj.__protocol_attrs__),
                }
            elif name.isupper() and not callable(obj) and not isinstance(obj, type):
                constants[name] = {"module": module.__name__, "value": _encode(obj)}

    return {"types": dict(sorted(types.items())), "constants": dict(sorted(constants.items()))}


def resolve(dotted: str) -> Any:
    """``module:attr`` to the live object."""
    mod_name, _, attr = dotted.partition(":")
    if not attr:
        raise ValueError(f"expected 'module:attr', got {dotted!r}")
    return getattr(importlib.import_module(mod_name), attr)


def reflect_derived() -> dict:
    """Each derived fact, its canonical value, and everywhere it is restated."""
    from control_plane.contracts import derived

    out = {}
    for fact in derived.FACTS:
        out[fact.name] = {
            "canonical": fact.canonical,
            "value": _encode(resolve(fact.canonical)),
            "why": fact.why,
            "copies": list(fact.copies),
            "restated_at": list(fact.restated_at),
        }
    return dict(sorted(out.items()))


def reflect_env() -> dict:
    """Every environment variable this project reads, and its default."""
    from control_plane import envspec

    return {
        name: {
            "default": _encode(var.default),
            "owner": var.owner,
            "note": var.note,
        }
        for name, var in sorted(envspec.VARIABLES.items())
    }


def build() -> dict:
    manifest = {
        "note": (
            "Generated by control_plane/contracts/manifest.py. Do not edit. "
            "Regenerate with: python3 -m control_plane.contracts.manifest --write"
        ),
        **reflect_contracts(),
        "derived": reflect_derived(),
        "env": reflect_env(),
    }
    return manifest


def render(manifest: dict | None = None) -> str:
    return json.dumps(manifest if manifest is not None else build(), indent=2, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--write", action="store_true", help=f"rewrite {MANIFEST_PATH.name} in place"
    )
    args = parser.parse_args(argv)

    text = render()
    if args.write:
        MANIFEST_PATH.write_text(text, encoding="utf-8")
        print(f"wrote {MANIFEST_PATH}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a CLI
    raise SystemExit(main())
