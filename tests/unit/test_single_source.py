"""One fact, one home, and a red test when a copy stops agreeing.

``tests/unit/test_contracts.py`` pins the shapes. This pins everything derived from
them -- which is where the drift actually happened, because a shape has one
obvious home and a fact about a shape has none.

The pattern is not new here. ``test_modelcache.py`` already asserts that
``gateway.internal_api._TERMINAL_STATES`` equals ``deploy.fsm.TERMINAL``,
because that copy was made deliberately (a lazy import, for a reason its
comment gives) and somebody knew a deliberate copy still drifts. That single
assertion is generalised here: ``control_plane/contracts/derived.py`` names
every such fact and every site that restates it, and this file does the
comparing.

Extend it by adding a row to ``derived.py``, not by adding an assertion here.

The second half does the same for environment variables, which have no import
graph at all: a name is a string in four languages, and nothing fails when two
of them disagree.
"""

from __future__ import annotations

import ast
import collections
import pathlib
import re

import pytest

from control_plane import envspec
from control_plane.contracts import derived
from control_plane.contracts.manifest import resolve

REPO = pathlib.Path(__file__).resolve().parents[2]

#: Directories that are not source: build output, caches, dependencies, and the
#: scratch trees the UI verifiers leave behind.
_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", "dist", ".pytest_cache", "build",
    ".venv", "venv", ".mypy_cache", ".ruff_cache",
}

#: Where a variable can be read from. Markdown is deliberately excluded: a
#: document naming a variable that no longer exists is a documentation bug, and
#: failing this test on it would put the two problems in one place.
_SOURCE_SUFFIXES = {".py", ".sh", ".ts", ".tsx", ".mjs", ".yaml", ".yml"}
_SOURCE_NAMES = {"Dockerfile", "install.sh", "entrypoint.sh"}

_ENV_NAME = re.compile(r"\bDERATE_[A-Z0-9_]+\b")


def _source_files() -> list[pathlib.Path]:
    out = []
    for path in REPO.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(REPO)
        if any(part in _SKIP_DIRS or part.startswith(".history-check") for part in rel.parts):
            continue
        if path.suffix in _SOURCE_SUFFIXES or path.name in _SOURCE_NAMES:
            out.append(path)
    return out


# ---------------------------------------------------------------------------
# Derived facts: the canonical value, and everywhere it is copied
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fact", derived.FACTS, ids=lambda f: f.name)
def test_the_canonical_definition_resolves(fact):
    """A row naming a home that no longer exists is worse than no row."""
    resolve(fact.canonical)


def _members(value):
    if isinstance(value, dict):
        return set(value)
    if isinstance(value, (set, frozenset, tuple, list)):
        return set(value)
    return {value}


_COPIES = [
    (fact, copy) for fact in derived.FACTS for copy in fact.copies
]


@pytest.mark.parametrize(
    "fact,copy", _COPIES, ids=[f"{f.name}->{c}" for f, c in _COPIES]
)
def test_every_copy_still_agrees_with_its_canonical_definition(fact, copy):
    canonical = resolve(fact.canonical)
    other = resolve(copy)

    if fact.compare == "members":
        assert _members(other) == _members(canonical), (
            f"\n\n{copy} no longer names the same things as {fact.canonical}.\n"
            f"  only in {fact.canonical}: {_members(canonical) - _members(other)}\n"
            f"  only in {copy}: {_members(other) - _members(canonical)}\n\n"
            f"{fact.canonical} is the one that decides. Also check: "
            f"{', '.join(fact.restated_at) or '(nothing else listed)'}"
        )
    else:
        assert other == canonical, (
            f"\n\n{copy} has drifted from {fact.canonical}.\n"
            f"  {fact.canonical} = {canonical!r}\n"
            f"  {copy} = {other!r}\n\n"
            f"{fact.canonical} is the one that decides. Also check: "
            f"{', '.join(fact.restated_at) or '(nothing else listed)'}"
        )


