"""CONTRACTS.md, written from the manifests rather than by hand.

``00-architecture.md`` is the design record and should stay one: it is a
journal, its appendices argue with its early sections, and that is what a
journal is for. What it cannot also be is the place you look up what is true
right now -- section 4.8 introduces its route table with "the UI codes against
exactly this" and is missing seventeen product routes, and section 1's
non-goals have been reversed, reaffirmed and re-reversed across three dated
appendices with no forward reference from the original text.

So the lookup surface is generated. Every line below comes from
``manifest.json`` or ``routes.json``, which come from the code. It cannot be
stale in a way the code is not, and nobody has to remember to update it.

    python3 -m control_plane.contracts.document --write
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import manifest, routes

DOCUMENT_PATH = Path(__file__).resolve().parents[2] / "docs" / "CONTRACTS.md"

_HEADER = """# Contracts, as the code has them

**Generated. Do not edit.** Regenerate with:

    python3 -m control_plane.contracts.manifest --write
    python3 -m control_plane.contracts.routes --write
    python3 -m control_plane.contracts.document --write

`tests/unit/test_contracts_manifest.py` fails when any of the three is stale, so
what follows is what the code said at the last commit that ran the suite.

This is the lookup surface. `00-architecture.md` is the design record and the
reasoning -- it is written as a journal, its appendices amend its early
sections, and reading it top-down will give you superseded answers. Look things
up here; read that for why.
"""


def _fmt_value(value) -> str:
    if isinstance(value, list):
        return ", ".join(f"`{v}`" for v in value) if value else "(empty)"
    return f"`{value}`"


def render() -> str:
    m = manifest.build()
    r = routes.build()
    out = [_HEADER]

    enums = {k: v for k, v in m["types"].items() if v["kind"] == "enum"}
    dataclasses_ = {k: v for k, v in m["types"].items() if v["kind"] == "dataclass"}
    protocols = {k: v for k, v in m["types"].items() if v["kind"] == "protocol"}

    out.append("\n## Enumerations\n")
    out.append("Every value that crosses the wire as a string.\n")
    out.append("\n| Type | Values | Defined in |\n|---|---|---|\n")
    for name, spec in enums.items():
        values = ", ".join(f"`{v}`" for v in spec["members"].values())
        out.append(f"| `{name}` | {values} | `{spec['module']}` |\n")

    out.append("\n## Data shapes\n")
    for name, spec in dataclasses_.items():
        out.append(f"\n### `{name}`\n\n`{spec['module']}`\n\n")
        out.append("| Field | Type | Optional |\n|---|---|---|\n")
        for f in spec["fields"]:
            out.append(f"| `{f['name']}` | `{f['type']}` | {'yes' if f['optional'] else 'no'} |\n")

    out.append("\n## Ports\n")
    out.append("\nThe interfaces components hold each other to.\n")
    out.append("\n| Port | Methods |\n|---|---|\n")
    for name, spec in protocols.items():
        out.append(f"| `{name}` | {', '.join(f'`{x}`' for x in spec['methods'])} |\n")

    out.append("\n## Constants\n")
    out.append("\n| Name | Value | Defined in |\n|---|---|---|\n")
    for name, spec in m["constants"].items():
        value = spec["value"]
        if isinstance(value, dict):
            shown = f"({len(value)} entries)"
        else:
            shown = f"`{value}`"
        out.append(f"| `{name}` | {shown} | `{spec['module']}` |\n")

    out.append("\n## Derived facts\n")
    out.append(
        "\nFacts about the shapes above that live outside `contracts/`, and every\n"
        "site that restates one. `tests/unit/test_single_source.py` holds the copies to\n"
        "the canonical value.\n"
    )
    for name, fact in m["derived"].items():
        out.append(f"\n### `{name}`\n\n")
        out.append(f"- **Canonical**: `{fact['canonical']}` = {_fmt_value(fact['value'])}\n")
        out.append(f"- **Why not in `contracts/`**: {fact['why']}\n")
        if fact["copies"]:
            out.append(f"- **Checked copies**: {', '.join(f'`{c}`' for c in fact['copies'])}\n")
        if fact["restated_at"]:
            out.append(
                f"- **Restated (not checkable)**: {', '.join(f'`{c}`' for c in fact['restated_at'])}\n"
            )

    out.append("\n## HTTP surface\n")
    out.append(f"\n### Coordinator gateway ({len(r['gateway'])} routes)\n\n")
    out.append("| Method | Path |\n|---|---|\n")
    for route in r["gateway"]:
        out.append(f"| {', '.join(route['methods'])} | `{route['path']}` |\n")
    out.append(f"\n### Node agent ({len(r['agent'])} routes)\n\n")
    out.append("| Method | Path |\n|---|---|\n")
    for route in r["agent"]:
        out.append(f"| {', '.join(route['methods'])} | `{route['path']}` |\n")

    out.append("\n## Environment\n")
    out.append(
        "\nEvery variable the tree reads. `tests/unit/test_single_source.py` fails on one\n"
        "that is read and not declared here, and on two readers disagreeing about a\n"
        "default. A blank default means absence is itself the answer.\n"
    )
    out.append("\n| Variable | Default | Owner |\n|---|---|---|\n")
    for name, spec in m["env"].items():
        default = f"`{spec['default']}`" if spec["default"] is not None else ""
        note = f" — {spec['note']}" if spec["note"] else ""
        out.append(f"| `{name}` | {default} | `{spec['owner']}`{note} |\n")

    return "".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="rewrite CONTRACTS.md")
    args = parser.parse_args(argv)

    text = render()
    if args.write:
        DOCUMENT_PATH.write_text(text, encoding="utf-8")
        print(f"wrote {DOCUMENT_PATH}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a CLI
    raise SystemExit(main())
