"""P-SIDECAR: pure computation for the UI's additive fields.

These are unit tests with no HTTP. The point of the module under test is that it
can be exercised without a gateway, so that the eventual edit to serialize.py
and internal_api.py -- files several sessions edit concurrently -- is a call site
and nothing more.
"""

from __future__ import annotations

from control_plane.gateway.ui_detail import (
    admission_blocks,
    provider_spend,
    spend_fields,
    strength_raw,
    target_counters,
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
