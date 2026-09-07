"""P-SIDECAR: pure computation for the UI's additive fields, plus (P-WIRE) the
call sites that wire them into serialize.py and internal_api.py.

The first half of this file is unit tests with no HTTP: the point of
`ui_detail.py` is that it can be exercised without a gateway, so that the edit
to serialize.py and internal_api.py -- files several sessions edit
concurrently -- is a call site and nothing more. The second half pins that the
call sites actually pass the right data through: a measured link's five
annotation keys, a provider's nine spend keys, and a routing target's
counters/strength_raw/admission_blocks, over real HTTP through `create_app`.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from control_plane.gateway import create_app, serialize
from control_plane.gateway.admission import BLOCK_RATE_LIMITED
from control_plane.gateway.ui_detail import (
    admission_blocks,
    provider_spend,
    spend_fields,
    strength_raw,
    target_counters,
)
from control_plane.links.record import LinkAnnotation, annotate
from tests.fixtures import LINKS
from tests.test_gateway import (
    FakeDeployments,
    FakeProviders,
    build_deps,
    make_deployment,
    make_provider,
    two_unequal_replicas,
)

SECRET = "sk-live-do-not-leak-000000000000"


class FakeAccountingProviders:
    """A provider port that accounts, and that also returns a key-shaped field
    it has no business exposing."""

    def __init__(self, rows):
        self.rows = rows
        self.calls: list[dict] = []

    def public_list(self, *, include_models: bool = True):
        self.calls.append({"include_models": include_models})
        return self.rows


class FakeSilentProviders:
    """A provider port with no accounting at all -- the shipped stub surface."""


# ==========================================================================
# The load-bearing test: this module must stay an allowlist, not a merge.
# ==========================================================================


def test_a_key_shaped_field_from_the_port_cannot_reach_the_wire():
    """provider_public_dict is checked for key material upstream, but this is
    the gateway's own second line of defence and it must not depend on that.
    If this module ever becomes `dict(row)` this test fails."""
    port = FakeAccountingProviders(
        [
            {
                "provider_id": "openrouter",
                "spend_today_usd": 0.41,
                "requests_today": 96,
                # None of these are in the allowlist.
                "resolved_key": SECRET,
                "api_key": SECRET,
                "authorization": f"Bearer {SECRET}",
            }
        ]
    )
    out = provider_spend(port)
    flat = repr(out)
    assert SECRET not in flat
    assert "resolved_key" not in out["openrouter"]
    assert "api_key" not in out["openrouter"]
    assert "authorization" not in out["openrouter"]
    assert out["openrouter"]["spend_today_usd"] == 0.41


def test_models_are_not_fetched_twice():
    """The payload builds `models` from the Provider record through its own
    allowlist; asking for them here doubles the response for nothing."""
    port = FakeAccountingProviders([{"provider_id": "p"}])
    provider_spend(port)
    assert port.calls == [{"include_models": False}]


# ==========================================================================
# None means "we do not know". Zero means zero.
# ==========================================================================


def test_a_port_that_does_not_account_yields_null_not_zero():
    fields = spend_fields(provider_spend(FakeSilentProviders()).get("anything"))
    assert set(fields) == {
        "admitting",
        "admission_block",
        "daily_budget_usd",
        "spend_today_usd",
        "tokens_today",
        "requests_today",
        "unpriced_requests_today",
        "retry_in_s",
        "model_count",
    }
    for key, value in fields.items():
        assert value is None, f"{key} should be None, got {value!r}"
        assert value != 0, f"{key} must not be zero -- a stub has not spent $0"


def test_honest_zeros_from_a_live_port_are_passed_through():
    """A day with no ledger entry legitimately reports 0 requests and 0 tokens.
    Collapsing those to None would lose the difference between 'no requests
    today' and 'nobody is counting'."""
    port = FakeAccountingProviders(
        [
            {
                "provider_id": "openrouter",
                "spend_today_usd": 0.0,
                "requests_today": 0,
                "unpriced_requests_today": 0,
                "tokens_today": {"input": 0, "output": 0},
                "retry_in_s": 0.0,
            }
        ]
    )
    fields = spend_fields(provider_spend(port)["openrouter"])
    assert fields["spend_today_usd"] == 0.0
    assert fields["requests_today"] == 0
    assert fields["tokens_today"] == {"input": 0, "output": 0}
    assert fields["spend_today_usd"] is not None


def test_tokens_today_keeps_its_nested_shape():
    """The inner keys are `input`/`output`, deliberately renamed from the
    dataclass's input_tokens/output_tokens."""
    port = FakeAccountingProviders(
        [{"provider_id": "p", "tokens_today": {"input": 41200, "output": 9840}}]
    )
    assert provider_spend(port)["p"]["tokens_today"] == {"input": 41200, "output": 9840}


def test_a_raising_port_degrades_to_empty_rather_than_propagating():
    class Boom:
        def public_list(self, **_):
            raise RuntimeError("provider store is down")

    assert provider_spend(Boom()) == {}


# ==========================================================================
# Admission blocks
# ==========================================================================


