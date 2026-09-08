"""What could actually run here, right now.

``FitCalculator.check`` answers "does this model fit". This inverts it: given
the hardware as it is at this moment, which of these models runs, at what
quantization, and which is the largest of them.

It walks the quantization ladder per model and takes the best-quality scheme
that fits, running a real ``check()`` at each rung rather than
``_suggest_quant``'s weights-only estimate. That estimate holds every other
term fixed, which is right for a *suggestion* attached to a refusal and wrong
for a verdict: KV, activations and comm buffers all move with the plan.

Two honesty rules are structural here:

- **A measured ``weight_bytes`` is only valid for the dtype it was measured
  at.** The moment the ladder substitutes a scheme, the measured total is
  dropped and the formula takes over, and the row says so. Pricing q4_k_m
  weights from measured bf16 bytes would fabricate exactly the sort of number
  ``weight_bytes`` was introduced to remove.
- **"Largest" is reported with its decode rate.** Otherwise the largest model
  that runs is silently one that loads and decodes at 3 tok/s.
"""

from __future__ import annotations

import dataclasses
import inspect
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from control_plane.contracts import (
    FitRequest,
    ModelShape,
    NodeProfile,
    ParallelismKind,
    ParallelismPlan,
    Verdict,
)

from .calculator import FitCalculator
from .constants import CONTEXT_ROUNDING, QUANT_SUGGESTION_ORDER

log = logging.getLogger("fit.capacity")


@dataclass(frozen=True)
class CapacityRow:
    """One model's answer, under one budget."""

    model_id: str
    label: str
    total_params: int
    native_dtype: str
    dtype: str | None
    requantized: bool
    verdict: str
    total: int | None
    headroom: int | None
    predicted_decode_tps: float | None
    reason: str
    warnings: list[str]
    #: What this row was actually judged at. Defaulted because every existing
    #: caller passes a context in and gets it straight back; they matter when
    #: the caller passes None and the context is derived per model, at which
    #: point one number at the top of a report no longer describes every row
    #: in it.
    context: int = 0
    max_seqs: int = 1

    @property
    def fits(self) -> bool:
        return self.verdict in ("fits", "fits_degraded")


def _single_node_plan(node_ids: list[str]) -> ParallelismPlan:
    """The shape a capacity question is asked in: one node, no sharding.

    Capacity here means "what runs on this machine as it stands", not "what
    could run if we replanned the cluster around it" -- that is the planner's
    question and it needs a target and a link measurement to answer.
    """
    return ParallelismPlan(
        kind=ParallelismKind.SINGLE_NODE,
        tensor_parallel=1,
        pipeline_parallel=1,
        expert_parallel=1,
        data_parallel=1,
        node_ids=node_ids,
        reason="capacity probe: one node, no sharding",
        measured_link_gbps=0.0,
        rejected=[],
    )


def probe_plan(node_ids: list[str], tensor_parallel: int) -> ParallelismPlan:
    """The shape a capacity question is asked in across several machines.

    The sibling of ``_single_node_plan`` and the same kind of object: a
    placement stated, not searched for. The caller has already named the
    machines and the degree -- typically the widest tensor-parallel degree the
    model legally admits over them -- so there is nothing here to optimise.

    ``measured_link_gbps`` stays 0.0 for the same reason it does on the
    single-node probe: this is not a planner result and must not be mistaken
    for one. A real launch goes through ``POST /api/plan``, which has a target
    and a link measurement; this answers "would it fit on these", which is a
    memory question and needs neither.
    """
    if tensor_parallel <= 1:
        return _single_node_plan(node_ids[:1])
    return ParallelismPlan(
        kind=ParallelismKind.TENSOR,
        tensor_parallel=tensor_parallel,
        pipeline_parallel=1,
        expert_parallel=1,
        data_parallel=1,
        node_ids=node_ids[:tensor_parallel],
        reason=(
            f"capacity probe: tensor-parallel across "
            f"{tensor_parallel} named machines"
        ),
        measured_link_gbps=0.0,
        rejected=[],
    )


