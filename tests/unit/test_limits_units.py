"""The limits package's pure parts: cycle bounds, limit names, pricing."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from control_plane.contracts.auth import (
    LIMIT_TYPES,
    MODEL_LIMIT_SUFFIXES,
    AuthUnavailable,
    LimitPort,
    PricingPort,
)
from control_plane.contracts.telemetry import CostBasis
from control_plane.limits import (
    LimitKind,
    LimitService,
    PricingStore,
    cycle_bounds,
    estimate_prompt_tokens,
    kind_of,
    model_limit_type,
    parse_limit_type,
)
from control_plane.limits.cycle import CYCLE_S
from control_plane.limits.service import IMAGE_TOKENS_ESTIMATE
from control_plane.limits.types import CYCLE_METRIC, FIXED_LIMIT_TYPES, KIND

# -- cycle_bounds ------------------------------------------------------------

A = 1_731_801_600.123456  # a fractional anchor, as a REAL created_at is


def test_cycle_is_thirty_days():
    assert CYCLE_S == 30 * 86400


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (A, (A, A + CYCLE_S)),
        (A + 1, (A, A + CYCLE_S)),
        (A + CYCLE_S - 0.001, (A, A + CYCLE_S)),
        (
            A + CYCLE_S,
            (A + CYCLE_S, A + 2 * CYCLE_S),
        ),  # resets_at is the next cycle's first instant
        (A + 2 * CYCLE_S - 0.001, (A + CYCLE_S, A + 2 * CYCLE_S)),
        (A + 13 * CYCLE_S, (A + 13 * CYCLE_S, A + 14 * CYCLE_S)),
        (A - 5, (A, A + CYCLE_S)),  # before the anchor: the first cycle, never cycle -1
        (A - 10 * CYCLE_S, (A, A + CYCLE_S)),
    ],
)
def test_cycle_bounds_boundaries(now, expected):
    assert cycle_bounds(A, now) == expected


def test_cycle_start_is_bit_identical_across_a_cycle_and_contiguous():
    """``cycle_start`` keys the counter rows: two instants in one cycle must
    produce the same float, or one cycle's usage splits across two rows."""
    rng = random.Random(7)
    for n in (0, 1, 2, 7, 40):
        start, end = cycle_bounds(A, A + n * CYCLE_S)
        for _ in range(200):
            now = start + rng.random() * (end - start)
            if now >= end:
                continue
            assert cycle_bounds(A, now) == (start, end)
        assert cycle_bounds(A, end)[0] == end  # no gap, no overlap


def test_limit_service_exposes_the_same_cycle_bounds(tmp_path: Path):
    svc = LimitService.open(tmp_path / "l.db", keys=None, pricing=None)
    assert svc.cycle_bounds(A, A + CYCLE_S + 3) == cycle_bounds(A, A + CYCLE_S + 3)


# -- limit names ---------------------------------------------------------------


def test_every_fixed_name_parses_to_itself():
    assert len(FIXED_LIMIT_TYPES) == 14
    for name in FIXED_LIMIT_TYPES:
        assert parse_limit_type(name) == (name, None)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("model:gpt-4o:rpm", ("model:rpm", "gpt-4o")),
        ("model:meta-llama/Llama-3.1-8B:tpm", ("model:tpm", "meta-llama/Llama-3.1-8B")),
        ("model:acme/llama-3:8b:tokens_cycle", ("model:tokens_cycle", "acme/llama-3:8b")),
        ("model:a:b:c:usd_cycle", ("model:usd_cycle", "a:b:c")),
    ],
)
def test_per_model_names_split_on_the_last_colon(name, expected):
    assert parse_limit_type(name) == expected
    assert model_limit_type(expected[1], expected[0].removeprefix("model:")) == name


@pytest.mark.parametrize(
    "name",
    [
        "rpms",
        "",
        "model:",
        "model::rpm",
        "model:gpt-4o",
        "model:gpt-4o:rpd",
        "model:<id>:rpm",  # the template itself, unsubstituted
        "key:requests_cycle",
        "REQUESTS_CYCLE",
    ],
)
def test_unknown_names_are_refused_by_name(name):
    with pytest.raises(ValueError) as err:
        parse_limit_type(name)
    assert repr(name) in str(err.value)


def test_every_contract_type_has_a_kind_and_cycle_types_a_counter():
    bases = {parse_limit_type(t.replace("<id>", "m"))[0] for t in LIMIT_TYPES}
    assert bases == set(KIND)
    assert {f"model:{s}" for s in MODEL_LIMIT_SUFFIXES} <= bases
    assert {b for b, k in KIND.items() if k is LimitKind.CYCLE} == set(CYCLE_METRIC)
    assert kind_of("max_concurrent") is LimitKind.CONCURRENCY
    assert kind_of("model:x:rpm") is LimitKind.WINDOW
    assert kind_of("max_prompt_tokens_per_request") is LimitKind.REQUEST_CAP