class FakeAdmission:
    def __init__(self, mapping):
        self.mapping = mapping

    def blocks(self, target_id):
        return self.mapping.get(target_id, set())


def test_block_reasons_are_sorted_so_the_json_is_deterministic():
    """blocks() returns an unordered set. Unsorted, two identical states
    serialise differently between requests."""
    adm = FakeAdmission({"d-a": {"memory_critical", "draining"}})
    assert admission_blocks(adm, ["d-a"]) == {"d-a": ["draining", "memory_critical"]}


def test_a_target_with_no_block_is_absent_rather_than_empty():
    adm = FakeAdmission({"d-a": set()})
    assert admission_blocks(adm, ["d-a"]) == {}


def test_a_controller_without_blocks_yields_nothing():
    assert admission_blocks(object(), ["d-a"]) == {}


# ==========================================================================
# Strength raw
# ==========================================================================


class _Score:
    def __init__(self, raw, source):
        self.raw = raw
        self.source = source


class _Index:
    def __init__(self, raw_strength):
        self.raw_strength = raw_strength


def test_raw_strength_is_extracted_per_target():
    idx = _Index({"d-a": _Score(38.4, "measured"), "p:x": _Score(1.0, "default")})
    assert strength_raw(idx) == {"d-a": 38.4, "p:x": 1.0}


def test_an_index_without_raw_strength_yields_nothing():
    assert strength_raw(object()) == {}


# ==========================================================================
# Per-target counters: the None/0 split mirrors TargetStats exactly
# ==========================================================================


class _Stats:
    def __init__(self, mapping):
        self.mapping = mapping

    def peek(self, tid):
        return self.mapping.get(tid)


class _T:
    def __init__(self, **kw):
        self.completed = kw.get("completed", 0)
        self.failed = kw.get("failed", 0)
        self.total_tokens = kw.get("total_tokens", 0)
        self.decode_tps = kw.get("decode_tps")
        self.mean_duration_s = kw.get("mean_duration_s")


def test_counters_are_zero_and_ewmas_are_none_for_a_fresh_target():
    """A target that exists but has served nothing: counters legitimately read
    0, while decode_tps has never been observed. decode_tps is never 0 through
    complete(), so a 0 there would be a number nobody measured."""
    out = target_counters(_Stats({"d-a": _T()}), ["d-a"])["d-a"]
    assert out["completed"] == 0
    assert out["failed"] == 0
    assert out["total_tokens"] == 0
    assert out["decode_tps"] is None
    assert out["mean_duration_s"] is None


def test_a_never_selected_target_reports_null_counters_not_zero():
    """Absent from the registry entirely -- the UI must be able to tell 'new
    replica' from 'replica nothing routes to'."""
    out = target_counters(_Stats({}), ["d-ghost"])["d-ghost"]
    assert all(v is None for v in out.values())


def test_observed_values_pass_through():
    stats = _Stats(
        {"d-a": _T(completed=412, failed=3, total_tokens=91_000, decode_tps=42.1,
                   mean_duration_s=6.2)}
    )
    out = target_counters(stats, ["d-a"])["d-a"]
    assert out == {
        "completed": 412,
        "failed": 3,
        "total_tokens": 91_000,
        "decode_tps": 42.1,
        "mean_duration_s": 6.2,
    }


def test_a_registry_without_peek_yields_nothing():
    assert target_counters(object(), ["d-a"]) == {}


# ==========================================================================
# P-WIRE: call-site pins, over real HTTP through create_app.
#
# Everything above tests the pure functions in isolation. Everything below
# tests that internal_api.py and serialize.py actually call them and thread
# the result onto the wire -- the part a pure-function test cannot see.
# ==========================================================================


# ---------------------------------------------------------------------------
# link_payload: the five annotation keys (audit M-7)
# ---------------------------------------------------------------------------


def test_link_payload_carries_the_five_annotation_keys_when_present():
    bare = LINKS[("spark-01", "spark-02")]
    annotated = annotate(
        bare,
        LinkAnnotation(
            active_ports=1,
            total_ports=2,
            ports_inspected_on="spark-01",
            gdr_detected_by="nccl all_reduce completed under GDR",
            duration_s=4.2,
        ),
    )
    payload = serialize.link_payload(annotated)
    assert payload["active_ports"] == 1
    assert payload["total_ports"] == 2
    assert payload["ports_inspected_on"] == "spark-01"
    assert payload["gdr_detected_by"] == "nccl all_reduce completed under GDR"
    assert payload["duration_s"] == 4.2


def test_link_payload_omits_the_five_annotation_keys_without_an_annotation():
    payload = serialize.link_payload(LINKS[("spark-01", "spark-02")])
    for key in (
        "active_ports",
        "total_ports",
        "ports_inspected_on",
        "gdr_detected_by",
        "duration_s",
    ):
        assert key not in payload


# ---------------------------------------------------------------------------
# provider_payload: the nine spend keys, over /api/providers
# ---------------------------------------------------------------------------


