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
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from control_plane.contracts import (
    FitRequest,
    ModelShape,
    NodeProfile,
    ParallelismKind,
    ParallelismPlan,
    Verdict,
)

from .calculator import FitCalculator
from .constants import QUANT_SUGGESTION_ORDER

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


def largest_runnable(
    shapes: Sequence[tuple[ModelShape, str, int | None]],
    nodes: list[NodeProfile],
    *,
    context: int,
    max_seqs: int,
    kv_dtype: str,
    allocatable: Mapping[str, int] | None = None,
    ladder: Sequence[str] = QUANT_SUGGESTION_ORDER,
    calculator: FitCalculator | None = None,
) -> tuple[list[CapacityRow], CapacityRow | None]:
    """(one row per shape, the largest that fits).

    ``shapes`` is (shape, label, measured weight bytes or None). ``allocatable``
    given, every verdict is taken against the live budget; omitted, against the
    static ceiling.
    """
    calc = calculator or FitCalculator()
    if not nodes:
        return [], None

    plan = _single_node_plan([nodes[0].node_id])
    rows: list[CapacityRow] = []

    for shape, label, weight_bytes in shapes:
        row = _walk(
            calc, shape, label, weight_bytes, nodes, plan,
            context, max_seqs, kv_dtype, allocatable, ladder,
        )
        rows.append(row)

    fitting = [r for r in rows if r.fits]
    # Largest by parameter count -- the honest reading of "largest model" --
    # with active parameters breaking a tie, since that is what decides
    # whether it is usable once loaded.
    best = max(fitting, key=lambda r: r.total_params) if fitting else None
    return rows, best


def _walk(
    calc: FitCalculator,
    shape: ModelShape,
    label: str,
    weight_bytes: int | None,
    nodes: list[NodeProfile],
    plan: ParallelismPlan,
    context: int,
    max_seqs: int,
    kv_dtype: str,
    allocatable: Mapping[str, int] | None,
    ladder: Sequence[str],
) -> CapacityRow:
    native = shape.dtype

    def check(candidate: ModelShape, wb: int | None):
        req = FitRequest(
            shape=candidate,
            context_length=context,
            max_concurrent_seqs=max_seqs,
            kv_dtype=kv_dtype,
            plan=plan,
            weight_bytes=wb,
        )
        return calc.check(req, nodes, allocatable=allocatable)

    try:
        first = check(shape, weight_bytes)
    except Exception:
        log.exception("capacity check failed for %s", shape.model_id)
        return CapacityRow(
            shape.model_id, label, shape.total_params, native, None, False,
            "error", None, None, None,
            "The fit gate could not be run for this model.", [],
        )

    if first.verdict is not Verdict.WONT_FIT:
        return _row(shape, label, native, native, False, first, [])

    # Down the ladder, best quality first, stopping at the first that fits.
    seen_native = False
    for name in ladder:
        if name == native:
            seen_native = True
            continue
        # Only ever go DOWN from the native precision: offering to run a
        # model at a *larger* dtype than it ships in is not a capacity answer.
        if not seen_native:
            continue
        candidate = dataclasses.replace(shape, dtype=name)
        try:
            # The measured on-disk total belongs to the native dtype alone.
            result = check(candidate, None)
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
            )

    return _row(shape, label, native, None, False, first, [])


def _row(shape, label, native, dtype, requantized, result, extra) -> CapacityRow:
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
    )