def test_prompt_estimate_matches_admission_formula():
    """``gateway/admission.py:221``: chars/4 over messages, prompt and input,
    plus 4 tokens per message -- and IMAGE_TOKENS_ESTIMATE per image part,
    which admission's formula does not have."""
    body = {
        "messages": [
            {"role": "system", "content": "x" * 40},
            {"role": "user", "content": [{"type": "text", "text": "y" * 20}, {"type": "image"}]},
        ],
        "prompt": ["z" * 8, 5],
        "input": "w" * 12,
    }
    assert (
        estimate_prompt_tokens(body)
        == int((40 + 20 + 8 + 12) / 4) + 2 * 4 + IMAGE_TOKENS_ESTIMATE
    )
    assert estimate_prompt_tokens({}) == 0


def test_an_image_part_is_estimated_not_free():
    """Finding L10: an image used to count as zero prompt tokens."""
    url = "data:image/png;base64," + "A" * 4000
    body = {
        "messages": [
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": url}}],
            }
        ]
    }
    assert IMAGE_TOKENS_ESTIMATE == 1000
    assert estimate_prompt_tokens(body) == 1000 + 4


# -- pricing -------------------------------------------------------------------


@pytest.fixture
def pricing(tmp_path: Path) -> PricingStore:
    return PricingStore.open(tmp_path / "limits.db", clock=lambda: 1000.0)


def cost(store: PricingStore, **kw):
    args = dict(
        model="m",
        target_kind="local",
        prompt_tokens=1_000_000,
        completion_tokens=500_000,
        metered_usd=None,
        provider_in_per_mtok=None,
        provider_out_per_mtok=None,
    )
    args.update(kw)
    return store.cost(**args)


def test_the_stores_satisfy_their_ports(tmp_path: Path, pricing):
    assert isinstance(pricing, PricingPort)
    assert isinstance(LimitService.open(tmp_path / "l.db", keys=None, pricing=pricing), LimitPort)


def test_metered_wins_over_every_rate(pricing):
    pricing.set("m", 1.0, 2.0, actor="admin")
    assert cost(pricing, metered_usd=0.0) == (0.0, CostBasis.METERED)
    assert cost(pricing, metered_usd=0.37, provider_in_per_mtok=9, provider_out_per_mtok=9) == (
        0.37,
        CostBasis.METERED,
    )


def test_local_price_row_with_both_rates_estimates(pricing):
    pricing.set("m", 1.0, 2.0, actor="admin")
    assert cost(pricing) == (1.0 + 1.0, CostBasis.ESTIMATED)
    # A local price never prices a remote request.
    assert cost(pricing, target_kind="remote") == (None, CostBasis.UNPRICED)


@pytest.mark.parametrize(("rate_in", "rate_out"), [(1.0, None), (None, 2.0), (None, None)])
def test_a_half_priced_local_model_is_unpriced_not_cheap(pricing, rate_in, rate_out):
    pricing.set("m", rate_in, rate_out, actor="admin")
    assert cost(pricing) == (None, CostBasis.UNPRICED)


def test_provider_rates_estimate_when_both_known(pricing):
    assert cost(
        pricing, target_kind="remote", provider_in_per_mtok=3.0, provider_out_per_mtok=15.0
    ) == (
        3.0 + 7.5,
        CostBasis.ESTIMATED,
    )
    assert cost(pricing, target_kind="remote", provider_in_per_mtok=3.0) == (
        None,
        CostBasis.UNPRICED,
    )
    assert cost(pricing, model="unknown") == (None, CostBasis.UNPRICED)


def test_set_list_clear_and_reopen(tmp_path: Path, pricing):
    price = pricing.set("b", 0.5, 1.5, actor="alice")
    pricing.set("a", 0, 0, actor="bob")
    assert (price.model, price.set_by, price.set_at) == ("b", "alice", 1000.0)
    assert [p.model for p in pricing.list()] == ["a", "b"]
    assert pricing.set("b", 0.75, None, actor="carol").output_usd_per_mtok is None
    pricing.close()
    again = PricingStore.open(tmp_path / "limits.db")
    assert again.get("b").input_usd_per_mtok == 0.75 and again.get("b").set_by == "carol"
    again.clear("b", actor="alice")
    assert again.get("b") is None and [p.model for p in again.list()] == ["a"]


@pytest.mark.parametrize("bad", [-0.01, float("nan"), float("inf"), "1", True])
def test_rates_must_be_finite_non_negative_numbers(pricing, bad):
    with pytest.raises(ValueError):
        pricing.set("m", bad, 1.0, actor="admin")
    with pytest.raises(ValueError):
        pricing.set("m", 1.0, bad, actor="admin")
    assert pricing.get("m") is None


def test_pricing_fails_closed_on_an_unopenable_path(tmp_path: Path):
    blocker = tmp_path / "f"
    blocker.write_text("x")
    store = PricingStore.open(blocker / "limits.db")
    with pytest.raises(AuthUnavailable):
        store.get("m")
