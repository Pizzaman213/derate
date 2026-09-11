"""Absence means "choose one", for concurrency as well as for context.

`internal_api._plan_and_fit` opens with three lines explaining why a context
nobody named must be derived once the plan and the machines are known -- "a
context picked before the placement is a number, not a fit". The very next
line read `int(payload.get("concurrency") or 1)`.

So every deployment on this cluster launched `--max-num-seqs 1`. Decode is
bandwidth bound: the weights are read once per step whatever the batch holds,
and at one sequence that read produces one token.
"""

from __future__ import annotations

import dataclasses

from control_plane.fit.calculator import FitCalculator
from control_plane.fit.capacity import (
    MAX_DERIVED_CONCURRENCY,
    MIN_USEFUL_CONTEXT,
    concurrency_for,
)
from tests.fixtures import GPT_OSS_120B, LLAMA_3_3_70B, QWEN3_30B_A3B, SPARK_01
from tests.unit.test_fit import make_plan


def derive(shape, nodes=None, **kw):
    return concurrency_for(
        FitCalculator(), shape, make_plan(), nodes or [SPARK_01],
        kv_dtype="fp16", **kw,
    )


class TestConcurrencyIsDerivedNotDefaulted:
    def test_a_model_with_headroom_batches(self):
        """The whole point. One sequence is a floor, not an answer."""
        assert derive(QWEN3_30B_A3B) > 1

    def test_it_never_exceeds_the_ceiling(self):
        """A small model fits hundreds of sequences and nothing here has
        measured where more slots stop buying throughput and start buying
        preemption."""
        assert derive(QWEN3_30B_A3B) <= MAX_DERIVED_CONCURRENCY
        assert derive(GPT_OSS_120B) <= MAX_DERIVED_CONCURRENCY

    def test_the_ceiling_is_honoured_when_the_caller_lowers_it(self):
        """A latency-targeted request asks for the single-stream regime, and
        the planner already owns the constant that says where that stops."""
        assert derive(QWEN3_30B_A3B, ceiling=2) <= 2

    def test_a_model_with_no_room_still_serves_one(self):
        """Never 0. A refusal is the gate's to phrase, in its own sentence --
        not something a caller silently launches at."""
        starved = dataclasses.replace(SPARK_01, addressable_memory=1)
        assert derive(LLAMA_3_3_70B, nodes=[starved]) == 1

    def test_a_ceiling_below_one_is_not_a_launch_at_zero(self):
        assert derive(QWEN3_30B_A3B, ceiling=0) == 1

    def test_a_port_that_cannot_answer_degrades_to_one(self):
        """`context_for` degrades to FALLBACK_CONTEXT for the same reason: the
        gateway composes ports it does not own, including the stub whose whole
        job is to have nothing on it."""

        class Empty:
            pass

        assert concurrency_for(
            Empty(), QWEN3_30B_A3B, make_plan(), [SPARK_01], kv_dtype="fp16"
        ) == 1

    def test_a_port_that_raises_degrades_rather_than_failing_the_request(self):
        class Broken:
            def max_seqs(self, *a, **k):
                raise RuntimeError("no")

        assert concurrency_for(
            Broken(), QWEN3_30B_A3B, make_plan(), [SPARK_01], kv_dtype="fp16"
        ) == 1

    def test_it_asks_at_a_window_worth_having(self):
        """Derived at MIN_USEFUL_CONTEXT rather than at the real context,
        because the two cannot be solved from each other -- `context_for`
        already takes concurrency as an input. Pinned so the reference cannot
        drift into something nobody would serve at."""
        assert MIN_USEFUL_CONTEXT >= 4096
        wide = derive(QWEN3_30B_A3B, context=MIN_USEFUL_CONTEXT)
        narrow = derive(QWEN3_30B_A3B, context=MIN_USEFUL_CONTEXT * 8)
        assert wide >= narrow, "a longer window cannot hold more sequences"


class TestTheSeamItRestsOn:
    def test_max_seqs_is_public_on_the_calculator(self):
        """`_largest_max_seqs` has always existed and was reachable only from
        inside a refusal. The derivation needs it from outside."""
        assert callable(getattr(FitCalculator(), "max_seqs", None))

    def test_max_seqs_agrees_with_a_real_fit_check(self):
        """The derived figure has to actually fit, or the gate refuses the
        launch the derivation just chose."""
        from control_plane.contracts import FitRequest, Verdict

        calc = FitCalculator()
        seqs = derive(QWEN3_30B_A3B)
        result = calc.check(
            FitRequest(
                shape=QWEN3_30B_A3B, plan=make_plan(),
                context_length=MIN_USEFUL_CONTEXT, max_concurrent_seqs=seqs,
                kv_dtype="fp16",
            ),
            [SPARK_01],
        )
        assert result.verdict is not Verdict.WONT_FIT