class AccountingProviders(FakeProviders):
    """A provider port that accounts -- `public_list` returns real rows."""

    def public_list(self, *, include_models: bool = True):
        return [
            {
                "provider_id": p.provider_id,
                "admitting": True,
                "admission_block": None,
                "daily_budget_usd": 5.0,
                "spend_today_usd": 0.0,
                "tokens_today": {"input": 0, "output": 0},
                "requests_today": 0,
                "unpriced_requests_today": 0,
                "retry_in_s": None,
                "model_count": len(p.models),
            }
            for p in self.providers
        ]


_SPEND_KEYS = (
    "admitting",
    "admission_block",
    "daily_budget_usd",
    "spend_today_usd",
    "tokens_today",
    "requests_today",
    "unpriced_requests_today",
    "retry_in_s",
    "model_count",
)


def test_provider_spend_keys_are_all_none_on_a_non_accounting_port_over_http():
    deps = build_deps(providers=FakeProviders([make_provider()]))
    with TestClient(create_app(deps)) as client:
        provider = client.get("/api/providers").json()[0]
    for key in _SPEND_KEYS:
        assert provider[key] is None, f"{key} should be None, got {provider[key]!r}"


def test_provider_spend_keys_pass_through_honest_zeros_over_http():
    deps = build_deps(providers=AccountingProviders([make_provider()]))
    with TestClient(create_app(deps)) as client:
        provider = client.get("/api/providers").json()[0]
    # Honest zeros/known values from a live port must survive, not collapse
    # to null the way the non-accounting case above does.
    assert provider["spend_today_usd"] == 0.0
    assert provider["requests_today"] == 0
    assert provider["unpriced_requests_today"] == 0
    assert provider["tokens_today"] == {"input": 0, "output": 0}
    assert provider["daily_budget_usd"] == 5.0
    assert provider["admitting"] is True
    assert provider["admission_block"] is None


def test_provider_spend_keys_survive_the_single_provider_patch_response():
    """The single-provider responses (add/patch/refresh) get spend too, not
    only the list endpoint."""
    deps = build_deps(providers=AccountingProviders([make_provider(priority=10)]))
    with TestClient(create_app(deps)) as client:
        patched = client.patch(
            "/api/providers/openrouter", json={"priority": 20}
        ).json()
    assert patched["priority"] == 20
    assert patched["spend_today_usd"] == 0.0
    assert patched["requests_today"] == 0


# ---------------------------------------------------------------------------
# routing targets: counters (None-vs-0 survives to HTTP), strength_raw,
# admission_blocks
# ---------------------------------------------------------------------------


def test_target_counters_none_vs_zero_semantics_survive_to_http():
    deps = build_deps(
        deployments=FakeDeployments(
            [
                make_deployment("d-a", "llama-3.3-70b", backend_url="http://a/v1"),
                make_deployment("d-b", "llama-3.3-70b", backend_url="http://b/v1"),
            ]
        )
    )
    app = create_app(deps)
    with TestClient(app) as client:
        # d-a has been dispatched to at least once (proxy.py calls stats.get());
        # d-b never has, so its TargetStats record is never created.
        app.state.ctx.stats.get("d-a")
        config = next(
            c
            for c in client.get("/api/routing").json()
            if c["served_name"] == "llama-3.3-70b"
        )
    targets = {t["target_id"]: t for t in config["targets"]}
    assert targets["d-a"]["counters"] == {
        "completed": 0,
        "failed": 0,
        "total_tokens": 0,
        "decode_tps": None,
        "mean_duration_s": None,
    }
    assert targets["d-b"]["counters"] == {
        "completed": None,
        "failed": None,
        "total_tokens": None,
        "decode_tps": None,
        "mean_duration_s": None,
    }


def test_strength_raw_is_present_exactly_where_strength_source_is_over_http():
    deps = build_deps(deployments=two_unequal_replicas(42.0, 12.0))
    with TestClient(create_app(deps)) as client:
        config = next(
            c
            for c in client.get("/api/routing").json()
            if c["served_name"] == "llama-3.3-70b"
        )
    targets = config["targets"]
    assert targets, "expected at least one routing target"
    for t in targets:
        assert (t["strength_raw"] is not None) == (t["strength_source"] is not None)
    assert {t["strength_source"] for t in targets} == {"predicted"}
    assert all(isinstance(t["strength_raw"], float) for t in targets)


def test_admission_blocks_appear_on_the_wire_for_a_blocked_target_only():
    deps = build_deps(
        deployments=FakeDeployments(
            [
                make_deployment("d-a", "llama-3.3-70b", backend_url="http://a/v1"),
                make_deployment("d-b", "llama-3.3-70b", backend_url="http://b/v1"),
            ]
        )
    )
    app = create_app(deps)
    with TestClient(app) as client:
        app.state.ctx.admission.block("d-a", BLOCK_RATE_LIMITED)
        config = next(
            c
            for c in client.get("/api/routing").json()
            if c["served_name"] == "llama-3.3-70b"
        )
    targets = {t["target_id"]: t for t in config["targets"]}
    assert targets["d-a"]["admission_blocks"] == [BLOCK_RATE_LIMITED]
    assert targets["d-b"]["admission_blocks"] is None
