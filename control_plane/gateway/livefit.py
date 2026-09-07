"""The live-memory budget, and the seam that makes it safe to ask for.

The fit gate budgets against a static ceiling: what the hardware could spend
with nothing else running. On a unified-memory part that ceiling is not
reachable while an operating system -- or any process this control plane did
not start -- is spending from the same pool, so a check against it approves
models that will not load.

This module supplies the live figure and calls the gate twice: once on the
static ceiling (the answer to "would this fit on an idle machine") and once on
what the node can actually hand out (the answer the Serve button needs). Both
verdicts go on the wire; the backend, not the UI, decides which one governs.

Two rules run through everything here:

- **Absence is never zero.** A registry that cannot answer, a node with no
  telemetry yet, or a fit port that predates the parameter all degrade to the
  static verdict. A cold coordinator must not refuse every launch.
- **Never fabricate the live answer.** When it cannot be computed, `live` is
  None and `unavailable_reason` says why, rather than a number nobody measured.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Mapping
from typing import Any

from control_plane.contracts import FitRequest, FitResult, NodeProfile, Verdict

log = logging.getLogger("gateway.livefit")

#: Reported as ``serve.basis``.
BASIS_LIVE = "live"
BASIS_STATIC = "static"


def _accepts_allocatable(check: Any) -> bool:
    """Whether this FitPort's ``check`` takes the additive keyword.

    The gateway composes ports it does not own, and the parameter is an
    additive deviation from the frozen 4.7 signature. A port written against
    the original is not broken -- it simply cannot answer the live question,
    and the caller degrades rather than raising a TypeError at request time.

    Deliberately a signature probe and not ``try/except TypeError`` around the
    call: that would also swallow a genuine TypeError raised *inside* check(),
    turning a real bug into a silent fallback to the budget we are trying to
    stop trusting.
    """
    try:
        sig = inspect.signature(check)
    except (TypeError, ValueError):  # builtins, C callables, exotic proxies
        return False
    for param in sig.parameters.values():
        if param.name == "allocatable":
            return True
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


def allocatable_map(
    registry: Any, node_ids: list[str]
) -> tuple[dict[str, int], list[dict[str, str]], str | None]:
    """node_id -> live allocatable bytes, plus what we had to exclude.

    Duck-typed on ``Registry.available_memory``, exactly as this gateway
    already duck-types resolve_full, supported_by, candidates, handle_join and
    admit. A registry without it (either stub) yields an empty map, so every
    existing caller keeps today's static answer unchanged.

    Returns ``(budgets, excluded, unavailable_reason)``. ``excluded`` carries
    one entry per node dropped from the budget and why, so a refusal can name
    the exclusion instead of silently refusing everything.
    """
    available = getattr(registry, "available_memory", None)
    if not callable(available):
        return {}, [], "this registry does not report live allocatable memory"

    budgets: dict[str, int] = {}
    excluded: list[dict[str, str]] = []
    for node_id in node_ids:
        try:
            value = available(node_id)
        except Exception:
            log.exception("available_memory failed for %s", node_id)
            excluded.append(
                {"node_id": node_id, "reason": "the live memory read failed"}
            )
            continue
        if value is None:
            excluded.append(
                {"node_id": node_id, "reason": "no telemetry sample yet"}
            )
            continue
        budgets[node_id] = int(value)

    if not budgets:
        return {}, excluded, "no node could report live allocatable memory"
    return budgets, excluded, None


def drop_zero_addressable(
    nodes: list[NodeProfile], budgets: dict[str, int]
) -> tuple[dict[str, int], list[dict[str, str]]]:
    """Remove nodes that report no addressable memory at all.

    A node whose profile says zero addressable bytes -- a container that could
    not probe its GPU, say -- would otherwise be the argmin of every budget and
    make every verdict WONT_FIT. Dropping it silently would be its own lie, so
    each removal is recorded and surfaces in the refusal.
    """
    by_id = {n.node_id: n for n in nodes}
    kept: dict[str, int] = {}
    excluded: list[dict[str, str]] = []
    for node_id, value in budgets.items():
        profile = by_id.get(node_id)
        if profile is not None and profile.addressable_memory <= 0:
            excluded.append(
                {
                    "node_id": node_id,
                    "reason": "reports 0 addressable bytes and was excluded "
                    "from the memory budget",
                }
            )
            continue
        kept[node_id] = value
    return kept, excluded


def dual_check(
    fit: Any,
    req: FitRequest,
    nodes: list[NodeProfile],
    budgets: Mapping[str, int] | None,
) -> tuple[FitResult | None, FitResult | None, str | None]:
    """(static verdict, live verdict, why live is missing).

    Never raises for a port that predates the parameter, and never invents a
    live verdict: when one cannot be produced the second element is None and
    the third says why, so the caller degrades to the static answer knowingly.
    """
    try:
        static = fit.check(req, nodes)
    except Exception:
        log.exception("static fit check failed")
        raise

    if not budgets:
        return static, None, "no live allocatable reading for these nodes"
    if not _accepts_allocatable(fit.check):
        return static, None, "this fit port does not accept a live memory budget"

    try:
        live = fit.check(req, nodes, allocatable=budgets)
    except Exception:
        log.exception("live fit check failed")
        return static, None, "the live fit check failed"
    return static, live, None


def serve_decision(
    static: FitResult | None,
    live: FitResult | None,
    unavailable_reason: str | None,
    *,
    override_param: str = "allow_over_live_memory",
    extra_gates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """What the button is allowed to do, decided here rather than in the UI.

    The UI reads exactly one field. Handing it two verdicts and asking it to
    pick would put the choice of which budget governs a launch in the client,
    which is how the two answers drift apart again.

    ``extra_gates`` are permissions that are not about memory at all -- pooling
    unlike hardware is the first. They are why ``overrides`` exists alongside
    the older ``override_required``/``override_param`` pair: a launch can need a
    tick while the fit itself passes, and ``allowed: false`` cannot express
    that. ``overrides`` is the complete list when present, the live-memory gate
    included; the two legacy fields stay as a mirror of its first entry so a
    client that predates them still reads a correct single gate.
    """
    governing = live if live is not None else static
    basis = BASIS_LIVE if live is not None else BASIS_STATIC
    extra_gates = list(extra_gates or [])

    if governing is None:
        return {
            "allowed": False,
            "verdict": None,
            "basis": basis,
            "reason": "the fit gate did not answer, so nothing has checked "
            "whether this fits",
            "override_required": False,
            "override_param": None,
            "unavailable_reason": unavailable_reason,
            # No verdict means no launch, and no tick can produce one. An empty
            # list here is the honest answer, not the absent field.
            "overrides": [],
        }

    refused = governing.verdict is Verdict.WONT_FIT
    # An override only exists for a *live* refusal. A static WONT_FIT means the
    # model does not fit this hardware at all, and no amount of waiting or
    # operator insistence changes that.
    overridable = refused and live is not None

    gates: list[dict[str, Any]] = []
    if overridable:
        gates.append({"param": override_param, "reason": governing.reason})
    gates.extend(extra_gates)

    # A static refusal is not overridable, so a launch that is refused outright
    # must not advertise gates that could never unblock it.
    if refused and not overridable:
        gates = []

    return {
        # `allowed` keeps its old meaning exactly: the fit gate passed. It is
        # no longer sufficient on its own -- a caller must also satisfy every
        # entry in `overrides` -- which is why the extra gates do not touch it.
        "allowed": not refused,
        "verdict": governing.verdict.value,
        "basis": basis,
        "reason": governing.reason,
        "override_required": bool(gates) and gates[0]["param"] == override_param,
        "override_param": (
            override_param
            if gates and gates[0]["param"] == override_param
            else None
        ),
        "unavailable_reason": unavailable_reason,
        "overrides": gates,
    }