def test_the_terminal_state_assertion_that_started_this_still_holds():
    """The seed. ``test_modelcache.py`` has asserted this since the copy was
    made; it is repeated here so that deleting that test does not silently
    remove the only check on the oldest known duplicate."""
    from control_plane.deploy import fsm
    from control_plane.gateway.internal_api import _TERMINAL_STATES

    assert _TERMINAL_STATES == fsm.TERMINAL


# ---------------------------------------------------------------------------
# Environment variables: no import graph, so a grep is the import graph
# ---------------------------------------------------------------------------


def test_every_environment_variable_in_the_tree_is_declared():
    undeclared: dict[str, set[str]] = collections.defaultdict(set)
    for path in _source_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for name in _ENV_NAME.findall(text):
            if not envspec.is_declared(name):
                undeclared[name].add(str(path.relative_to(REPO)))

    assert not undeclared, (
        "\n\nEnvironment variables nobody declared:\n"
        + "\n".join(f"  {n}  ({', '.join(sorted(f))})" for n, f in sorted(undeclared.items()))
        + "\n\nAdd them to control_plane/envspec.py -- name, default and the file "
        "that owns the read. If the name is a family rather than a variable "
        "(a per-provider key reference, a per-node address), add a DYNAMIC "
        "pattern instead.\n"
    )


def _python_env_defaults() -> dict[str, set[tuple[str, str]]]:
    """Every ``environ.get(<a derate name>, <default>)`` in the Python tree.

    Spelled that way on purpose: a literal example name in this docstring would
    be found by the scan above and reported as an undeclared variable.
    """
    found: dict[str, set[tuple[str, str]]] = collections.defaultdict(set)

    class Visitor(ast.NodeVisitor):
        def __init__(self, rel: str) -> None:
            self.rel = rel

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in ("get", "getenv"):
                first = node.args[0] if node.args else None
                if (
                    isinstance(first, ast.Constant)
                    and isinstance(first.value, str)
                    and first.value.startswith("DERATE_")
                    and len(node.args) > 1
                ):
                    default = node.args[1]
                    text = (
                        default.value
                        if isinstance(default, ast.Constant) and isinstance(default.value, str)
                        else ast.unparse(default)
                    )
                    found[first.value].add((text, self.rel))
            self.generic_visit(node)

    for path in _source_files():
        if path.suffix != ".py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:  # pragma: no cover - a peer's file mid-edit
            continue
        Visitor(str(path.relative_to(REPO))).visit(tree)
    return found


def test_no_two_readers_of_a_variable_disagree_about_its_default():
    """Two defaults for one name is a silent behavioural fork.

    ``docker/placeholder_app.py`` defaulted ``DERATE_AGENT_PORT`` to whatever
    ``DERATE_PORT`` was while the Dockerfile and ``registry/config.py`` both
    said 8081, so an operator who named only the coordinator port got an agent
    on top of it and no error either way.
    """
    forks = {
        name: sites
        for name, sites in _python_env_defaults().items()
        if len({default for default, _ in sites}) > 1
    }
    assert not forks, "\n\n" + "\n".join(
        f"{name} is read with {len({d for d, _ in sites})} different defaults:\n"
        + "\n".join(f"    {default!r:24} {where}" for default, where in sorted(sites))
        for name, sites in sorted(forks.items())
    ) + "\n"


def test_declared_defaults_match_what_the_code_actually_falls_back_to():
    wrong = []
    for name, sites in sorted(_python_env_defaults().items()):
        var = envspec.VARIABLES.get(name)
        if var is None or var.default is None:
            continue
        for default, where in sorted(sites):
            if default != var.default:
                wrong.append((name, var.default, default, where))

    assert not wrong, (
        "\n\ncontrol_plane/envspec.py disagrees with the code:\n"
        + "\n".join(
            f"  {name}: declared {declared!r}, but {where} falls back to {actual!r}"
            for name, declared, actual, where in wrong
        )
        + "\n\nThe code is right and the declaration is the copy. Fix envspec.py.\n"
    )
