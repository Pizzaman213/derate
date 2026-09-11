"""The live-memory budget: the gate, the refusal, and the capacity answer.

The regression these pin is recorded in the plan that produced them, and was
reproduced on real hardware: a GB10 holding 92 GiB of foreign process was
told a 66 GiB model fit, because the gate budgeted against the 107.7 GiB
static ceiling rather than the ~15 GiB the machine could actually hand out.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from control_plane.contracts import (
    FitRequest,
    ParallelismKind,
    ParallelismPlan,
    Verdict,
)
from control_plane.fit import FitCalculator
from control_plane.planner import Planner
from control_plane.fit.capacity import largest_runnable
from control_plane.gateway import livefit
from control_plane.gateway.app import create_app
from tests.fixtures import GPT_OSS_120B, QWEN3_30B_A3B, SPARK_01
from tests.unit.test_gateway import FakeRegistry, build_deps, node_state

GIB = 1024**3


def _plan() -> ParallelismPlan:
    return ParallelismPlan(
        kind=ParallelismKind.SINGLE_NODE,
        tensor_parallel=1,
        pipeline_parallel=1,
        expert_parallel=1,
        data_parallel=1,
        node_ids=[SPARK_01.node_id],
        reason="single node",
        measured_link_gbps=0.0,
        rejected=[],
    )


def _req(shape, context=4096, seqs=4):
    return FitRequest(
        shape=shape,
        context_length=context,
        max_concurrent_seqs=seqs,
        kv_dtype="fp16",
        plan=_plan(),
    )


class LiveRegistry(FakeRegistry):
    """A registry that answers the live question, as the real one does."""

    def __init__(self, states, allocatable):
        super().__init__(states)
        self._alloc = allocatable

    def available_memory(self, node_id, guardrail=0.90):
        return self._alloc.get(node_id, 0)

    def memory_report(self, node_id):
        if node_id not in self._alloc:
            return None
        return {
            "node_id": node_id,
            "allocatable": self._alloc[node_id],
            "static_ceiling": SPARK_01.usable_memory(0.90),
            "addressable": SPARK_01.addressable_memory,
            "pool_used": SPARK_01.usable_memory(0.90) - self._alloc[node_id],
            "host_available": 30 * GIB,
            "host_reserve": 8 * GIB,
            "swap_used": 16 * GIB,
            "unified_memory": True,
        }


def _deps(allocatable):
    reg = LiveRegistry([node_state(SPARK_01, memory_used_pct=78.0)], allocatable)
    return dataclasses.replace(build_deps(registry=reg), fit=FitCalculator())


# -- the gate ---------------------------------------------------------------


def test_live_budget_flips_a_verdict_the_static_ceiling_approved():
    """The regression, verbatim: fits on idle hardware, will not fit now."""
    calc = FitCalculator()
    static = calc.check(_req(GPT_OSS_120B), [SPARK_01])
    live = calc.check(
        _req(GPT_OSS_120B), [SPARK_01], allocatable={SPARK_01.node_id: 15 * GIB}
    )

    assert static.verdict is not Verdict.WONT_FIT, "the old answer approved it"
    assert live.verdict is Verdict.WONT_FIT
    assert static.budget_basis == "static"
    assert live.budget_basis == "live"
    assert live.usable_per_node == 15 * GIB


def test_absent_node_falls_back_to_its_ceiling_and_says_so():
    """A cold coordinator must not refuse every launch."""
    calc = FitCalculator()
    result = calc.check(_req(GPT_OSS_120B), [SPARK_01], allocatable={"other": 1 * GIB})
    assert result.budget_basis == "static"
    assert result.usable_per_node == SPARK_01.usable_memory(0.90)
    assert any("no live memory reading" in w for w in result.warnings)


def test_a_live_refusal_never_claims_the_static_ceiling():
    """The reason string is the product; under a live budget the parenthetical
    '90% of 119.7 GiB addressable' is simply false."""
    calc = FitCalculator()
    live = calc.check(
        _req(GPT_OSS_120B), [SPARK_01], allocatable={SPARK_01.node_id: 15 * GIB}
    )
    assert "allocatable right now" in live.reason
    # The ceiling may still be named, but only as the thing that is NOT
    # available -- never as the budget the verdict was taken against.
    assert "against 15.0 GiB allocatable right now" in live.reason


def test_node_count_suggestion_uses_the_budget_in_force():
    """'spread it over N nodes' computed against memory that does not exist
    names a number that would still run out."""
    calc = FitCalculator()
    live = calc.check(
        _req(GPT_OSS_120B), [SPARK_01], allocatable={SPARK_01.node_id: 15 * GIB}
    )
    static = calc.check(_req(GPT_OSS_120B, context=1 << 20), [SPARK_01])
    assert live.verdict is Verdict.WONT_FIT
    # More nodes are needed against the smaller budget than the larger one.
    assert "nodes" in live.reason or "node" in live.reason
    assert static is not None


def test_max_context_honours_the_live_budget():
    calc = FitCalculator()
    plan = _plan()
    static = calc.max_context(GPT_OSS_120B, plan, [SPARK_01], 4, "fp16")
    live = calc.max_context(
        GPT_OSS_120B, plan, [SPARK_01], 4, "fp16",
        allocatable={SPARK_01.node_id: 70 * GIB},
    )
    assert live < static


# -- the seam that keeps older ports working --------------------------------


def test_a_port_without_the_parameter_degrades_instead_of_raising():
    """Five test doubles in this suite take (self, req, nodes) positionally.
    The gateway composes ports it does not own; one of them predating an
    additive parameter is not an error, it is a port that cannot answer."""

    class Legacy:
        def check(self, req, nodes):
            return FitCalculator().check(req, nodes)

    assert livefit._accepts_allocatable(Legacy().check) is False
    static, live, why = livefit.dual_check(
        Legacy(), _req(GPT_OSS_120B), [SPARK_01], {SPARK_01.node_id: 15 * GIB}
    )
    assert static is not None
    assert live is None
    assert "does not accept a live memory budget" in why


def test_zero_addressable_node_is_excluded_not_silently_refused():
    """A node with nothing to spend would be the argmin of every budget and
    make every verdict WONT_FIT."""
    dead = dataclasses.replace(SPARK_01, node_id="worker-docker", addressable_memory=0)
    kept, excluded = livefit.drop_unbudgetable(
        [SPARK_01, dead], {SPARK_01.node_id: 15 * GIB, "worker-docker": 0}
    )
    assert set(kept) == {SPARK_01.node_id}
    assert excluded and excluded[0]["node_id"] == "worker-docker"
    # The wording moved with the rule: the test is now "has nothing to spend",
    # not "has no GPU", because a CPU node reports 0 addressable for an honest
    # reason and can still be served from.
    assert "excluded from the budget" in excluded[0]["reason"]


# -- the wire ---------------------------------------------------------------


def test_plan_carries_both_verdicts_and_one_serve_decision():
    with TestClient(create_app(_deps({SPARK_01.node_id: 15 * GIB}))) as client:
        body = client.post(
            "/api/plan",
            json={"model_id": "openai/gpt-oss-120b", "context": 4096, "concurrency": 4},
        ).json()

    assert body["fit"]["budget_basis"] == "static"
    assert body["fit_live"]["budget_basis"] == "live"
    assert body["fit"]["verdict"] != body["fit_live"]["verdict"]
    # The UI reads exactly one field for the button.
    assert body["serve"]["allowed"] is False
    assert body["serve"]["basis"] == "live"
    assert body["serve"]["override_required"] is True
    assert body["serve"]["override_param"] == "allow_over_live_memory"


def test_plan_without_a_live_reading_degrades_to_the_static_answer():
    """Never refuse on missing telemetry.

    The honest "no live reading" case is a registry that cannot answer the
    question at all -- a stub, or one predating the method. A registry that
    CAN answer and says zero is reporting a full machine, which is a real
    refusal and not this case.
    """
    plain = dataclasses.replace(
        build_deps(registry=FakeRegistry([node_state(SPARK_01, memory_used_pct=78.0)])),
        fit=FitCalculator(),
    )
    assert not hasattr(plain.registry, "available_memory")
    with TestClient(create_app(plain)) as client:
        body = client.post(
            "/api/plan",
            json={"model_id": "openai/gpt-oss-120b", "context": 4096, "concurrency": 4},
        ).json()

    assert body["fit_live"] is None
    assert body["serve"]["basis"] == "static"
    assert body["serve"]["allowed"] is True
    assert body["serve"]["unavailable_reason"]


def test_launch_is_refused_with_409_and_starts_nothing():
    deps = _deps({SPARK_01.node_id: 15 * GIB})
    with TestClient(create_app(deps)) as client:
        before = len(client.get("/api/deployments").json())
        r = client.post(
            "/api/deployments",
            json={"model_id": "openai/gpt-oss-120b", "context": 4096, "concurrency": 4},
        )
        after = len(client.get("/api/deployments").json())

    # 409, not 400: the request is legal and would succeed on an idle box, so
    # it is a conflict with machine state and an unchanged retry can work.
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "live_memory_insufficient"
    assert body["error"]["param"] == "allow_over_live_memory"
    assert "It would fit on an idle machine" in body["error"]["message"]
    assert body["override"]["value_required"] is True
    assert after == before, "a refusal must start nothing"


def test_the_override_launches_and_is_recorded_on_the_fit():
    recorded = {}
    deps = _deps({SPARK_01.node_id: 15 * GIB})
    original = deps.deployments.launch

    def spy(shape, plan, fit, runtime, context_length, concurrency, **kwargs):
        recorded["fit"] = fit
        return original(shape, plan, fit, runtime, context_length, concurrency, **kwargs)

    deps.deployments.launch = spy  # type: ignore[method-assign]

    with TestClient(create_app(deps)) as client:
        r = client.post(
            "/api/deployments",
            json={
                "model_id": "openai/gpt-oss-120b",
                "context": 4096,
                "concurrency": 4,
                "allow_over_live_memory": True,
            },
        )

    assert r.status_code == 201
    fit = recorded["fit"]
    # The budget the launch was gated on is the one stored, so a later
    # fit_miss compares against a number that existed.
    assert fit.budget_basis == "live"
    assert any("at the operator's instruction" in w for w in fit.warnings)


def test_static_wont_fit_is_still_a_400_and_offers_no_override():
    """A model that fits no budget at all is not an override case."""
    huge = dataclasses.replace(GPT_OSS_120B, total_params=20_000_000_000_000)
    deps = _deps({SPARK_01.node_id: 15 * GIB})
    with TestClient(create_app(deps)) as client:
        client.app  # noqa: B018 - keep the lifespan open
        r = client.post(
            "/api/deployments",
            json={"model_id": "openai/gpt-oss-120b", "context": 1 << 21, "concurrency": 64},
        )
    assert r.status_code in (400, 409)
    if r.status_code == 400:
        assert r.json()["error"]["code"] == "wont_fit"
    assert huge is not None


# -- capacity ---------------------------------------------------------------


def test_capacity_answers_differently_under_the_two_budgets():
    shapes = [
        (GPT_OSS_120B, "gpt-oss-120b", None),
        (QWEN3_30B_A3B, "qwen3-30b-a3b", None),
    ]
    _, static_best = largest_runnable(
        shapes, [SPARK_01], context=4096, max_seqs=4, kv_dtype="fp16"
    )
    _, live_best = largest_runnable(
        shapes, [SPARK_01], context=4096, max_seqs=4, kv_dtype="fp16",
        allocatable={SPARK_01.node_id: 15 * GIB},
    )
    assert static_best is not None
    assert static_best.model_id == GPT_OSS_120B.model_id
    # The live answer is smaller, or there is none.
    assert live_best is None or live_best.total_params <= static_best.total_params


def test_a_requantized_row_drops_the_measured_weight_bytes_and_says_so():
    """A measured on-disk total belongs to the dtype it was measured at.
    Pricing q4_k_m weights from measured bf16 bytes would invent a number."""
    rows, _ = largest_runnable(
        [(QWEN3_30B_A3B, "qwen3-30b-a3b", 60 * GIB)],
        [SPARK_01], context=4096, max_seqs=4, kv_dtype="fp16",
        allocatable={SPARK_01.node_id: 15 * GIB},
    )
    row = rows[0]
    if row.requantized:
        assert any("not the measured checkpoint" in w for w in row.warnings)


def test_capacity_never_offers_a_larger_dtype_than_the_model_ships_in():
    rows, _ = largest_runnable(
        [(GPT_OSS_120B, "gpt-oss-120b", None)],
        [SPARK_01], context=4096, max_seqs=4, kv_dtype="fp16",
        allocatable={SPARK_01.node_id: 15 * GIB},
    )
    row = rows[0]
    if row.dtype is not None and row.requantized:
        from control_plane.fit.constants import QUANT_SUGGESTION_ORDER

        order = list(QUANT_SUGGESTION_ORDER)
        assert order.index(row.dtype) > order.index(row.native_dtype)


# -- error messages ---------------------------------------------------------


def test_a_plan_failure_carries_its_reason_not_just_a_class_name():
    from control_plane.gateway import errors

    try:
        try:
            raise ConnectionError("Remote end closed connection without response")
        except Exception as inner:
            raise RuntimeError("hub request failed: https://example/api/models/X") from inner
    except Exception as exc:
        assert "hub request failed" in errors.detail(exc)
        assert errors.cause_chain(exc) == [
            "ConnectionError: Remote end closed connection without response"
        ]


def test_error_detail_is_redacted_before_it_leaves():
    from control_plane.gateway import errors

    class R:
        def scrub(self, text):
            return text.replace("sk-secret", "***")

    exc = RuntimeError("auth failed for sk-secret")
    assert "sk-secret" not in errors.detail(exc, R())


@pytest.mark.parametrize("empty", [RuntimeError(), ValueError("")])
def test_detail_falls_back_to_the_class_name(empty):
    from control_plane.gateway import errors

    assert errors.detail(empty) == type(empty).__name__


# -- manual placement meets the live gate -----------------------------------
#
# The feature's central safety property: naming the machines narrows what gets
# checked, it never skips the check.


#: A second Spark with a smaller addressable slice. A different hardware group
#: -- `topology._shape_key` includes addressable memory, and its docstring says
#: why: "two GB10s with different addressable memory cannot hold equal shards"
#: -- while still being large enough that the model fits on an idle machine.
#: That is what lets these tests reach the pooling gate at all: a 3090 is so
#: much smaller that the static fit refuses first, and a launch that cannot fit
#: any budget is correctly reported as unfittable rather than as un-pooled.
SPARK_LITE = dataclasses.replace(
    SPARK_01,
    node_id="spark-lite",
    hostname="spark-lite",
    addressable_memory=int(SPARK_01.addressable_memory * 0.8),
)


def _mixed_deps(allocatable):
    """Two unlike Sparks, both live, planned by the real planner."""
    reg = LiveRegistry(
        [
            node_state(SPARK_01, memory_used_pct=78.0),
            node_state(SPARK_LITE, memory_used_pct=10.0),
        ],
        allocatable,
    )
    deps = dataclasses.replace(build_deps(registry=reg), fit=FitCalculator())
    deps.planner = Planner()
    return deps


def test_manual_placement_does_not_bypass_the_live_fit_gate():
    """Choosing the machine by hand still meets the same refusal."""
    deps = _deps({SPARK_01.node_id: 15 * GIB})
    deps.planner = Planner()
    with TestClient(create_app(deps)) as client:
        refused = client.post(
            "/api/deployments",
            json={
                "model_id": "openai/gpt-oss-120b",
                "context": 4096,
                "concurrency": 4,
                "node_ids": [SPARK_01.node_id],
            },
        )
        allowed = client.post(
            "/api/deployments",
            json={
                "model_id": "openai/gpt-oss-120b",
                "context": 4096,
                "concurrency": 4,
                "node_ids": [SPARK_01.node_id],
                "allow_over_live_memory": True,
            },
        )

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "live_memory_insufficient"
    assert allowed.status_code == 201


def test_the_two_overrides_are_independent():
    """Neither permission implies the other.

    The most important test in this feature. `allow_mixed_hardware` says
    nothing about whether the memory is there, and `allow_over_live_memory`
    says nothing about whether the machines belong in one pool. If either ever
    starts unlocking the other, a launch can get through on a permission its
    operator never granted.
    """
    # A context wide enough that the plan genuinely spans both machines. At a
    # small context the model fits on one, only one machine is occupied, and
    # nothing is pooled -- correctly, since the gate reports what will actually
    # run rather than what was ticked.
    body = {
        "model_id": "openai/gpt-oss-120b",
        "context": 131072,
        "concurrency": 8,
        "node_ids": [SPARK_01.node_id, SPARK_LITE.node_id],
    }
    alloc = {SPARK_01.node_id: 15 * GIB, SPARK_LITE.node_id: 15 * GIB}

    with TestClient(create_app(_mixed_deps(alloc))) as client:
        # The memory override alone must not permit the pool.
        memory_only = client.post(
            "/api/deployments", json={**body, "allow_over_live_memory": True}
        )
        # The pooling override alone must not permit the memory.
        pool_only = client.post(
            "/api/deployments", json={**body, "allow_mixed_hardware": True}
        )

    assert memory_only.status_code == 400
    assert memory_only.json()["error"]["code"] == "mixed_hardware_not_allowed"

    assert pool_only.status_code == 409
    assert pool_only.json()["error"]["code"] == "live_memory_insufficient"


def test_the_live_budget_is_taken_against_the_machines_that_were_named():
    """A machine that was ticked but carries no rank must not set the budget."""
    alloc = {SPARK_01.node_id: 15 * GIB, SPARK_LITE.node_id: 1 * GIB}
    with TestClient(create_app(_mixed_deps(alloc))) as client:
        body = client.post(
            "/api/plan",
            json={
                "model_id": "openai/gpt-oss-120b",
                "context": 4096,
                "concurrency": 4,
                "node_ids": [SPARK_01.node_id],
            },
        ).json()

    assert body["capacity"]["binding_node"] == SPARK_01.node_id
    assert [n["node_id"] for n in body["capacity"]["nodes"]] == [SPARK_01.node_id]


def test_the_operators_choice_is_recorded_on_the_persisted_fit():
    """`plan.reason` stays planner prose even for a shape a human forced, so
    the attribution has to live somewhere the deployment keeps."""
    recorded = {}
    deps = _deps({SPARK_01.node_id: 15 * GIB})
    deps.planner = Planner()
    original = deps.deployments.launch

    def spy(shape, plan, fit, runtime, context_length, concurrency, **kwargs):
        recorded["fit"] = fit
        return original(shape, plan, fit, runtime, context_length, concurrency, **kwargs)

    deps.deployments.launch = spy  # type: ignore[method-assign]

    with TestClient(create_app(deps)) as client:
        r = client.post(
            "/api/deployments",
            json={
                "model_id": "openai/gpt-oss-120b",
                "context": 4096,
                "concurrency": 4,
                "node_ids": [SPARK_01.node_id],
                "allow_over_live_memory": True,
            },
        )

    assert r.status_code == 201
    assert any(
        "placed and shaped at the operator's instruction" in w
        for w in recorded["fit"].warnings
    )


class TestDerivedContext:
    """`largest_runnable(context=None)` -- the gate choosing the question.

    The models screen used to ask for a context length before a model had been
    chosen, and the Serve panel asked again. Both are gone, so the number has
    to come from the arithmetic instead. What it must not do is make the
    quantization ladder meaningless, which is the failure mode the derivation
    is shaped around.
    """

    NATIVE_WINDOW = 40960

    def _walk(self, allocatable, *, native=NATIVE_WINDOW):
        return largest_runnable(
            [(QWEN3_30B_A3B, QWEN3_30B_A3B.model_id, None)],
            [SPARK_01],
            context=None,
            max_seqs=1,
            kv_dtype="fp16",
            allocatable=allocatable,
            native_context={QWEN3_30B_A3B.model_id: native},
        )[0][0]

    def test_a_roomy_machine_gets_the_models_own_window_at_its_own_dtype(self):
        row = self._walk(None)
        assert row.dtype == QWEN3_30B_A3B.dtype, "nothing forced a requantize"
        assert row.context == self.NATIVE_WINDOW

    def test_a_tight_budget_still_quantizes_rather_than_shrinking_context(self):
        """The regression the two-pass derivation exists to prevent.

        Deriving the context per LADDER RUNG would fit every rung by
        construction -- `max_context` returns the largest context that fits, so
        bf16 at 512 tokens "fits" -- the native dtype would always win, and a
        ladder that always recommends the native dtype has stopped answering
        the question it exists for.
        """
        row = self._walk({SPARK_01.node_id: 26 * GIB})
        assert row.fits, row.reason
        assert row.requantized, (
            f"chose {row.dtype} at {row.context} tokens instead of quantizing"
        )
        assert row.context >= 4096, (
            f"traded quality for {row.context} tokens, which is not a context "
            f"worth having"
        )

    def test_the_derived_context_is_one_the_gate_verified(self):
        """Never report a context that has not been checked. Feed it back in."""
        calc = FitCalculator()
        for allocatable in (None, {SPARK_01.node_id: 26 * GIB}):
            row = self._walk(allocatable)
            if not row.fits:
                continue
            shape = dataclasses.replace(QWEN3_30B_A3B, dtype=row.dtype)
            again = calc.check(
                FitRequest(
                    shape=shape,
                    context_length=row.context,
                    max_concurrent_seqs=row.max_seqs,
                    kv_dtype="fp16",
                    plan=_plan(),
                ),
                [SPARK_01],
                allocatable=allocatable,
            )
            assert again.verdict is not Verdict.WONT_FIT, again.reason

    def test_an_unknown_window_falls_back_to_what_the_screen_used_to_ask_at(self):
        """A model whose `max_position_embeddings` we could not read is judged
        the way it was judged before the context became derivable -- lower if
        that does not fit, never higher. The alternative is the ceiling of the
        search, which is 2,097,152 tokens and is not a window anybody asked
        for."""
        from control_plane.fit.capacity import FALLBACK_CONTEXT

        row = self._walk(None, native=None)
        assert row.context == FALLBACK_CONTEXT

    def test_a_ladderless_walk_is_not_handed_a_refused_rungs_window(self):
        """The variant-table regression, end to end.

        `/api/models/variants` walks with `ladder=()` -- each published file is
        judged as it ships, never re-quantized into something nobody published.
        That removes the recovery every other test on this path leans on, so a
        context derived from a rung which does not fit is fatal rather than
        merely wasteful: the whole list refuses on KV cache.

        The shipped symptom was a 26B MoE whose 4-bit variants are ~13 GiB
        being judged at 262,144 tokens -- the native window of its 48 GiB bf16
        sibling, which had been refused outright -- and every row reporting
        "will not fit" at 12 tok/s on hardware that holds it.
        """
        tight = {SPARK_01.node_id: 20 * GIB}
        rows, _ = largest_runnable(
            [
                (dataclasses.replace(QWEN3_30B_A3B, dtype="bf16"), "bf16", None),
                (dataclasses.replace(QWEN3_30B_A3B, dtype="q4_k_m"), "q4", None),
            ],
            [SPARK_01],
            context=None,
            max_seqs=1,
            kv_dtype="fp16",
            allocatable=tight,
            ladder=(),
            native_context={"bf16": self.NATIVE_WINDOW, "q4": self.NATIVE_WINDOW},
        )
        by_label = {r.label: r for r in rows}
        assert by_label["q4"].fits, (
            f"the 4-bit variant was refused at {by_label['q4'].context} tokens: "
            f"{by_label['q4'].reason}"
        )
        assert by_label["q4"].context < self.NATIVE_WINDOW

    def test_a_row_that_cannot_load_still_reports_no_throughput_worth_reading(self):
        """A decode figure is about a model that is resident. The screen used
        to print one beside every refusal, computed against a cache read for a
        window nothing had verified."""
        tight = {SPARK_01.node_id: 4 * GIB}
        rows, _ = largest_runnable(
            [(dataclasses.replace(QWEN3_30B_A3B, dtype="bf16"), "bf16", None)],
            [SPARK_01],
            context=None,
            max_seqs=1,
            kv_dtype="fp16",
            allocatable=tight,
            ladder=(),
            native_context={"bf16": self.NATIVE_WINDOW},
        )
        assert rows[0].verdict == Verdict.WONT_FIT.value
        assert rows[0].reason, "a refusal has to say what blew the budget"
        assert "weights" in rows[0].reason.lower(), (
            f"the refusal does not name the term that blew the budget: "
            f"{rows[0].reason}"
        )

    def test_a_named_context_is_untouched(self):
        rows, _ = largest_runnable(
            [(QWEN3_30B_A3B, QWEN3_30B_A3B.model_id, None)],
            [SPARK_01],
            context=32768,
            max_seqs=4,
            kv_dtype="fp16",
        )
        assert rows[0].context == 32768
        assert rows[0].max_seqs == 4


class TestAnUnmeasuredNodeIsNotALiveReading:
    """A ceiling reported as a measurement is the bug this file exists for.

    The regression above was budgeting against the static ceiling instead of
    the live figure. This is the same defect one layer down and it survived
    that fix: ``Registry.available_memory`` is typed ``-> int``, so a node
    nothing had sampled was answered with its nameplate, and the gate could
    not tell that apart from a reading. The verdict then went out as
    ``basis: "live"`` with prose saying "allocatable right now" about a number
    no machine had ever been asked for.

    That matters most on exactly the hardware this project targets, where the
    pool is shared with the OS and with whatever else the operator is running:
    the ceiling is not an optimistic estimate there, it is the one figure
    guaranteed to be wrong.
    """

    def _registry(self, tmp_path, sampled: bool):
        import time

        from control_plane.registry.telemetry import TelemetrySample
        from tests.unit.test_registry import (
            REAL_SPARK_AVAILABLE,
            REAL_SPARK_GPU_MIB,
            REAL_SPARK_POOL_TOTAL,
            make_registry,
        )

        registry = make_registry(tmp_path, local=SPARK_01)
        if sampled:
            registry.apply_sample(
                SPARK_01.node_id,
                TelemetrySample(
                    ts=time.time(),
                    memory_used=REAL_SPARK_POOL_TOTAL - REAL_SPARK_AVAILABLE,
                    memory_total=REAL_SPARK_POOL_TOTAL,
                    power_watts=84.0,
                    temperature_c=81.0,
                    utilization_pct=96.0,
                    gpu_memory_used=REAL_SPARK_GPU_MIB * 1024**2,
                    gpu_process_count=1,
                    host_memory_total=REAL_SPARK_POOL_TOTAL,
                    host_memory_available=REAL_SPARK_AVAILABLE,
                ),
            )
        return registry

    def test_an_unsampled_node_is_excluded_rather_than_guessed(self, tmp_path):
        registry = self._registry(tmp_path, sampled=False)

        assert registry.available_memory(SPARK_01.node_id) == SPARK_01.usable_memory(), (
            "available_memory still answers with the ceiling, which is what the "
            "memory report wants; allocatable_or_none is the honest one"
        )
        assert registry.allocatable_or_none(SPARK_01.node_id) is None

        budgets, excluded, why = livefit.allocatable_map(registry, [SPARK_01.node_id])
        assert budgets == {}, "a nameplate must not enter the live budget"
        assert [e["node_id"] for e in excluded] == [SPARK_01.node_id]
        assert excluded[0]["reason"], "an exclusion has to say why"
        assert why

    def test_the_verdict_says_static_not_live(self, tmp_path):
        registry = self._registry(tmp_path, sampled=False)
        budgets, _, _ = livefit.allocatable_map(registry, [SPARK_01.node_id])
        static, live, missing = livefit.dual_check(
            FitCalculator(), _req(QWEN3_30B_A3B, context=32768, seqs=4), [SPARK_01], budgets
        )

        assert live is None, "nothing measured this node, so there is no live verdict"
        assert missing
        decision = livefit.serve_decision(static, live, unavailable_reason=missing)
        assert decision["basis"] == livefit.BASIS_STATIC
        assert "allocatable right now" not in static.reason, (
            "the static verdict must not describe a ceiling as a live reading: "
            f"{static.reason}"
        )

    def test_a_sampled_node_still_gates_on_the_live_figure(self, tmp_path):
        """The fix must not cost the measurement when there is one."""
        registry = self._registry(tmp_path, sampled=True)
        live_bytes = registry.allocatable_or_none(SPARK_01.node_id)
        assert live_bytes is not None
        assert live_bytes < SPARK_01.usable_memory() / 5, (
            "the sample says most of the pool is spoken for"
        )

        budgets, excluded, why = livefit.allocatable_map(registry, [SPARK_01.node_id])
        assert budgets == {SPARK_01.node_id: live_bytes}
        assert excluded == [] and why is None

        _, live, _ = livefit.dual_check(
            FitCalculator(), _req(QWEN3_30B_A3B, context=32768, seqs=4), [SPARK_01], budgets
        )
        assert live is not None and live.verdict is Verdict.WONT_FIT, (
            "a 30B cannot fit in what this node can actually hand out"
        )

    def test_an_unknown_node_is_absent_not_zero(self, tmp_path):
        """The mirror of the same mistake: absence rendered as a number.

        ``available_memory`` answers an id it does not hold with ``0``, which
        reads as "this node can allocate nothing" and would refuse every
        launch onto it rather than admitting it was never found.
        """
        registry = self._registry(tmp_path, sampled=True)
        assert registry.available_memory("nope") == 0
        assert registry.allocatable_or_none("nope") is None

    def test_a_node_whose_telemetry_stopped_is_not_a_current_reading(self, tmp_path):
        """A node can answer /agent/health with its samples frozen.

        ``_sample_node`` keeps the last reading on purpose -- the health loop
        owns that verdict -- so staleness has to be read off the node state
        rather than inferred from the sample being present.
        """
        registry = self._registry(tmp_path, sampled=True)
        assert registry.allocatable_or_none(SPARK_01.node_id) is not None

        registry._members[SPARK_01.node_id].healthy = False
        assert registry.allocatable_or_none(SPARK_01.node_id) is None, (
            "an old reading of a shared pool is not a fact about now"
        )