def largest_runnable(
    shapes: Sequence[tuple[ModelShape, str, int | None]],
    nodes: list[NodeProfile],
    *,
    context: int | None,
    max_seqs: int,
    kv_dtype: str,
    allocatable: Mapping[str, int] | None = None,
    ladder: Sequence[str] = QUANT_SUGGESTION_ORDER,
    calculator: FitCalculator | None = None,
    plan_for: Callable[[ModelShape], ParallelismPlan] | None = None,
    native_context: Mapping[str, int | None] | None = None,
) -> tuple[list[CapacityRow], CapacityRow | None]:
    """(one row per shape, the largest that fits).

    ``shapes`` is (shape, label, measured weight bytes or None). ``allocatable``
    given, every verdict is taken against the live budget; omitted, against the
    static ceiling.

    ``plan_for`` given, every verdict is taken against the placement it
    returns for that shape -- which is how a caller asks "what runs on the
    machines I ticked" rather than "what runs on one machine". Per shape and
    not one fixed plan, because the legal degrees are a property of the model:
    tensor parallelism has to divide the KV heads, so the same two machines
    take one model at TP=2 and another at TP=1. Omitted, it is the single-node
    probe this has always been, on the first node of ``nodes``.

    ``context`` may be None, meaning "choose one per model" -- see
    ``ladder_context``. ``native_context`` then supplies each model's own
    window, keyed by the row LABEL rather than the model id, because the
    variant ladder walks one model under several labels.
    """
    calc = calculator or FitCalculator()
    if not nodes:
        return [], None

    default_plan = _single_node_plan([nodes[0].node_id])
    natives = native_context or {}
    rows: list[CapacityRow] = []

    for shape, label, weight_bytes in shapes:
        plan = plan_for(shape) if plan_for else default_plan
        row = _walk(
            calc, shape, label, weight_bytes, nodes, plan,
            context, max_seqs, kv_dtype, allocatable, ladder,
            natives.get(label),
        )
        rows.append(row)

    fitting = [r for r in rows if r.fits]
    # Largest by parameter count -- the honest reading of "largest model" --
    # with active parameters breaking a tie, since that is what decides
    # whether it is usable once loaded.
    best = max(fitting, key=lambda r: r.total_params) if fitting else None
    return rows, best


def _rungs(
    shape: ModelShape, ladder: Sequence[str], weight_bytes: int | None
) -> list[tuple[ModelShape, str, int | None]]:
    """The quantization schemes to try, best quality first.

    The native dtype, then every scheme BELOW it in the ladder. Only ever
    downward: offering to run a model at a larger dtype than it ships in is
    not a capacity answer.

    One list, built once, so the context derivation and the verdict walk can
    never disagree about which rungs exist -- a derivation that priced a rung
    the walk does not try would produce a context nothing was judged at.

    The measured on-disk total belongs to the native dtype alone, so every
    other rung carries None and the formula takes over.
    """
    out = [(shape, shape.dtype, weight_bytes)]
    seen_native = False
    for name in ladder:
        if name == shape.dtype:
            seen_native = True
            continue
        if not seen_native:
            continue
        out.append((dataclasses.replace(shape, dtype=name), name, None))
    return out


#: The context to assume for a model whose own window we could not read.
#:
#: Exactly what ``GET /api/capacity`` defaulted to before the context became
#: derivable, so a model with no ``max_position_embeddings`` is judged today
#: the way it was judged yesterday -- lower if that does not fit, never
#: higher. Reaching for ``MAX_CONTEXT_SEARCH`` instead would print a
#: two-million-token window for a 0.5B model and call it a choice.
FALLBACK_CONTEXT = 8192

#: Below this a context is not worth trading quality for.
#:
#: A judgement call, and the only one in this file -- stated as a number so it
#: can be argued with rather than buried in a comparison. It exists because
#: the ladder walk and the context search pull in opposite directions: a
#: smaller dtype always buys more context, so "the rung with the most context"
#: is always the most degraded rung on the ladder, and "the best rung that
#: fits at all" is always the native one at a context too small to use. Neither
#: is an answer. This is the line between them: take the best quality that
#: still leaves a context somebody could work in.
MIN_USEFUL_CONTEXT = 4096


def _clamp_context(largest: int, native_window: int | None) -> int:
    """The context rule itself: the model's own window, or what fits, whichever
    is smaller.

    Both halves are load-bearing. Without the window, a small model on a big
    machine is reported at a context its runtime would refuse to start with,
    and the ceiling of the search (2,097,152 tokens) is not a window anybody
    asked for. Without the fit figure it is not a measurement at all.
    """
    ceiling = native_window if native_window and native_window > 0 else FALLBACK_CONTEXT
    return min(ceiling, largest) if largest > 0 else ceiling


