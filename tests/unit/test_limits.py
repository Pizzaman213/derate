"""LimitService: every limit type, the check order, reservations, cycles.

The set-driven test reads ``.omp/work/auth-keys/sets/limit_types.json`` and
proves each member can be configured on a key, blocks exactly at its
threshold with the right status/code/limit_source, and reports when it
resets. A member with no scenario here fails the coverage test rather than
being skipped.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from control_plane.contracts.auth import (
    LIMIT_TYPES,
    ApiKey,
    AuthUnavailable,
    KeyStatus,
    LimitDecision,
    LimitScope,
    Org,
    Principal,
    PrincipalVia,
    Reservation,
    UsageActuals,
)
from control_plane.contracts.telemetry import CostBasis
from control_plane.limits import (
    LimitKind,
    LimitService,
    PricingStore,
    cycle_bounds,
    estimate_prompt_tokens,
    kind_of,
    parse_limit_type,
)
from control_plane.limits.cycle import CYCLE_S

ROOT = Path(__file__).resolve().parents[2]
LIMIT_SET = json.loads((ROOT / ".omp/work/auth-keys/sets/limit_types.json").read_text())["members"]

DAY = 86400.0
T0 = 1_760_000_000.0  # the key's created_at, and its cycle anchor
ORG_T0 = T0 - 10 * DAY  # the org is older: its cycles end on other days
NOW = T0 + 5 * DAY
#: Contains both ':' and '/', so a per-model limit name must split on the last colon.
MODEL = "acme/llama-3:8b"
OTHER = "other-model"
FALLBACK = "local-llama"


class FakeKeys:
    """In-memory KeyPort: the three methods LimitService reads."""

    def __init__(self) -> None:
        self.keys: dict[str, ApiKey] = {}
        self.orgs: dict[str, Org] = {}

    def get_key(self, key_id: str) -> ApiKey | None:
        return self.keys.get(key_id)

    def get_org(self, org_id: str) -> Org | None:
        return self.orgs.get(org_id)

    def effective_allowlist(self, key: ApiKey, org: Org, exposed: list[str]) -> list[str]:
        base = list(exposed) if org.allowed_models is None else list(org.allowed_models)
        if key.allowed_models is None:
            return base
        return [m for m in key.allowed_models if m in base]


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "limits.db"
        self.keys = FakeKeys()
        self.pricing = PricingStore.open(self.path)
        self.svc = self.open()
        self.org(limits={})

    def open(self) -> LimitService:
        return LimitService.open(self.path, keys=self.keys, pricing=self.pricing, clock=lambda: NOW)

    def org(self, org_id: str = "o1", *, limits: dict | None = None, **fields) -> Org:
        org = Org(
            org_id=org_id,
            name=org_id,
            created_at=ORG_T0,
            created_by="admin",
            limits=dict(limits or {}),
            **fields,
        )
        self.keys.orgs[org_id] = org
        return org

    def key(
        self, key_id: str = "k1", *, org_id: str = "o1", limits: dict | None = None, **fields
    ) -> ApiKey:
        key = ApiKey(
            key_id=key_id,
            name=key_id,
            org_id=org_id,
            created_by="admin",
            created_at=T0,
            expires_at=None,
            status=KeyStatus.ACTIVE,
            limits=dict(limits or {}),
            **fields,
        )
        self.keys.keys[key_id] = key
        return key

    def principal(self, key_id: str = "k1") -> Principal:
        key = self.keys.keys[key_id]
        return Principal(
            key=key,
            org=self.keys.orgs[key.org_id],
            via=PrincipalVia.API_KEY,
            ip="10.0.0.1",
            user_agent="pytest",
        )

    def check(
        self,
        key_id: str = "k1",
        *,
        model: str = MODEL,
        kind: str | None = "remote",
        body: dict | None = None,
        now: float = NOW,
        svc: LimitService | None = None,
    ) -> tuple[LimitDecision, Reservation | None]:
        return (svc or self.svc).check_and_reserve(
            self.principal(key_id), model=model, target_kind=kind, body=body or {}, now=now
        )

    def settle(
        self,
        res: Reservation,
        *,
        model: str = MODEL,
        kind: str | None = "remote",
        prompt: int = 0,
        completion: int = 0,
        cost: float | None = None,
        basis: CostBasis | None = None,
        now: float = NOW,
    ) -> None:
        if basis is None:
            basis = CostBasis.METERED if cost is not None else CostBasis.UNPRICED
        self.svc.settle(
            res,
            UsageActuals(
                prompt_tokens=prompt,
                completion_tokens=completion,
                cost_usd=cost,
                cost_basis=basis,
                target_kind=kind or "",
                model_served=model,
            ),
            now=now,
        )

    def run(
        self,
        key_id: str = "k1",
        *,
        model: str = MODEL,
        kind: str | None = "remote",
        body: dict | None = None,
        now: float = NOW,
        **usage,
    ) -> LimitDecision:
        decision, res = self.check(key_id, model=model, kind=kind, body=body, now=now)
        if res is not None:
            self.settle(res, model=model, kind=kind, now=now, **usage)
        return decision

    def state(self, name: str, *, scope=LimitScope.KEY, scope_id: str = "k1", now: float = NOW):
        states = {s.limit_type: s for s in self.svc.states(scope, scope_id, now=now)}
        return states[name]

    def counter(self, metric: str, *, scope: str = "key", scope_id: str = "k1") -> list[float]:
        conn = sqlite3.connect(self.path)
        try:
            rows = conn.execute(
                "SELECT value FROM cycle_counters WHERE scope=? AND scope_id=? AND metric=? "
                "ORDER BY cycle_start",
                (scope, scope_id, metric),
            ).fetchall()
        finally:
            conn.close()
        return [r[0] for r in rows]


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


KEY_CYCLE_END = cycle_bounds(T0, NOW)[1]


# -- the set-driven test -----------------------------------------------------


@dataclass(frozen=True)
class Blocked:
    decision: LimitDecision
    resets_at: float | None
    at: float = NOW


@dataclass(frozen=True)
class Scenario:
    value: float
    drive: Callable[[Harness, str], Blocked]
    status: int = 429
    code: str = "limit_exceeded"
    extra: dict = field(default_factory=dict)


def _cycle(kind: str | None, unit: str, *, priced_local: bool = False) -> Callable:
    """Configure 10, spend 9 (admitted), probe at 9 (admitted, spends 1), block at 10."""

    def spend(h: Harness, name: str, amount: int) -> None:
        if unit == "requests":
            for _ in range(amount):
                assert h.run(kind=kind).allowed, f"{name}: request under the limit refused"
            return
        usage = {
            "prompt": {"prompt": amount},
            "completion": {"completion": amount},
            "usd": {"cost": float(amount)},
        }[unit]
        assert h.run(kind=kind, **usage).allowed, f"{name}: request under the limit refused"

    def drive(h: Harness, name: str) -> Blocked:
        if priced_local:
            h.pricing.set(MODEL, 1.0, 1.0, actor="admin")
        h.key(limits={name: 10})
        spend(h, name, 9)
        spend(h, name, 1)
        decision, res = h.check(kind=kind)
        assert res is None
        return Blocked(decision, KEY_CYCLE_END)

    return drive


def _rpm(h: Harness, name: str) -> Blocked:
    h.key(limits={name: 3})
    assert h.run(now=NOW).allowed
    assert h.run(now=NOW + 1).allowed
    assert h.run(now=NOW + 2).allowed
    decision, _ = h.check(now=NOW + 3)
    return Blocked(decision, NOW + 60, at=NOW + 3)


def _tpm(h: Harness, name: str) -> Blocked:
    h.key(limits={name: 1000})
    assert h.run(now=NOW, prompt=999).allowed
    assert h.run(now=NOW + 1, prompt=1).allowed
    decision, _ = h.check(now=NOW + 2)
    return Blocked(decision, NOW + 60, at=NOW + 2)


def _concurrent(h: Harness, name: str) -> Blocked:
    h.key(limits={name: 2})
    assert h.check()[1] is not None
    assert h.check()[1] is not None
    decision, _ = h.check()
    return Blocked(decision, NOW)


def _max_output(h: Harness, name: str) -> Blocked:
    h.key(limits={name: 100})
    assert h.run(body={"max_tokens": 100}).allowed
    decision, _ = h.check(body={"max_tokens": 101})
    assert "100" in decision.message
    return Blocked(decision, None)


def _max_prompt(h: Harness, name: str) -> Blocked:
    h.key(limits={name: 50})
    at_cap = {"messages": [{"role": "user", "content": "x" * 184}]}
    over = {"messages": [{"role": "user", "content": "x" * 188}]}
    assert estimate_prompt_tokens(at_cap) == 50 and estimate_prompt_tokens(over) == 51
    assert h.run(body=at_cap).allowed
    decision, _ = h.check(body=over)
    assert "50" in decision.message and "51" in decision.message
    return Blocked(decision, None)


SCENARIOS: dict[str, Scenario] = {
    "requests_cycle": Scenario(10, _cycle("remote", "requests")),
    "tokens_total_cycle": Scenario(10, _cycle("remote", "prompt")),
    "tokens_input_cycle": Scenario(10, _cycle("remote", "prompt")),
    "tokens_output_cycle": Scenario(10, _cycle("remote", "completion")),
    "budget_usd_cycle": Scenario(10, _cycle("remote", "usd")),
    "local_tokens_cycle": Scenario(10, _cycle("local", "prompt")),
    "local_usd_cycle": Scenario(10, _cycle("local", "usd", priced_local=True)),
    "cloud_tokens_cycle": Scenario(10, _cycle("remote", "prompt")),
    "cloud_usd_cycle": Scenario(10, _cycle("remote", "usd")),
    "rpm": Scenario(3, _rpm),
    "tpm": Scenario(1000, _tpm),
    "max_concurrent": Scenario(2, _concurrent),
    "max_output_tokens_per_request": Scenario(100, _max_output, 400, "max_output_tokens_exceeded"),
    "max_prompt_tokens_per_request": Scenario(50, _max_prompt, 413, "max_prompt_tokens_exceeded"),
    "model:tokens_cycle": Scenario(10, _cycle("remote", "prompt")),
    "model:usd_cycle": Scenario(10, _cycle("remote", "usd")),
    "model:rpm": Scenario(3, _rpm),
    "model:tpm": Scenario(1000, _tpm),
}


def concrete(member: str) -> str:
    return member.replace("<id>", MODEL)


def test_the_limit_set_is_the_contract_and_every_member_has_a_scenario():
    assert LIMIT_SET == sorted(LIMIT_TYPES), "sets/limit_types.json is stale; regenerate it"
    assert {parse_limit_type(concrete(m))[0] for m in LIMIT_SET} == set(SCENARIOS)


@pytest.mark.parametrize("member", LIMIT_SET)
def test_every_limit_type_configures_blocks_at_threshold_and_reports_resets(h, member):
    name = concrete(member)
    base, served = parse_limit_type(name)
    assert served == (MODEL if member.startswith("model:") else None)
    scenario = SCENARIOS[base]
    blocked = scenario.drive(h, name)
    d = blocked.decision

    assert not d.allowed
    assert (d.status, d.code, d.limit_source) == (scenario.status, scenario.code, f"key:{name}")
    assert d.limit == scenario.value
    assert d.resets_at == blocked.resets_at
    is_cap = kind_of(name) is LimitKind.REQUEST_CAP
    assert (d.resets_at is None) == is_cap
    assert d.limit_source in d.message

    states = {s.limit_type: s for s in h.svc.states(LimitScope.KEY, "k1", now=blocked.at)}
    # Every key also carries the default rpm and max_concurrent unless it
    # names its own; a per-model rpm is a different limit and does not
    # replace either.
    assert set(states) == {name, "rpm", "max_concurrent"}
    state = states[name]
    assert (state.limit_type, state.scope, state.scope_id) == (name, LimitScope.KEY, "k1")
    assert state.limit == state.effective_limit == scenario.value
    assert (state.resets_at is None) == is_cap
    if is_cap:
        assert (state.used, state.fraction, state.near, state.at) == (0, 0, False, False)
    else:
        assert state.at and state.near and state.remaining == 0
        assert state.used == d.used
        assert state.resets_at == blocked.resets_at


# -- check order, scopes, targets ------------------------------------------


def test_allowlist_refusal_comes_first_and_names_the_model(h):
    h.key(allowed_models=["a"], limits={"max_output_tokens_per_request": 1})
    d, res = h.check(model="b", body={"max_tokens": 50})
    assert (d.allowed, d.status, d.code, res) == (False, 403, "model_not_allowed", None)
    assert "'b'" in d.message
    assert h.check(model="a", body={"max_tokens": 1})[0].allowed


def test_missing_max_tokens_is_injected_from_the_tightest_cap(h):
    h.org(limits={"max_output_tokens_per_request": 64})
    h.key(limits={"max_output_tokens_per_request": 128})
    d, res = h.check(body={"messages": []})
    assert d.allowed and d.inject_max_tokens == 64
    assert res is not None and res.estimated_output == 64
    d, _ = h.check(body={"max_completion_tokens": 65})
    assert (d.status, d.limit_source) == (400, "org:max_output_tokens_per_request")
    assert "64" in d.message
    d, _ = h.check(body={"max_tokens": 10})
    assert d.allowed and d.inject_max_tokens is None


def test_org_limit_blocks_with_org_source_across_its_keys(h):
    h.org(limits={"requests_cycle": 2, f"model:{MODEL}:rpm": 5})
    h.key("k1")
    h.key("k2")
    assert h.run("k1").allowed
    assert h.run("k2").allowed
    d = h.run("k1")
    assert (d.allowed, d.limit_source, d.used, d.limit) == (False, "org:requests_cycle", 2, 2)
    assert d.resets_at == cycle_bounds(ORG_T0, NOW)[1] != KEY_CYCLE_END
    assert h.state("requests_cycle", scope=LimitScope.ORG, scope_id="o1").at


def test_key_limit_is_checked_before_the_org_limit(h):
    h.org(limits={"requests_cycle": 1})
    h.key(limits={"requests_cycle": 1})
    assert h.run().allowed
    assert h.run().limit_source == "key:requests_cycle"


def test_rate_limits_are_checked_before_cycle_limits(h):
    h.key(limits={"requests_cycle": 1, "rpm": 1})
    assert h.run().allowed
    assert h.run().limit_source == "key:rpm"


def test_per_model_limits_apply_only_to_their_model(h):
    h.key(limits={f"model:{MODEL}:rpm": 1, f"model:{MODEL}:tokens_cycle": 5})
    assert h.run(prompt=100).allowed
    assert h.run().limit_source == f"key:model:{MODEL}:rpm"
    assert h.run(model=OTHER, prompt=100).allowed
    assert h.run(model=OTHER).allowed
    assert h.run(now=NOW + 61).limit_source == f"key:model:{MODEL}:tokens_cycle"


def test_local_and_cloud_pools_only_apply_to_their_target_kind(h):
    h.key(limits={"local_tokens_cycle": 10, "cloud_tokens_cycle": 10})
    assert h.run(kind="local", prompt=10).allowed
    assert h.run(kind="remote", prompt=10).allowed
    assert h.run(kind="local").limit_source == "key:local_tokens_cycle"
    assert h.run(kind="remote").limit_source == "key:cloud_tokens_cycle"
    # An unknown target applies neither pool; total-token limits still bind.
    assert h.run(kind=None).allowed


def test_rpm_window_slides(h):
    h.key(limits={"rpm": 2})
    assert h.run(now=NOW).allowed
    assert h.run(now=NOW + 30).allowed
    d = h.run(now=NOW + 59)
    assert (d.allowed, d.resets_at, d.used) == (False, NOW + 60, 2)
    assert h.run(now=NOW + 60).allowed  # the first one aged out
    assert not h.run(now=NOW + 61).allowed


def test_a_key_that_names_no_rpm_is_held_to_the_default(h):
    from control_plane.contracts.auth import DEFAULT_KEY_RPM

    h.key(limits={"requests_cycle": 1000})
    h.key("k2", limits={"rpm": 5})
    for i in range(DEFAULT_KEY_RPM):
        assert h.run(now=NOW + i * 0.1).allowed, i
    d = h.run(now=NOW + 13)
    assert (d.allowed, d.limit_source, d.limit) == (False, "key:rpm", DEFAULT_KEY_RPM)
    state = h.state("rpm")
    assert (state.limit, state.used) == (DEFAULT_KEY_RPM, DEFAULT_KEY_RPM)
    # A key that names its own rpm keeps it, lower or higher.
    for i in range(5):
        assert h.run("k2", now=NOW + i).allowed
    assert h.run("k2", now=NOW + 6).limit_source == "key:rpm"
    h.key("k3", limits={"rpm": 1000})
    for i in range(DEFAULT_KEY_RPM + 1):
        assert h.run("k3", now=NOW + i * 0.1).allowed, i



def test_a_key_that_names_no_max_concurrent_is_held_to_the_default(h):
    from control_plane.contracts.auth import DEFAULT_KEY_MAX_CONCURRENT

    h.key(limits={"rpm": 1000})
    for i in range(DEFAULT_KEY_MAX_CONCURRENT):
        d, res = h.check()
        assert d.allowed and res is not None, i
    d, res = h.check()
    assert (d.allowed, d.limit_source, d.limit, res) == (
        False,
        "key:max_concurrent",
        DEFAULT_KEY_MAX_CONCURRENT,
        None,
    )
    h.key("k2", limits={"max_concurrent": 2})
    assert h.check("k2")[0].allowed and h.check("k2")[0].allowed
    assert h.check("k2")[0].limit_source == "key:max_concurrent"


def test_string_or_float_n_counts_every_copy(h):
    # The engines coerce "4" and 4.0 to 4; counting them as one copy let a
    # request generate four times the output cap.
    h.key(limits={"max_output_tokens_per_request": 100})
    d, _ = h.check(body={"max_tokens": 50, "n": 4.0})
    assert (d.allowed, d.code) == (False, "max_output_tokens_exceeded")
    d, _ = h.check(body={"max_tokens": 50, "n": "4"})
    assert (d.allowed, d.status, d.code) == (False, 400, "invalid_request")
    assert "n must be an integer" in d.message
    d, _ = h.check(body={"max_tokens": 50, "best_of": 129.0})
    assert (d.allowed, d.status) == (False, 400)
    d, _ = h.check(body={"max_tokens": 50, "n": True})
    assert (d.allowed, d.status) == (False, 400)


def test_n_predict_is_an_output_ceiling(h):
    h.key(limits={"max_output_tokens_per_request": 100})
    d, _ = h.check(body={"n_predict": 101})
    assert (d.allowed, d.code) == (False, "max_output_tokens_exceeded")
    d, _ = h.check(body={"n_predict": "101"})
    assert (d.allowed, d.status) == (False, 400)
    d, res = h.check(body={"n_predict": 100})
    assert d.allowed and res.estimated_output == 100


def test_a_completions_batch_multiplies_the_output(h):
    h.key(limits={"max_output_tokens_per_request": 100})
    d, _ = h.check(body={"max_tokens": 40, "prompt": ["a", "b", "c"]})
    assert (d.allowed, d.code) == (False, "max_output_tokens_exceeded")
    d, _ = h.check(body={"max_tokens": 40, "prompt": [[1, 2], [3]]})
    assert d.allowed
    # A single tokenised prompt is one prompt, not a batch of ints.
    d, _ = h.check(body={"max_tokens": 90, "prompt": [1, 2, 3]})
    assert d.allowed


def test_a_prompt_batch_and_an_input_list_are_capped():
    """Admission charges KV for at most a node's concurrency, so an uncapped
    batch passed it and booked copies x 256 tokens for a million prompts."""
    from control_plane.limits.service import (
        MAX_BATCH_PROMPTS,
        MAX_COMPLETIONS,
        MAX_EMBEDDING_INPUTS,
        completions_requested,
    )

    assert completions_requested({"prompt": ["a"] * MAX_BATCH_PROMPTS}) == MAX_BATCH_PROMPTS
    with pytest.raises(ValueError, match=f"at most {MAX_BATCH_PROMPTS}"):
        completions_requested({"prompt": ["a"] * (MAX_BATCH_PROMPTS + 1)})
    # n x prompts is held to the same ceiling as n alone: 128 x 128 booked
    # 16,384 generations from a body of a few KB.
    half = MAX_COMPLETIONS // 2
    assert completions_requested({"n": 2, "prompt": ["a"] * half}) == MAX_COMPLETIONS
    with pytest.raises(ValueError, match=f"at most {MAX_COMPLETIONS} generations"):
        completions_requested({"n": 2, "prompt": ["a"] * (half + 1)})
    with pytest.raises(ValueError, match=f"at most {MAX_EMBEDDING_INPUTS}"):
        completions_requested({"input": ["a"] * (MAX_EMBEDDING_INPUTS + 1)})
    with pytest.raises(ValueError, match=f"at most {MAX_EMBEDDING_INPUTS}"):
        completions_requested({"input": [[1]] * (MAX_EMBEDDING_INPUTS + 1)})
    # One tokenised input is one input however long it is.
    assert completions_requested({"input": list(range(MAX_EMBEDDING_INPUTS + 1))}) == 1
    assert completions_requested({"input": ["a"] * MAX_EMBEDDING_INPUTS}) == 1
    # Ids the engines coerce (1.0, "1", True) are prompts all the same: counted
    # by entry, a batch of them was zero prompts and slipped the product cap.
    for spelling in ([1.0, 2.0], ["1", "2"], [True, 2]):
        with pytest.raises(ValueError, match=f"at most {MAX_COMPLETIONS} generations"):
            completions_requested({"n": 2, "prompt": [spelling] * (half + 1)})


def test_token_id_prompts_count_toward_the_prompt_cap(h):
    h.key(limits={"max_prompt_tokens_per_request": 100})
    d, _ = h.check(body={"prompt": list(range(101))})
    assert (d.allowed, d.code) == (False, "max_prompt_tokens_exceeded")
    d, _ = h.check(body={"input": [list(range(60)), list(range(41))]})
    assert (d.allowed, d.code) == (False, "max_prompt_tokens_exceeded")
    # ...whatever the engine coerces them from.
    d, _ = h.check(body={"prompt": [1.0] * 101})
    assert (d.allowed, d.code) == (False, "max_prompt_tokens_exceeded")
    d, _ = h.check(body={"prompt": [["1"] * 101]})
    assert (d.allowed, d.code) == (False, "max_prompt_tokens_exceeded")
    # llama-server tokenizes text inside a mixed prompt and a `prompt_string`.
    for prompt in ([["x" * 8000, 1]], [{"prompt_string": "x" * 8000}]):
        for name in ("prompt", "input"):
            d, _ = h.check(body={name: prompt})
            assert (d.allowed, d.code) == (False, "max_prompt_tokens_exceeded"), (name, prompt[0])


def test_the_longest_batch_member_is_estimated_as_one_tokenised_prompt():
    """Admission re-estimates the longest member alone; returned bare, a member
    of string ids read as a batch of one-character strings, ~4x low."""
    from control_plane.limits.service import largest_batch_prompt

    for ids in (["1"] * 4090, [1.0] * 4090, list(range(4090))):
        longest = largest_batch_prompt({"prompt": [ids[:10], ids]})
        assert estimate_prompt_tokens({"prompt": longest}) >= 4090
    # Weighed as the whole-body estimate counts it: text inside a mixed member
    # and a llama-server `prompt_string` are the long ones here.
    for big in (["x" * 8000, 1], {"prompt_string": "x" * 8000}):
        longest = largest_batch_prompt({"prompt": ["y" * 100, big]})
        assert longest == [big]
        assert estimate_prompt_tokens({"prompt": longest}) >= 2000


def test_tpm_counts_live_estimates_then_actual_tokens(h):
    h.key(limits={"tpm": 1000})
    body = {"max_tokens": 600}
    d1, r1 = h.check(body=body)
    assert d1.allowed
    d2, _ = h.check(body=body)  # 600+ live already
    assert d2.allowed
    d3, _ = h.check(body=body)  # 1200+ live: blocked on the estimates alone
    assert (d3.allowed, d3.limit_source) == (False, "key:tpm")
    h.settle(r1, prompt=1, completion=1)  # the estimate is replaced by 2 tokens
    assert h.state("tpm").used == 2 + estimate_prompt_tokens(body) + 600


def test_live_reservations_stop_concurrent_requests_jointly_overshooting(h):
    h.key(limits={"tokens_total_cycle": 1000})
    body = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 400}
    est = estimate_prompt_tokens(body) + 400
    held = [h.check(body=body) for _ in range(4)]
    admitted = [res for d, res in held if d.allowed]
    assert len(admitted) == 3  # 0, est, 2*est < 1000 <= 3*est
    blocked = held[3][0]
    assert blocked.used == 3 * est and blocked.limit_source == "key:tokens_total_cycle"


def test_a_stream_that_crosses_the_limit_settles_in_full_and_the_next_blocks(h):
    h.key(limits={"tokens_total_cycle": 1000})
    d, res = h.check(body={"max_tokens": 100})
    assert d.allowed
    other, res2 = h.check(body={"max_tokens": 100})
    assert other.allowed  # admitted while the first was in flight
    h.settle(res, prompt=200, completion=1300)  # crossed mid-flight
    assert h.counter("tokens_total") == [1500]
    d = h.check()[0]
    assert (d.allowed, d.used) == (False, 1500 + res2.estimated_prompt + res2.estimated_output)
    h.settle(res2, prompt=1, completion=1)
    assert h.check()[0].used == 1502


def test_32_threads_reserving_at_once_get_exactly_max_concurrent_slots(h):
    h.key(limits={"max_concurrent": 5})
    start = threading.Barrier(32)
    results: list[Reservation | None] = []
    guard = threading.Lock()

    def worker() -> None:
        start.wait()
        _, res = h.check()
        with guard:
            results.append(res)

    threads = [threading.Thread(target=worker) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r is not None for r in results) == 5
    assert h.state("max_concurrent").used == 5


def test_32_threads_never_oversubscribe_max_concurrent_or_requests_cycle(h):
    h.key(limits={"max_concurrent": 5, "requests_cycle": 40})
    start = threading.Barrier(32)
    guard = threading.Lock()
    tally = {"inflight": 0, "peak": 0, "admitted": 0}
    unexpected: list[str | None] = []
    deadline = time.monotonic() + 20

    def worker() -> None:
        start.wait()
        while time.monotonic() < deadline:
            d, res = h.check()
            if res is None:
                if d.limit_source == "key:requests_cycle":
                    return
                if d.limit_source != "key:max_concurrent":
                    unexpected.append(d.limit_source)
                    return
                time.sleep(0)
                continue
            with guard:
                tally["inflight"] += 1
                tally["admitted"] += 1
                tally["peak"] = max(tally["peak"], tally["inflight"])
            time.sleep(0.0005)
            with guard:
                tally["inflight"] -= 1
            h.settle(res, prompt=1, completion=1)

    threads = [threading.Thread(target=worker) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert unexpected == []
    assert tally["peak"] <= 5
    assert tally["admitted"] == 40
    assert h.counter("requests") == [40]
    assert h.state("max_concurrent").used == 0


# -- settle ---------------------------------------------------------------


def test_settle_counts_every_metric_for_key_and_org_once(h):
    h.key()
    d, res = h.check(kind="local")
    h.settle(res, kind="local", prompt=30, completion=12, cost=0.5, basis=CostBasis.ESTIMATED)
    h.settle(res, kind="local", prompt=30, completion=12, cost=0.5)  # second settle: no-op
    for scope, scope_id in (("key", "k1"), ("org", "o1")):
        got = {
            m: h.counter(m, scope=scope, scope_id=scope_id)
            for m in (
                "tokens_total",
                "tokens_input",
                "tokens_output",
                "requests",
                "usd",
                "local_tokens",
                "local_usd",
                "cloud_tokens",
                "cloud_usd",
                f"model:{MODEL}:tokens",
                f"model:{MODEL}:usd",
                "unpriced_requests",
            )
        }
        assert got == {
            "tokens_total": [42],
            "tokens_input": [30],
            "tokens_output": [12],
            "requests": [1],
            "usd": [0.5],
            "local_tokens": [42],
            "local_usd": [0.5],
            "cloud_tokens": [],
            "cloud_usd": [],
            f"model:{MODEL}:tokens": [42],
            f"model:{MODEL}:usd": [0.5],
            "unpriced_requests": [],
        }


def test_release_without_actuals_gives_everything_back(h):
    h.key(limits={"max_concurrent": 1, "rpm": 1, "requests_cycle": 1})
    d, res = h.check()
    assert d.allowed
    assert not h.check()[0].allowed
    h.svc.settle(res, None, now=NOW)
    assert h.counter("requests") == []
    assert h.state("rpm").used == 0
    assert h.check()[0].allowed


def test_unpriced_requests_never_count_toward_usd_limits(h):
    h.key(limits={"budget_usd_cycle": 1.0, "cloud_usd_cycle": 1.0})
    for _ in range(5):
        assert h.run(prompt=1000, completion=1000).allowed  # no cost: unpriced
    # A cost with an UNPRICED basis is not a price either.
    assert h.run(cost=9.0, basis=CostBasis.UNPRICED).allowed
    assert h.counter("unpriced_requests") == [6]
    assert h.counter("usd") == []
    assert h.state("budget_usd_cycle").used == 0
    assert h.run(cost=1.0).allowed
    d = h.run()
    assert (d.allowed, d.limit_source, d.used) == (False, "key:budget_usd_cycle", 1.0)
    assert "$1 of $1" in d.message


def test_settle_never_raises_when_the_store_fails_and_still_frees_the_slot(h, caplog):
    h.key(limits={"max_concurrent": 1})
    d, res = h.check()
    assert d.allowed
    conn = sqlite3.connect(h.path)
    conn.execute("DROP TABLE cycle_counters")
    conn.commit()
    conn.close()
    with caplog.at_level("ERROR", logger="control_plane.limits"):
        h.settle(res, prompt=1, completion=1)
    assert "was not counted" in caplog.text
    assert h.check()[0].allowed


# -- cloud fallback ----------------------------------------------------------


@pytest.mark.parametrize(
    "limit_type", ["cloud_tokens_cycle", "cloud_usd_cycle", "budget_usd_cycle"]
)
def test_cloud_pool_exhaustion_offers_the_local_fallback(h, limit_type):
    h.key(limits={limit_type: 1}, cloud_fallback_local_model=FALLBACK)
    assert h.run(prompt=1, cost=1.0).allowed
    d = h.run()
    assert (d.allowed, d.limit_source, d.fallback_model) == (False, f"key:{limit_type}", FALLBACK)
    # The caller re-checks with the local model; an unpriced local model
    # cannot spend a dollar, so even an exhausted budget lets it through.
    assert h.check(model=FALLBACK, kind="local")[0].allowed


def test_fallback_is_only_offered_for_the_cloud_pool_types_on_a_remote_target(h):
    h.org(limits={"cloud_usd_cycle": 1})
    h.key(
        limits={"tokens_total_cycle": 1, "budget_usd_cycle": 5}, cloud_fallback_local_model=FALLBACK
    )
    assert h.run(prompt=5).allowed
    d = h.run()
    assert (d.limit_source, d.fallback_model) == ("key:tokens_total_cycle", None)

    h.key("k2", limits={"budget_usd_cycle": 1}, cloud_fallback_local_model=FALLBACK)
    h.pricing.set(MODEL, 1.0, 1.0, actor="admin")
    assert h.run("k2", kind="local", cost=1.0).allowed
    d = h.run("k2", kind="local")
    assert (d.limit_source, d.fallback_model) == ("key:budget_usd_cycle", None)

    h.key("k3")  # no fallback configured; org pool exhausted
    assert h.run("k3", cost=1.0).allowed
    d = h.run("k3")
    assert (d.limit_source, d.fallback_model) == ("org:cloud_usd_cycle", None)

    h.key("k4", cloud_fallback_local_model=FALLBACK)  # org pool, key's fallback
    assert h.run("k4").fallback_model == FALLBACK


# -- raise, reset, cycles -----------------------------------------------------


def test_raise_for_cycle_keeps_the_counter_and_ends_with_the_cycle(h):
    h.key(limits={"requests_cycle": 2})
    assert h.run().allowed and h.run().allowed
    assert not h.run().allowed
    state = h.svc.raise_for_cycle(LimitScope.KEY, "k1", "requests_cycle", 4, actor="admin", now=NOW)
    assert (state.limit, state.effective_limit, state.used) == (2, 4, 2)
    assert h.counter("requests") == [2]
    assert h.run().allowed and h.run().allowed
    d = h.run()
    assert (d.allowed, d.limit, d.used) == (False, 4, 4)

    next_cycle = KEY_CYCLE_END
    st = h.state("requests_cycle", now=next_cycle)
    assert (st.effective_limit, st.used) == (2, 0)
    assert h.run(now=next_cycle).allowed and h.run(now=next_cycle).allowed
    assert h.run(now=next_cycle).limit == 2


def test_raise_refuses_a_limit_that_is_not_configured(h):
    h.key(limits={"rpm": 5})
    with pytest.raises(ValueError, match="requests_cycle"):
        h.svc.raise_for_cycle(LimitScope.KEY, "k1", "requests_cycle", 4, actor="admin")
    with pytest.raises(ValueError, match="nope"):
        h.svc.raise_for_cycle(LimitScope.KEY, "k1", "nope", 4, actor="admin")
    with pytest.raises(ValueError):
        h.svc.raise_for_cycle(LimitScope.KEY, "k1", "rpm", -1, actor="admin")
    with pytest.raises(ValueError, match="ghost"):
        h.svc.raise_for_cycle(LimitScope.KEY, "ghost", "rpm", 9, actor="admin")


def test_reset_zeroes_only_the_current_cycle(h):
    h.key(limits={"requests_cycle": 3})
    first = T0 + 1
    later = T0 + CYCLE_S + 1
    assert h.run(now=first).allowed and h.run(now=first).allowed
    assert h.run(now=later).allowed
    h.svc.reset_cycle(LimitScope.KEY, "k1", actor="admin", now=later)
    assert h.counter("requests") == [2]  # the first cycle's row remains
    assert h.state("requests_cycle", now=first).used == 2
    assert h.state("requests_cycle", now=later).used == 0
    conn = sqlite3.connect(h.path)
    rows = conn.execute("SELECT scope, scope_id, cycle_start, actor FROM cycle_resets").fetchall()
    conn.close()
    assert rows == [("key", "k1", T0 + CYCLE_S, "admin")]


def test_counters_and_overrides_survive_reopening_the_file(h):
    h.key(limits={"requests_cycle": 3, "tokens_total_cycle": 10_000})
    assert h.run(prompt=7).allowed and h.run(prompt=7).allowed
    h.svc.raise_for_cycle(LimitScope.KEY, "k1", "tokens_total_cycle", 20_000, actor="a", now=NOW)
    h.svc.close()
    for _ in range(2):
        svc = h.open()
        states = {s.limit_type: s for s in svc.states(LimitScope.KEY, "k1", now=NOW)}
        assert states["requests_cycle"].used == 2
        assert states["tokens_total_cycle"].used == 14
        assert states["tokens_total_cycle"].effective_limit == 20_000
        svc.close()
    h.svc = h.open()
    assert h.run().allowed
    assert h.run().limit_source == "key:requests_cycle"


def test_settle_lands_in_the_cycle_it_happens_in(h):
    h.key(limits={"requests_cycle": 5})
    d, res = h.check(now=KEY_CYCLE_END - 1)
    h.settle(res, now=KEY_CYCLE_END)
    assert h.state("requests_cycle", now=KEY_CYCLE_END - 1).used == 0
    assert h.state("requests_cycle", now=KEY_CYCLE_END).used == 1


# -- states ------------------------------------------------------------------


def test_states_mark_near_at_the_fraction_and_at_when_full(h):
    h.key(limits={"requests_cycle": 5, "max_prompt_tokens_per_request": 10})
    for _ in range(3):
        h.run()
    st = h.state("requests_cycle")
    assert (st.fraction, st.near, st.at, st.remaining) == (0.6, False, False, 2)
    h.run()
    st = h.state("requests_cycle")
    assert (st.fraction, st.near, st.at) == (0.8, True, False)
    h.run()
    st = h.state("requests_cycle")
    assert (st.near, st.at, st.resets_at) == (True, True, KEY_CYCLE_END)
    cap = h.state("max_prompt_tokens_per_request")
    assert (cap.used, cap.fraction, cap.resets_at, cap.near) == (0, 0, None, False)


def test_states_of_an_unknown_subject_are_empty(h):
    assert h.svc.states(LimitScope.KEY, "ghost") == []
    assert h.svc.states(LimitScope.ORG, "ghost") == []


def test_a_zero_limit_admits_nothing_and_reads_as_at(h):
    h.key(limits={"requests_cycle": 0})
    assert not h.run().allowed
    assert h.state("requests_cycle").at


# -- the store ----------------------------------------------------------------


def test_an_unopenable_store_fails_closed_and_is_retried(tmp_path):
    blocker = tmp_path / "data"
    blocker.write_text("not a directory")
    h = Harness(tmp_path)
    svc = LimitService.open(
        blocker / "limits.db", keys=h.keys, pricing=h.pricing, clock=lambda: NOW
    )
    h.key(limits={"requests_cycle": 5})
    with pytest.raises(AuthUnavailable):
        svc.check_and_reserve(h.principal(), model=MODEL, target_kind="remote", body={})
    with pytest.raises(AuthUnavailable):
        svc.states(LimitScope.KEY, "k1")
    blocker.unlink()
    blocker.mkdir()
    assert svc.check_and_reserve(h.principal(), model=MODEL, target_kind="remote", body={})[
        0
    ].allowed


def test_a_store_from_another_schema_is_refused(h):
    conn = sqlite3.connect(h.path)
    conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES('limits_schema_version', '99')")
    conn.commit()
    conn.close()
    h.key(limits={"requests_cycle": 5})
    with pytest.raises(AuthUnavailable, match="99"):
        h.svc.states(LimitScope.KEY, "k1")
    assert h.pricing.list() == []  # pricing keeps its own version key


def test_pricing_and_limits_share_one_file_with_separate_versions(h):
    h.key(limits={"requests_cycle": 5})
    h.pricing.set(MODEL, 1.0, 2.0, actor="admin")
    h.run()
    conn = sqlite3.connect(h.path)
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert meta == {"pricing_schema_version": "1", "limits_schema_version": "1"}
    assert {"meta", "local_pricing", "cycle_counters", "overrides", "cycle_resets"} <= tables


# -- served, cut short, multiplied (findings H5, L9, L10) -------------------------


def test_n_and_best_of_multiply_the_output_reservation_and_the_cap(h):
    h.key(limits={"max_output_tokens_per_request": 500})
    d, res = h.check(body={"max_tokens": 100, "n": 3})
    assert d.allowed and res.estimated_output == 300
    d, res = h.check(body={"max_tokens": 200, "n": 3})
    assert (d.allowed, d.status, d.used, res) == (False, 400, 600, None)
    assert d.code == "max_output_tokens_exceeded"
    d, res = h.check(body={"max_tokens": 100, "best_of": 6})
    assert (d.allowed, d.used) == (False, 600)
    # Nothing asked: the injected ceiling is per completion, so n of them fit.
    d, res = h.check(body={"n": 4})
    assert d.allowed and d.inject_max_tokens == 125 and res.estimated_output == 500


def test_n_without_a_cap_still_multiplies_the_reservation(h):
    h.key(limits={"tpm": 1000})
    d, res = h.check(body={"max_tokens": 300, "n": 3})
    assert d.allowed and res.estimated_output == 900
    assert h.state("tpm").used == 900


def test_a_started_request_settled_without_actuals_is_counted_not_voided(h):
    h.key(limits={"rpm": 5, "requests_cycle": 10})
    d, res = h.check(body={"max_tokens": 50})
    assert d.allowed
    h.svc.mark_started(res)
    h.svc.settle(res, None, now=NOW)
    assert h.counter("requests") == [1]
    assert h.counter("tokens_total") == [res.estimated_prompt + 50]
    assert h.state("rpm").used == 1
    assert h.state("requests_cycle").used == 1


def test_an_unstarted_request_settled_without_actuals_is_still_voided(h):
    h.key(limits={"rpm": 5})
    d, res = h.check()
    h.svc.settle(res, None, now=NOW)
    assert h.counter("requests") == []
    assert h.state("rpm").used == 0


def test_a_mixed_name_admitted_remote_rechecks_local_limits_on_a_local_target(h):
    h.key(limits={"local_tokens_cycle": 10})
    assert h.run(kind="local", prompt=10).allowed  # the local pool is spent
    d, res = h.check(kind="remote")  # admitted as REMOTE: cloud pools apply
    assert d.allowed
    local = h.svc.check_target_kind(res, "local", now=NOW)
    assert (local.allowed, local.limit_source) == (False, "key:local_tokens_cycle")
    assert h.svc.check_target_kind(res, "remote", now=NOW).allowed


def test_the_recheck_does_not_count_the_request_against_itself(h):
    # Were its own (REMOTE-booked) estimate counted, 9999 >= 9999 would block.
    h.key(limits={"local_tokens_cycle": 9_999})
    d, res = h.check(kind="remote", body={"max_tokens": 9_999})
    assert d.allowed
    assert h.svc.check_target_kind(res, "local", now=NOW).allowed


def test_an_absurd_n_is_a_400_and_leaks_no_slot(h):
    """``n`` with 400 digits used to overflow in _reserve after the live
    counters were bumped: a 500, and a max_concurrent slot never given back."""
    h.key(limits={"max_concurrent": 1})
    try:
        d, res = h.check(body={"n": 10**400})
    except OverflowError:
        d, res = None, None
    assert d is not None and d.status == 400 and res is None
    assert h.run().allowed


def test_a_fallback_that_lands_on_a_remote_target_still_answers_to_the_dollar_budget(h):
    """The fallback is re-checked as LOCAL, where an unpriced model spends no
    dollar, but a served name can also have a REMOTE target and the router may
    pick it. Re-checking only the ``cloud_*`` pools there let remote spend run
    past an exhausted ``budget_usd_cycle``."""
    h.key(limits={"budget_usd_cycle": 1}, cloud_fallback_local_model=FALLBACK)
    assert h.run(cost=1.0).allowed
    d = h.run()
    assert (d.allowed, d.limit_source, d.fallback_model) == (False, "key:budget_usd_cycle", FALLBACK)
    d, res = h.check(model=FALLBACK, kind="local")
    assert d.allowed and res is not None
    remote = h.svc.check_target_kind(res, "remote", now=NOW)
    assert (remote.allowed, remote.status, remote.limit_source) == (
        False, 429, "key:budget_usd_cycle"
    )


def test_a_fallback_landing_remote_answers_to_the_models_dollar_limit(h):
    name = f"model:{FALLBACK}:usd_cycle"
    h.key(limits={name: 1}, cloud_fallback_local_model=FALLBACK)
    assert h.run(model=FALLBACK, cost=1.0).allowed
    d, res = h.check(model=FALLBACK, kind="local")
    assert d.allowed and res is not None
    remote = h.svc.check_target_kind(res, "remote", now=NOW)
    assert (remote.allowed, remote.limit_source) == (False, f"key:{name}")


def test_a_remote_reservation_priced_at_its_rates_counts_as_live_dollars(h):
    """A remote reservation used to book $0, so every concurrent request saw
    the same dollar headroom and all of them ran past ``cloud_usd_cycle``."""
    h.key(limits={"cloud_usd_cycle": 1})
    body = {"max_tokens": 1000}
    principal = h.principal()
    rates = (0.0, 1000.0)  # 1000 output tokens at $1000/Mtok is $1
    first, res = h.svc.check_and_reserve(
        principal, model=MODEL, target_kind="remote", body=body, now=NOW, remote_rates=rates
    )
    assert first.allowed and res is not None
    second, held = h.svc.check_and_reserve(
        principal, model=MODEL, target_kind="remote", body=body, now=NOW, remote_rates=rates
    )
    assert (second.allowed, second.limit_source, held) == (False, "key:cloud_usd_cycle", None)
    # Settling the first without actuals gives its dollars back.
    h.svc.settle(res, None, now=NOW)
    assert h.svc.check_and_reserve(
        principal, model=MODEL, target_kind="remote", body=body, now=NOW, remote_rates=rates
    )[0].allowed


def test_remote_rates_never_price_a_local_reservation(h):
    h.key(limits={"local_usd_cycle": 1})
    body = {"max_tokens": 1000}
    for _ in range(3):
        d, _res = h.svc.check_and_reserve(
            h.principal(), model=MODEL, target_kind="local", body=body, now=NOW,
            remote_rates=(0.0, 1000.0),
        )
        assert d.allowed