def context_for(
    fit: Any,
    shape: ModelShape,
    plan: ParallelismPlan,
    nodes: list[NodeProfile],
    *,
    max_seqs: int,
    kv_dtype: str,
    weight_bytes: int | None = None,
    allocatable: Mapping[str, int] | None = None,
    native_window: int | None = None,
) -> int:
    """The context to judge one model at, when the caller named none.

    ``min(the model's own window, the largest context that fits)``, on a
    ``CONTEXT_ROUNDING`` grain, never above what the gate verified. THE rule --
    the planning path and the capacity probe both call this rather than each
    keeping a copy, because two implementations of "what context did we
    choose" is two answers to the question every verdict on screen is taken at.

    ``fit`` is a ``FitPort``, not necessarily a ``FitCalculator``: the extra
    ``weight_bytes`` and ``allocatable`` arguments are additive deviations from
    the frozen 4.7 signature, so a port written against the original is probed
    and called with what it accepts rather than raising a TypeError at request
    time. A signature probe and not ``try/except TypeError``, which would also
    swallow a genuine TypeError raised inside the port.

    Falls back to ``FALLBACK_CONTEXT`` when the port cannot answer at all --
    never to 0, which would be a refusal dressed as a choice.
    """
    solve = getattr(fit, "max_context", None)
    if not callable(solve):
        # A port that cannot solve for a context cannot be asked to choose
        # one. `FitPort` declares `max_context`, but the gateway composes ports
        # it does not own -- including the "fit is unavailable" stub, whose
        # whole job is to have nothing on it -- and a missing method here must
        # degrade to the number the screen used to ask at, not take the request
        # down. The launch path refuses separately and loudly when there is no
        # fit gate at all; that refusal is not this function's to pre-empt.
        return _clamp_context(0, native_window)

    kwargs: dict[str, Any] = {}
    try:
        params = inspect.signature(solve).parameters
    except (TypeError, ValueError):  # builtins, C callables, exotic proxies
        params = {}
    varkw = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    if weight_bytes is not None and ("weight_bytes" in params or varkw):
        kwargs["weight_bytes"] = weight_bytes
    if allocatable is not None and ("allocatable" in params or varkw):
        kwargs["allocatable"] = allocatable

    try:
        largest = solve(shape, plan, nodes, max_seqs, kv_dtype, **kwargs)
    except Exception:
        log.exception("context derivation failed for %s", shape.model_id)
        return FALLBACK_CONTEXT

    # A zero comes back as the model's own window rather than as 0: there is
    # no lower rung to fall to on this path, so the gate must be allowed to
    # refuse with its own sentence -- naming the term and the overflow --
    # rather than being handed a number invented here that pretends to be a
    # choice somebody made.
    return _clamp_context(largest, native_window)


def ladder_context(
    rungs: Sequence[tuple[ModelShape, str, int | None]],
    nodes: list[NodeProfile],
    plan: ParallelismPlan,
    *,
    max_seqs: int,
    kv_dtype: str,
    allocatable: Mapping[str, int] | None = None,
    native_window: int | None = None,
    calculator: FitCalculator | None = None,
) -> tuple[int, str | None]:
    """(the context to judge a whole ladder at, the rung it was derived on).

    ``rungs`` is (shape, name, measured weight bytes), best quality FIRST. The
    rule:

      the best-quality scheme that still leaves a workable context, judged at
      ``min(the model's own window, the largest context that scheme holds)``

    ONE context for the whole ladder, and NOT re-derived per rung. Per-rung
    derivation looks like the obvious implementation and quietly destroys the
    ladder. Two ways, both seen:

    - Walking DOWN a quantization ladder, ``max_context`` returns the largest
      context that fits, so every rung fits by construction -- bf16 at 512
      tokens "fits" -- the native dtype always wins, and a ladder that always
      recommends the native dtype has stopped answering the question it exists
      for.
    - Judging a LIST of published variants, every row gets its own context and
      the column stops being comparable: "fits at 31744" beside "fits at
      32256" is two answers to two questions printed as one table, and the
      headroom figures beside them cannot be read against each other at all.

    The two clamps are both load-bearing and pull opposite ways:

    - ``MIN_USEFUL_CONTEXT`` is the floor a rung must clear to be chosen.
      Without it the native dtype wins at 512 tokens.
    - the model's own window is the ceiling. Without it a small model on a big
      machine is reported at a context its runtime would refuse to start with,
      and the ceiling of the search (2,097,152) is not a window anybody asked
      for.

    A rung that does not fit AT ALL is skipped outright, before either clamp.
    ``max_context`` returns 0 for it, and ``_clamp_context`` maps that 0 to the
    model's own window -- correct on the single-model path, where there is no
    lower rung to fall to and the gate must be left to refuse in its own words,
    but wrong here, where the loop exists precisely to fall further. Without
    the skip the first rung always wins: a bf16 rung refused for its weights
    alone reports the model's full native window, clears
    ``MIN_USEFUL_CONTEXT`` trivially, and hands that context to every rung
    below it -- which is how a ladder of 13 GiB variants came to be judged at
    262,144 tokens and refused, one and all, on KV cache.

    Returns the rung's dtype alongside the number so the caller can say which
    scheme the context was chosen for; None when nothing here holds this model
    on this hardware, in which case the context is the search grain and the
    walk that follows produces the gate's own refusal -- naming the term and
    the overflow -- rather than a sentence invented in this function.
    """
    calc = calculator or FitCalculator()
    best_effort: tuple[int, str] | None = None

    for candidate, name, wb in rungs:
        try:
            largest = calc.max_context(
                candidate, plan, nodes, max_seqs, kv_dtype,
                weight_bytes=wb, allocatable=allocatable,
            )
        except Exception:
            log.exception("context derivation failed for %s", candidate.model_id)
            continue
        # A rung that holds NOTHING is not a candidate, and the check has to
        # happen here -- on the raw figure -- because `_clamp_context` maps 0 to
        # the model's own window, which is indistinguishable from a rung that
        # genuinely holds every token the model can address. Clamping first is
        # what let a 48 GiB bf16 rung, refused outright, hand a 262,144-token
        # context to a whole ladder of 13 GiB variants that would each have fit
        # at a workable one.
        if largest <= 0:
            continue
        chosen = _clamp_context(largest, native_window)
        if chosen >= MIN_USEFUL_CONTEXT:
            return chosen, name
        # Holds the weights but not a context worth having. Remember the
        # best-quality one of these in case no rung clears the floor -- a
        # cramped answer beats refusing to answer.
        if best_effort is None:
            best_effort = (chosen, name)

    if best_effort is not None:
        return best_effort
    return CONTEXT_ROUNDING, None


def _walk(
    calc: FitCalculator,
    shape: ModelShape,
    label: str,
    weight_bytes: int | None,
    nodes: list[NodeProfile],
    plan: ParallelismPlan,
    context: int | None,
    max_seqs: int,
    kv_dtype: str,
    allocatable: Mapping[str, int] | None,
    ladder: Sequence[str],
    native_window: int | None = None,
) -> CapacityRow:
    native = shape.dtype
    if context is None:
        context, _rung = ladder_context(
            _rungs(shape, ladder, weight_bytes), nodes, plan,
            max_seqs=max_seqs, kv_dtype=kv_dtype, allocatable=allocatable,
            native_window=native_window, calculator=calc,
        )

    def check(candidate: ModelShape, wb: int | None):
        req = FitRequest(
            shape=candidate,
            context_length=context,
            max_concurrent_seqs=max_seqs,
            kv_dtype=kv_dtype,
            plan=plan,
            weight_bytes=wb,
            native_window=native_window,
        )
        return calc.check(req, nodes, allocatable=allocatable)

    rungs = _rungs(shape, ladder, weight_bytes)
    native_shape, _native_name, native_wb = rungs[0]

    try:
        first = check(native_shape, native_wb)
    except Exception:
        log.exception("capacity check failed for %s", shape.model_id)
        return CapacityRow(
            shape.model_id, label, shape.total_params, native, None, False,
            "error", None, None, None,
            "The fit gate could not be run for this model.", [],
            context, max_seqs,
        )

    if first.verdict is not Verdict.WONT_FIT:
        return _row(
            shape, label, native, native, False, first, [], context, max_seqs
        )

    # Down the ladder, best quality first, stopping at the first that fits.
    for candidate, name, wb in rungs[1:]:
        try:
            result = check(candidate, wb)
        except Exception:
            log.exception("capacity rung %s failed for %s", name, shape.model_id)
            continue
        if result.verdict is not Verdict.WONT_FIT:
            return _row(
                shape, label, native, name, True, result,
                [
                    f"weights priced from the dtype formula, not the measured "
                    f"checkpoint: {name} bytes for this model have not been "
                    f"measured"
                ],
                context, max_seqs,
            )

    return _row(
        shape, label, native, None, False, first, [], context, max_seqs
    )


def _row(
    shape, label, native, dtype, requantized, result, extra, context, max_seqs
) -> CapacityRow:
    return CapacityRow(
        model_id=shape.model_id,
        label=label,
        total_params=shape.total_params,
        native_dtype=native,
        dtype=dtype,
        requantized=requantized,
        verdict=result.verdict.value,
        total=result.breakdown.total,
        headroom=result.headroom,
        predicted_decode_tps=result.predicted_decode_tps,
        reason=result.reason,
        warnings=list(result.warnings) + list(extra),
        context=context,
        max_seqs=max_seqs,
    )
