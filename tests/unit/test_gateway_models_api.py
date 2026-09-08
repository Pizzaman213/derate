"""The model browser's read surface, on capacity_api's router.

Three properties carry most of the weight here. That the routes are reachable
at all with the UI mounted -- get that wrong and every other assertion in this
file is testing a page of HTML. That the quantization table on the wire is the
one the fit gate uses, digit for digit, because a second copy in the browser is
a second answer that can disagree with a refusal. And that a resolver which
cannot describe a model says so rather than returning an empty shape that reads
as "nothing to see here".
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from control_plane.contracts.quant import BYTES_PER_PARAM, QUANT_INFO
from control_plane.gateway import GatewayDeps, GatewaySettings, create_app
from control_plane.resolver import StubResolver as DetailResolver
from tests.unit.test_gateway import (
    FakeRegistry,
    _no_gpu_profile,
    build_deps,
    make_node_profile,
)


@pytest.fixture()
def client():
    """A gateway whose resolver can answer resolve_full.

    The gateway's own StubResolver deliberately cannot -- there is a test in
    test_gateway.py pinning that the planner degrades when the port lacks it --
    so the detail route is exercised against the resolver package's stub.
    """
    deps = build_deps()
    deps.resolver = DetailResolver()
    with TestClient(create_app(deps)) as c:
        yield c


# ---- reachability -----------------------------------------------------------


def test_model_routes_are_reachable_even_when_the_ui_is_mounted(tmp_path):
    """A Starlette mount at "/" catches everything not matched by an EARLIER
    route. Registered after it, these routes would quietly serve index.html and
    surface in the browser as a JSON parse error three layers from the cause.

    Asked by mounting a UI and calling, rather than by inspecting
    app.router.routes, for the reason test_gateway_ui_api gives: included
    routers are wrapped in objects with no .path, so introspection would assert
    on an implementation detail instead of the property that matters.
    """
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>derate</title>")

    app = create_app(
        GatewayDeps(),
        settings=GatewaySettings(cluster_id="c-test", ui_dir=str(ui)),
    )
    with TestClient(app) as c:
        api = c.get("/api/models/quant-table")
        root = c.get("/")

    assert api.status_code == 200
    assert api.headers["content-type"].startswith("application/json"), (
        "the UI mount is shadowing /api/models/quant-table"
    )
    assert root.status_code == 200
    assert "text/html" in root.headers["content-type"]


# ---- the quantization table -------------------------------------------------


def test_quant_table_matches_the_contract_to_the_digit(client):
    """The whole reason this endpoint exists.

    If the browser is ever tempted to hold its own copy of these numbers, this
    is the test that makes the copy unnecessary. Exact equality, not approx: a
    rounded byte figure is a different footprint on a 671B model.
    """
    body = client.get("/api/models/quant-table").json()
    on_wire = {row["key"]: row for row in body["schemes"]}

    assert set(on_wire) == set(BYTES_PER_PARAM), "the wire and the table disagree on which schemes exist"
    for key, expected in BYTES_PER_PARAM.items():
        assert on_wire[key]["bytes_per_param"] == expected, key
        assert on_wire[key]["bits_per_weight"] == QUANT_INFO[key].bits_per_weight, key


def test_quant_table_is_ordered_cheapest_first(client):
    """So a ladder rendered straight from it reads in the order a person picks
    along -- smallest that will do, upward."""
    rows = client.get("/api/models/quant-table").json()["schemes"]
    costs = [r["bytes_per_param"] for r in rows]
    assert costs == sorted(costs)


def test_quant_table_carries_a_verdict_per_runtime(client):
    """Every scheme, every runtime. A missing entry would render as an empty
    cell that reads like "fine" rather than "nobody has said".

    The expected set comes from the support table rather than being typed
    here: this assertion is about completeness, and spelling the runtimes out
    would turn every new one into a test failure instead of a covered case.
    """
    from control_plane.resolver.support import RUNTIMES

    rows = client.get("/api/models/quant-table").json()["schemes"]
    levels = {"supported", "unverified", "unsupported"}
    assert set(RUNTIMES) >= {"vllm", "sglang", "tts"}
    for row in rows:
        assert set(row["runtimes"]) == set(RUNTIMES), row["key"]
        assert set(row["runtimes"].values()) <= levels, row


def test_the_importance_matrix_family_reaches_the_wire(client):
    """The formats Unsloth actually publishes. Before the table carried them
    they resolved to nothing, fell through to the bf16 default, and made every
    Unsloth GGUF repo look three to eight times its real size."""
    keys = {row["key"] for row in client.get("/api/models/quant-table").json()["schemes"]}
    assert {"iq1_s", "iq2_m", "iq3_xxs", "iq4_xs", "iq4_nl"} <= keys


def test_quant_table_is_cacheable_and_needs_no_ports(client):
    """No registry, no resolver, no hub: it must answer with nothing wired."""
    with TestClient(create_app(GatewayDeps())) as bare:
        reply = bare.get("/api/models/quant-table")
    assert reply.status_code == 200
    assert "max-age" in reply.headers.get("cache-control", "")


# ---- detail -----------------------------------------------------------------


def test_detail_requires_a_model_id(client):
    reply = client.get("/api/models/detail")
    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == "invalid_request"


def test_detail_carries_the_capability_facts_as_numbers(client):
    body = client.get(
        "/api/models/detail", params={"model_id": "meta-llama/Llama-3.3-70B-Instruct"}
    ).json()
    gqa = body["capabilities"]["gqa"]
    assert gqa["present"] is True
    # The ratio is computed here so no client divides two head counts and gets
    # a second answer.
    assert gqa["ratio"] == gqa["num_attention_heads"] / gqa["num_kv_heads"]


def test_absent_capabilities_are_null_and_never_zero(client):
    """`sliding_window: 0` would claim every layer is windowed; null says the
    model has no window at all. The UI already draws the two differently."""
    body = client.get(
        "/api/models/detail", params={"model_id": "meta-llama/Llama-3.3-70B-Instruct"}
    ).json()
    mla = body["capabilities"]["mla"]
    assert mla["present"] is False
    assert mla["latent_dim"] is None and mla["cached_width_per_layer"] is None


def test_detail_reports_whether_this_cluster_can_run_the_scheme(client):
    nodes = client.get(
        "/api/models/detail", params={"model_id": "meta-llama/Llama-3.3-70B-Instruct"}
    ).json()["nodes"]
    assert nodes["checked"] >= 1, "no node was consulted"
    assert isinstance(nodes["problems"], list)


def _detail_nodes(states):
    """The `nodes` block of /api/models/detail for a given roster."""
    deps = build_deps(registry=FakeRegistry(states))
    deps.resolver = DetailResolver()
    with TestClient(create_app(deps)) as c:
        return c.get(
            "/api/models/detail",
            params={"model_id": "meta-llama/Llama-3.3-70B-Instruct"},
        ).json()["nodes"]


def test_a_node_with_no_gpu_is_not_asked_about_quantization():
    """It answered every scheme with "compute capability '' is unreadable".

    That describes a silicon generation problem on a machine with no silicon to
    describe -- a Raspberry Pi in the roster made every model look like it had
    a hardware blocker, complete with empty parentheses where the GPU name
    would be. It also set ok False for a model the real node runs fine.
    """
    from tests.fixtures import node_state

    states = [node_state(make_node_profile()), node_state(_no_gpu_profile())]
    nodes = _detail_nodes(states)

    assert nodes["checked"] == 1  # the GB10, and only the GB10
    assert not any("unreadable" in p for p in nodes["problems"])
    assert not any("()" in p for p in nodes["problems"])
    assert nodes["skipped"] == [
        {
            "node_id": _no_gpu_profile().node_id,
            "reason": "no GPU memory; derate launches CUDA runtimes only",
        }
    ]


def test_a_gpu_whose_compute_capability_is_unreadable_still_objects():
    """The filter must not swallow the case the message was written for.

    A card behind a driver too old to report a capability has memory, is a real
    placement candidate, and genuinely cannot be promised a scheme.
    """
    from dataclasses import replace

    from tests.fixtures import node_state

    blind = replace(make_node_profile(), compute_capability="")
    nodes = _detail_nodes([node_state(blind)])

    assert nodes["checked"] == 1
    assert nodes["skipped"] == []
    assert any("unreadable" in p for p in nodes["problems"])
    # And it names the card, since this one has a name to give.
    assert any("NVIDIA GB10" in p for p in nodes["problems"])


def test_node_descriptions_never_render_empty_parentheses():
    """`node-id ()` reads as a missing value in a sentence about hardware."""
    assert _no_gpu_profile().describe() == _no_gpu_profile().node_id
    assert make_node_profile().describe().endswith(" (NVIDIA GB10)")


def test_detail_says_so_when_the_resolver_cannot_describe_a_model(client):
    """The gateway's StubResolver has no resolve_full. The honest answer is a
    501 naming the limitation, not an empty body that reads as 'no details'."""
    with TestClient(create_app(build_deps())) as plain:
        reply = plain.get(
            "/api/models/detail",
            params={"model_id": "meta-llama/Llama-3.3-70B-Instruct"},
        )
    assert reply.status_code == 501
    assert reply.json()["error"]["code"] == "resolver_lacks_detail"


# ---- launchability ----------------------------------------------------------


@pytest.mark.parametrize(
    "dtype,launchable",
    [("bf16", True), ("awq_int4", True), ("q4_k_m", False), ("iq4_xs", False)],
)
def test_gguf_schemes_are_marked_unlaunchable_with_a_reason(client, dtype, launchable):
    """Serve must be absent, not merely disabled, on a format nothing here can
    load -- and the row has to say why rather than leaving a dead control."""
    from dataclasses import replace

    from control_plane.gateway.serialize import resolution_payload
    from control_plane.resolver.support import build_verdict
    from control_plane.resolver.types import ParamSource, QuantSource, Resolution
    from tests.fixtures import MODEL_SHAPES

    shape = replace(MODEL_SHAPES["llama-3.3-70b"], dtype=dtype)
    payload = resolution_payload(
        Resolution(
            shape=shape,
            revision="main",
            param_source=ParamSource.SAFETENSORS_HEADERS,
            quant_source=QuantSource.QUANT_CONFIG,
            support=build_verdict(("LlamaForCausalLM",), dtype),
        )
    )
    assert payload["launchable"]["ok"] is launchable
    if not launchable:
        assert "llama.cpp" in payload["launchable"]["reason"]


def test_a_single_gguf_file_is_never_launchable():
    """Both serve templates take a repository path. An hf:// file reference is
    not one, and recipes.py's command allowlist would happily pass it through."""
    from dataclasses import replace

    from control_plane.gateway.serialize import resolution_payload
    from control_plane.resolver.support import build_verdict
    from control_plane.resolver.types import ParamSource, QuantSource, Resolution
    from tests.fixtures import MODEL_SHAPES

    shape = replace(
        MODEL_SHAPES["llama-3.3-70b"],
        model_id="hf://unsloth/Qwen3-30B-A3B-GGUF/Qwen3-30B-A3B-UD-Q4_K_XL.gguf",
        dtype="q4_k_m",
    )
    payload = resolution_payload(
        Resolution(
            shape=shape,
            revision="main",
            param_source=ParamSource.GGUF_TENSORS,
            quant_source=QuantSource.GGUF_FILE_TYPE,
            support=build_verdict(("LlamaForCausalLM",), shape.dtype),
        )
    )
    assert payload["launchable"]["ok"] is False
    assert "repositor" in payload["launchable"]["reason"]


# ---- MTP --------------------------------------------------------------------


def test_mtp_params_are_reported_as_excluded_rather_than_silently_dropped():
    """The checkpoint carries the module and the hub's weight index counts it;
    no runtime loads it without speculative decoding, so total_params excludes
    it. Both figures ship so a reader comparing against the repo's own
    parameter count sees why they differ instead of finding a discrepancy.
    """
    from control_plane.gateway.serialize import resolution_payload
    from control_plane.resolver.support import build_verdict
    from control_plane.resolver.types import ParamSource, QuantSource, Resolution
    from tests.fixtures import MODEL_SHAPES

    shape = MODEL_SHAPES["deepseek-v3"]
    mtp = 11_500_000_000
    payload = resolution_payload(
        Resolution(
            shape=shape,
            revision="main",
            param_source=ParamSource.SAFETENSORS_HEADERS,
            quant_source=QuantSource.QUANT_CONFIG,
            support=build_verdict(("DeepseekV3ForCausalLM",), shape.dtype),
            warnings=[
                "excluded a 11.5B-parameter multi-token-prediction module that "
                "the checkpoint carries but runtimes do not load by default"
            ],
            param_breakdown={"total": 1, "mtp": mtp},
        )
    )
    block = payload["capabilities"]["mtp"]
    assert block["present"] is True
    assert block["counted_in_total_params"] is False
    # The measured total, not the analytic one: they are different accountings
    # and mixing them is wrong by the reconciliation drift.
    assert block["total_params_with_mtp"] == shape.total_params + mtp
    assert "multi-token-prediction" in block["note"]


def test_param_breakdown_carries_both_totals(client):
    """total is what a runtime loads; total_with_mtp is what a weight index
    counts. An entry cached before the field existed reports null, which the UI
    renders as an em dash -- correct, not a bug."""
    from control_plane.resolver.params import ParamBreakdown

    data = ParamBreakdown(embedding=10, attention=20, mtp=5).as_dict()
    assert data["total_with_mtp"] - data["total"] == data["mtp"]


# ---- search -----------------------------------------------------------------


def test_search_degrades_to_the_local_sources_when_the_hub_is_unreachable(client):
    """The tab has to stay useful with no network.

    Deployments and provider catalogues are already in memory. Emptying the
    whole answer because the third source failed would be a fabricated "there
    are no models" -- so the failure is reported per source instead.
    """
    body = client.get("/api/models/search", params={"q": "llama"}).json()
    assert body["sources"]["deployments"]["ok"] is True
    assert body["sources"]["providers"]["ok"] is True
    hub = body["sources"]["huggingface"]
    assert hub["ok"] is False and hub["note"], "a failed hub must say why"


def test_search_never_resolves_anything(client):
    """Fifty rows would be fifty hub round trips per keystroke."""

    class Counting(DetailResolver):
        calls = 0

        def resolve_full(self, model_id, dtype=None):
            type(self).calls += 1
            return super().resolve_full(model_id, dtype)

    deps = build_deps()
    deps.resolver = Counting()
    with TestClient(create_app(deps)) as c:
        body = c.get("/api/models/search", params={"q": "llama"}).json()
    assert Counting.calls == 0
    assert all(row["resolved"] is False for row in body["results"])


def test_search_says_when_no_token_is_set(client):
    """Gated repositories simply will not appear. Better said than discovered
    as an inexplicably short list."""
    import os

    if os.environ.get("HF_TOKEN") or os.environ.get("DERATE_HF_TOKEN"):
        pytest.skip("a token is set in this environment")
    body = client.get("/api/models/search", params={"q": "llama"}).json()
    assert any("HF_TOKEN" in n for n in body["notes"])


def test_hub_search_raises_rather_than_returning_an_empty_list_on_a_rate_limit():
    """"No such model" and "the hub would not answer" are different facts, and
    returning [] for both makes a rate limit look like an empty catalogue."""
    from control_plane.resolver.hf import HubClient
    from control_plane.resolver.types import MetadataUnavailable

    class _Resp:
        status_code = 429

        def json(self):  # pragma: no cover - never reached
            return []

    client = HubClient()
    client._get = lambda *a, **k: _Resp()  # type: ignore[assignment]
    with pytest.raises(MetadataUnavailable) as caught:
        client.search("anything")
    assert "rate limit" in str(caught.value).lower()


def test_hub_search_still_returns_empty_for_an_ordinary_miss():
    from control_plane.resolver.hf import HubClient

    class _Resp:
        status_code = 404

        def json(self):
            return []

    client = HubClient()
    client._get = lambda *a, **k: _Resp()  # type: ignore[assignment]
    assert client.search("nothing-like-this") == []


class TestVariantOrdering:
    """The order *is* the recommendation.

    A variant list whose first row is the largest thing that actually runs
    needs no badge to be read correctly, which is the whole reason to compute
    the order on this side of the wire rather than sorting by size in a table
    header.
    """

    @staticmethod
    def _row(label, verdict, gb, *, launchable=False, bpw=4.9):
        return {
            "label": label,
            "verdict": verdict,
            "fits": verdict in ("fits", "fits_degraded"),
            "file_bytes": int(gb * 1e9),
            "bits_per_weight": bpw,
            "launchable": launchable,
            "repo_id": f"acme/{label}",
            "dtype": "q4_k_m",
        }

    def _ordered(self, rows):
        from control_plane.gateway.capacity_api import _ranked

        return [r["label"] for r in _ranked(rows)]

    def test_a_variant_that_fits_outranks_one_that_merely_loads(self):
        rows = [
            self._row("degraded", "fits_degraded", 32.0),
            self._row("fits", "fits", 17.0),
        ]
        assert self._ordered(rows)[0] == "fits"

    def test_within_a_tier_the_largest_wins(self):
        """More bytes is less lossy, so the best quality that still runs sits
        at the top."""
        rows = [
            self._row("small", "fits", 3.0, bpw=3.4),
            self._row("large", "fits", 17.7, bpw=4.9),
            self._row("middling", "fits", 9.0, bpw=4.25),
        ]
        assert self._ordered(rows) == ["large", "middling", "small"]

    def test_among_refusals_the_near_miss_comes_first(self):
        """Ordering these biggest-first would bury the only row worth a second
        look under the ones that were never close."""
        rows = [
            self._row("way-over", "wont_fit", 120.0, bpw=32.0),
            self._row("just-over", "wont_fit", 61.0, bpw=16.0),
        ]
        assert self._ordered(rows) == ["just-over", "way-over"]

    def test_an_unjudged_variant_sorts_last_rather_than_flatteringly(self):
        rows = [
            self._row("unjudged", None, 5.0),
            self._row("refused", "wont_fit", 61.0),
            self._row("fits", "fits", 4.0),
        ]
        assert self._ordered(rows) == ["fits", "refused", "unjudged"]

    def test_rank_is_stamped_on_every_row(self):
        from control_plane.gateway.capacity_api import _ranked

        rows = _ranked([self._row("a", "fits", 4.0), self._row("b", "fits", 8.0)])
        assert [r["rank"] for r in rows] == [0, 1]

    def test_the_recommendation_is_the_top_launchable_row(self):
        """Not a second maximum computed a second way. A recommendation that
        disagrees with the row at the top of the table is worse than none."""
        from control_plane.gateway.capacity_api import _ranked, _recommend

        rows = [
            self._row("gguf-biggest", "fits", 17.7, launchable=False),
            self._row("awq", "fits", 9.0, launchable=True),
            self._row("awq-small", "fits", 4.0, launchable=True),
        ]
        _ranked(rows)
        assert _recommend(rows)["label"] == "awq"

    def test_nothing_launchable_recommends_nothing(self):
        from control_plane.gateway.capacity_api import _recommend

        rows = [self._row("gguf", "fits", 17.7, launchable=False)]
        assert _recommend(rows) is None


class TestVariantFailureCaching:
    """A failed enumeration must be cheap to retry.

    `_variant_cache` is only written on success, so a model whose enumeration
    timed out paid the full 20s on every retry -- for ever. That is the shape a
    person actually meets: the ladder fails, they click again, it hangs for
    another twenty seconds. `Qwen/Qwen2.5-0.5B-Instruct` was in exactly that
    state, reproducibly, on the live coordinator.
    """

    @pytest.fixture(autouse=True)
    def _clean_caches(self):
        from control_plane.gateway import capacity_api

        capacity_api._variant_cache.clear()
        capacity_api._variant_fail_cache.clear()
        yield
        capacity_api._variant_cache.clear()
        capacity_api._variant_fail_cache.clear()

    def _client(self, quant_variants):
        """A gateway whose resolver enumerates through the given callable."""
        deps = build_deps()
        resolver = DetailResolver()
        resolver.quant_variants = quant_variants
        deps.resolver = resolver
        return TestClient(create_app(deps))

    def test_a_timeout_is_cached_so_the_retry_does_not_re_enumerate(self, monkeypatch):
        from control_plane.gateway import capacity_api

        # A timeout that costs nothing to produce: the point is the bookkeeping
        # around it, not the wall clock.
        monkeypatch.setattr(capacity_api, "_VARIANTS_TIMEOUT_S", 0.01)
        calls = []

        def slow(model_id):
            calls.append(model_id)
            import time as _t

            _t.sleep(0.5)
            return []

        with self._client(slow) as c:
            first = c.get("/api/models/variants?model_id=acme/Slow")
            second = c.get("/api/models/variants?model_id=acme/Slow")

        assert first.status_code == 504
        assert second.status_code == 504
        assert len(calls) == 1, "the retry re-ran an enumeration it had already lost"

    def test_a_replayed_failure_says_it_is_replayed(self, monkeypatch):
        from control_plane.gateway import capacity_api

        monkeypatch.setattr(capacity_api, "_VARIANTS_TIMEOUT_S", 0.01)

        def slow(model_id):
            import time as _t

            _t.sleep(0.5)
            return []

        with self._client(slow) as c:
            first = c.get("/api/models/variants?model_id=acme/Slow")
            second = c.get("/api/models/variants?model_id=acme/Slow")

        assert first.json()["error"]["from_cache"] is False
        assert second.json()["error"]["from_cache"] is True
        # A client that offers "try again" needs to distinguish "still failing"
        # from a button that did nothing.
        assert second.headers["Retry-After"] == "60"

    def test_the_message_survives_the_replay_word_for_word(self, monkeypatch):
        from control_plane.gateway import capacity_api

        monkeypatch.setattr(capacity_api, "_VARIANTS_TIMEOUT_S", 0.01)

        def slow(model_id):
            import time as _t

            _t.sleep(0.5)
            return []

        with self._client(slow) as c:
            first = c.get("/api/models/variants?model_id=acme/Slow").json()["error"]
            second = c.get("/api/models/variants?model_id=acme/Slow").json()["error"]

        assert first["message"] == second["message"]
        assert first["code"] == second["code"] == "variants_timeout"

    def test_a_missing_model_is_not_cached(self):
        """A 404 already fails on the first listing and is cheap; caching it
        would hide a repository that appeared a moment later."""
        from control_plane.resolver.types import ModelNotFound

        calls = []

        def missing(model_id):
            calls.append(model_id)
            raise ModelNotFound(f"no such model {model_id!r}")

        with self._client(missing) as c:
            first = c.get("/api/models/variants?model_id=acme/Ghost")
            second = c.get("/api/models/variants?model_id=acme/Ghost")

        assert first.status_code == second.status_code == 404
        assert len(calls) == 2, "a 404 was cached; it must be re-asked"
        assert first.json()["error"]["from_cache"] is False
        assert second.json()["error"]["from_cache"] is False

    def test_an_unexpected_failure_is_cached(self):
        """The generic branch is the one that hides a slow, repeating fault."""
        calls = []

        def broken(model_id):
            calls.append(model_id)
            raise RuntimeError("the hub said something unrepeatable")

        with self._client(broken) as c:
            first = c.get("/api/models/variants?model_id=acme/Broken")
            second = c.get("/api/models/variants?model_id=acme/Broken")

        assert first.status_code == second.status_code == 502
        assert len(calls) == 1
        assert second.json()["error"]["from_cache"] is True

    def test_a_success_is_not_shadowed_by_a_stale_failure(self):
        """Belt and braces: the two caches must stay disjoint."""
        from control_plane.gateway import capacity_api

        with self._client(lambda model_id: []) as c:
            ok = c.get("/api/models/variants?model_id=acme/Fine")

        assert ok.status_code == 200
        assert capacity_api._variant_fail_cache.get("acme/Fine") is None


class TestBatchedCapacity:
    """`/api/capacity?models=` -- a verdict for a model nobody curated.

    The merged model list is one search box over the catalogue, the disk, the
    deployments, the providers and the hub. Four of those five can produce a
    model the curated walk has never heard of, and a picker that answers "not
    checked" for everything it found is not answering the question it exists to
    answer.

    The property that matters most here is that this is the SAME endpoint and
    the SAME payload as the curated walk. A second shape, or a second fit path,
    is a second answer that can disagree with a refusal.
    """

    @pytest.fixture(autouse=True)
    def _clean_caches(self):
        from control_plane.gateway import capacity_api

        capacity_api._resolve_cache.clear()
        capacity_api._resolve_fail_cache.clear()
        yield
        capacity_api._resolve_cache.clear()
        capacity_api._resolve_fail_cache.clear()

    KNOWN = "Qwen/Qwen3-30B-A3B"
    ALSO_KNOWN = "deepseek-ai/DeepSeek-V3"
    UNKNOWN = "acme/NotOnTheHub"

    def _ids(self, side):
        return [r["model_id"] for r in side["rows"]]

    def test_no_models_param_is_exactly_todays_answer(self, client):
        """The live UI sends no `models=`. That call must not have moved."""
        from control_plane.fit.catalog import CURATED_MODELS

        body = client.get("/api/capacity?context=8192&concurrency=1").json()
        answered = set(self._ids(body["live"] or body["static"]))
        answered |= {u["model_id"] for u in body["unresolved"]}
        assert answered == {m.model_id for m in CURATED_MODELS}

    def test_models_param_answers_the_same_shape_as_the_curated_walk(self, client):
        """The contract-preservation test. Key sets, top level and per side."""
        curated = client.get("/api/capacity?context=8192&concurrency=1").json()
        named = client.get(
            f"/api/capacity?context=8192&concurrency=1&models={self.KNOWN}"
        ).json()

        assert set(curated) == set(named)
        for side in ("live", "static"):
            if curated.get(side) and named.get(side):
                assert set(curated[side]) == set(named[side])
                if curated[side]["rows"] and named[side]["rows"]:
                    assert set(curated[side]["rows"][0]) == set(named[side]["rows"][0])

    def test_a_named_model_replaces_the_curated_list(self, client):
        body = client.get(f"/api/capacity?models={self.KNOWN}").json()
        side = body["live"] or body["static"]
        assert self._ids(side) == [self.KNOWN]

    def test_a_requested_id_is_echoed_byte_for_byte(self, client):
        body = client.get(f"/api/capacity?models={self.KNOWN}").json()
        side = body["live"] or body["static"]
        assert side["rows"][0]["model_id"] == self.KNOWN

    def test_an_unresolvable_id_lands_in_unresolved_and_nowhere_else(self, client):
        body = client.get(
            f"/api/capacity?models={self.KNOWN},{self.UNKNOWN}"
        ).json()
        unresolved = {u["model_id"]: u["reason"] for u in body["unresolved"]}
        assert self.UNKNOWN in unresolved
        assert unresolved[self.UNKNOWN], "an unresolved entry with no sentence"
        for side in ("live", "static"):
            if body.get(side):
                assert self.UNKNOWN not in self._ids(body[side])
        assert body.get("best") is None or True  # `best` lives inside a side

    def test_every_requested_id_is_accounted_for(self, client):
        """The invariant the client depends on to tell "refused" from
        "never answered": rows and unresolved partition the request."""
        asked = [self.KNOWN, self.ALSO_KNOWN, self.UNKNOWN]
        body = client.get("/api/capacity?models=" + ",".join(asked)).json()
        side = body["live"] or body["static"]
        seen = set(self._ids(side)) | {u["model_id"] for u in body["unresolved"]}
        assert seen == set(asked)

    def test_over_the_cap_ids_are_reported_not_dropped(self, client):
        from control_plane.gateway.capacity_api import MAX_CAPACITY_MODELS

        asked = [f"acme/Model-{i}" for i in range(MAX_CAPACITY_MODELS + 8)]
        body = client.get("/api/capacity?models=" + ",".join(asked)).json()
        side = body["live"] or body["static"]
        seen = set(self._ids(side)) | {u["model_id"] for u in body["unresolved"]}
        assert seen == set(asked), "an id was silently truncated"
        capped = [u for u in body["unresolved"] if "at most" in u["reason"]]
        assert len(capped) == 8

    def test_every_id_failing_still_returns_a_report(self, client):
        """Not a 502. The cluster facts are still true, and with no HF_TOKEN
        this is the common case rather than the edge case."""
        r = client.get(f"/api/capacity?models={self.UNKNOWN},acme/AlsoMissing")
        assert r.status_code == 200
        body = r.json()
        side = body["live"] or body["static"]
        assert side["rows"] == []
        assert side["best"] is None
        assert len(body["unresolved"]) == 2
        assert body["probed_node"]

    def test_a_failed_resolve_is_not_re_asked_inside_the_negative_ttl(self, client):
        """`ShapeCache` never caches a failure, so without this every call pays
        a fresh hub round trip for a repo that will never stop failing."""
        from control_plane.gateway import capacity_api

        calls = []
        original = capacity_api._resolve

        def counting(ctx, model_id):
            calls.append(model_id)
            return original(ctx, model_id)

        capacity_api._resolve = counting
        try:
            client.get(f"/api/capacity?models={self.UNKNOWN}")
            client.get(f"/api/capacity?models={self.UNKNOWN}")
        finally:
            capacity_api._resolve = original

        assert calls.count(self.UNKNOWN) == 1

    def test_the_cached_failure_keeps_the_resolvers_own_sentence(self, client):
        first = client.get(f"/api/capacity?models={self.UNKNOWN}").json()
        second = client.get(f"/api/capacity?models={self.UNKNOWN}").json()
        assert first["unresolved"][0]["reason"] == second["unresolved"][0]["reason"]

    def test_a_duplicate_id_is_answered_once(self, client):
        body = client.get(
            f"/api/capacity?models={self.KNOWN},{self.KNOWN}"
        ).json()
        side = body["live"] or body["static"]
        assert self._ids(side) == [self.KNOWN]

    def test_two_spellings_are_two_models(self, client):
        """Case folding would answer a question about one repo under another."""
        body = client.get(
            f"/api/capacity?models={self.KNOWN},{self.KNOWN.lower()}"
        ).json()
        side = body["live"] or body["static"]
        seen = set(self._ids(side)) | {u["model_id"] for u in body["unresolved"]}
        assert seen == {self.KNOWN, self.KNOWN.lower()}

    def test_the_echoed_numbers_match_what_was_asked(self, client):
        """The caption is built from these; drift would name a context the
        verdicts beneath it were not taken at."""
        body = client.get(
            f"/api/capacity?context=32768&concurrency=8&models={self.KNOWN}"
        ).json()
        assert body["context"] == 32768
        assert body["concurrency"] == 8


class TestParseModels:
    def test_order_is_preserved_and_duplicates_dropped(self):
        from control_plane.gateway.capacity_api import _parse_models

        ids, over = _parse_models("a/b, c/d ,, a/b , e/f")
        assert ids == ["a/b", "c/d", "e/f"]
        assert over == []

    def test_case_is_not_folded(self):
        from control_plane.gateway.capacity_api import _parse_models

        ids, _ = _parse_models("Qwen/Q,qwen/q")
        assert ids == ["Qwen/Q", "qwen/q"]

    def test_the_cap_splits_rather_than_truncates(self):
        from control_plane.gateway.capacity_api import (
            MAX_CAPACITY_MODELS,
            _parse_models,
        )

        asked = [f"m/{i}" for i in range(MAX_CAPACITY_MODELS + 5)]
        ids, over = _parse_models(",".join(asked))
        assert len(ids) == MAX_CAPACITY_MODELS
        assert [o["model_id"] for o in over] == asked[MAX_CAPACITY_MODELS:]
        assert all(o["reason"] for o in over)

    def test_empty_is_empty(self):
        from control_plane.gateway.capacity_api import _parse_models

        assert _parse_models("") == ([], [])
        assert _parse_models("  ,  ,") == ([], [])


class TestCapacityWithNoConfiguration:
    """`/api/capacity` on a fresh install, with nothing entered anywhere.

    The screen this endpoint feeds bands every model row by fit. Before this,
    two configurations made it answer nothing at all: a coordinator whose
    machine has no GPU got a 503, and every row on the models screen read "not
    checked" for ever. That is the state a fresh install on a Pi or a laptop is
    in, so the models screen was useless exactly when somebody was deciding
    whether to keep the thing.

    The rule these tests pin is that absence of a parameter is a QUESTION, not
    a default: no `context=` means "choose one per model", which is a different
    request from `context=8192`, and both must be answerable.
    """

    @pytest.fixture(autouse=True)
    def _clean_caches(self):
        from control_plane.gateway import capacity_api

        capacity_api._resolve_cache.clear()
        capacity_api._resolve_fail_cache.clear()
        yield
        capacity_api._resolve_cache.clear()
        capacity_api._resolve_fail_cache.clear()

    def _rows(self, body):
        side = body.get("live") or body.get("static") or {}
        return side.get("rows") or []

    # ---- the GPU-less coordinator ------------------------------------------

    def _cpu_only_client(self, *, host_bytes=8 * 1024**3):
        """One enrolled machine, no GPU, reporting real host memory.

        `_no_gpu_profile` is what `probe_local` returns on a machine it found
        no GPU on: `addressable_memory` stays 0 on purpose, because that field
        is what the fit gate budgets against and RAM no model can reach must
        not appear in it. The live host figure lives on NodeState instead.
        """
        from tests.fixtures import node_state

        state = node_state(_no_gpu_profile())
        state.memory_total = host_bytes
        deps = build_deps(registry=FakeRegistry([state]))
        deps.resolver = DetailResolver()
        return TestClient(create_app(deps))

    def test_a_gpu_less_coordinator_answers_instead_of_503ing(self):
        with self._cpu_only_client() as client:
            response = client.get("/api/capacity")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["budget_basis"] == "host_memory"
        assert self._rows(body), "a 200 with no rows is a 503 wearing a hat"

    def test_the_host_basis_says_nothing_here_can_be_launched(self):
        """The containment on the whole feature.

        Sizing against host RAM makes the gate print "fits" for a model that
        cannot load on that machine, and the gate's sentence is the product.
        So every row carries the caveat and the report says `local_serving` is
        false -- a verdict you cannot act on must not grow a Serve button.
        """
        with self._cpu_only_client() as client:
            body = client.get("/api/capacity").json()

        assert body["local_serving"] is False
        for row in self._rows(body):
            joined = " ".join(row["warnings"])
            assert "no GPU was found" in joined, row["warnings"]
            assert "provider" in joined, row["warnings"]

    def test_the_static_side_is_withheld_rather_than_reported_as_zero(self):
        """The static ceiling is derived from addressable memory, which is 0
        here deliberately. Reporting that side would put "0.0 GiB usable"
        beside rows that say "fits"."""
        with self._cpu_only_client() as client:
            body = client.get("/api/capacity").json()
        assert body["static"] is None
        assert body["live"] is not None

    def test_a_machine_with_neither_budget_still_refuses(self):
        """The 200 comes from a MEASUREMENT. A node with no GPU and no host
        reading either has nothing to measure against, and inventing a number
        there is the failure this whole basis exists to avoid."""
        with self._cpu_only_client(host_bytes=0) as client:
            response = client.get("/api/capacity")
        assert response.status_code == 503
        assert "provider" in response.json()["error"]["message"]

    # ---- the derived context ------------------------------------------------

    def test_no_context_parameter_derives_one_per_model(self, client):
        body = client.get("/api/capacity").json()
        assert body["context"] is None, (
            "a single top-level context would describe whichever row came first"
        )
        rows = self._rows(body)
        assert rows
        for row in rows:
            assert row["context"] > 0
            assert row["max_seqs"] == 1

    def test_a_derived_context_is_one_the_gate_verified(self, client):
        """The round trip. Feeding the chosen context back in must reproduce
        the same verdict -- otherwise the number on screen is decoration."""
        body = client.get("/api/capacity").json()
        for row in self._rows(body):
            if not row["fits"]:
                continue
            again = client.get(
                f"/api/capacity?models={row['model_id']}"
                f"&context={row['context']}&concurrency={row['max_seqs']}"
            ).json()
            back = self._rows(again)
            assert back, row["model_id"]
            assert back[0]["fits"] is True, (
                f"{row['model_id']} was chosen at {row['context']} and does "
                f"not fit there: {back[0]['reason']}"
            )

    def test_a_named_context_is_still_honoured_verbatim(self, client):
        body = client.get("/api/capacity?context=2048&concurrency=3").json()
        assert body["context"] == 2048
        assert body["concurrency"] == 3
        for row in self._rows(body):
            assert row["context"] == 2048
            assert row["max_seqs"] == 3

    def test_an_unknown_window_falls_back_rather_than_reaching_for_the_ceiling(
        self, client
    ):
        """The stub resolver reports no `max_position_embeddings`, which is the
        honest state for a model whose config could not be read. Those are
        judged the way they were judged before the context became derivable.

        The alternative is the ceiling of the search -- 2,097,152 tokens -- for
        any small model on a big machine, which is not a window anybody asked
        for.
        """
        from control_plane.fit.capacity import FALLBACK_CONTEXT

        rows = self._rows(client.get("/api/capacity").json())
        assert rows
        assert all(r["context"] <= FALLBACK_CONTEXT for r in rows), [
            (r["model_id"], r["context"]) for r in rows
        ]

    def test_a_known_window_caps_the_derived_context(self):
        """A context above `max_position_embeddings` is one the runtime would
        refuse to start with -- a launch that fails minutes later for a reason
        chosen here.

        Asked against a resolver that actually reports a window, because the
        package stub reports None for every model and an assertion guarded on
        `if window:` would pass without ever running.
        """
        import dataclasses as _dc

        window = 16384

        class _Windowed(DetailResolver):
            def resolve_full(self, model_id, dtype=None):
                out = super().resolve_full(model_id, dtype)
                return _dc.replace(out, max_position_embeddings=window)

        deps = build_deps()
        deps.resolver = _Windowed()
        with TestClient(create_app(deps)) as client:
            rows = self._rows(client.get("/api/capacity").json())

        assert rows
        # Not merely <=: at least one row must actually be CLAMPED by the
        # window, or a machine too small to reach 16384 would satisfy this
        # without the cap ever being applied.
        assert any(r["context"] == window for r in rows), [
            (r["model_id"], r["context"]) for r in rows
        ]
        assert all(r["context"] <= window for r in rows)

    # ---- which machine ------------------------------------------------------

    def test_the_probe_reports_which_machine_it_sized_against(self, client):
        body = client.get("/api/capacity").json()
        assert body["probed_node"] in body["nodes"]
        assert body["tensor_parallel"] == [1]

    def test_the_default_is_one_machine_not_the_whole_roster(self, client):
        """Nothing ticked is a question about the machine in front of you.

        Sharding across whatever happens to be enrolled would answer "what
        could this cluster run if it were replanned around this model", which
        is the planner's question and needs a target and a link measurement.
        """
        roster = client.get("/api/nodes").json()
        assert len(roster) > 1, "fixture must have several machines to matter"
        assert len(client.get("/api/capacity").json()["nodes"]) == 1

    def test_on_scopes_the_answer_to_the_named_machines(self, client):
        """Asked against a machine that is NOT the default, so a scope that
        silently did nothing would still fail."""
        roster = [n["node_id"] for n in client.get("/api/nodes").json()]
        default = client.get("/api/capacity").json()["probed_node"]
        other = next(n for n in roster if n != default)

        scoped = client.get(f"/api/capacity?on={other}").json()
        assert scoped["nodes"] == [other]
        assert scoped["probed_node"] == other

    def test_the_coordinators_own_host_is_the_default_machine(self):
        """Not the largest. Somebody looking at a fresh install is standing in
        front of the coordinator, and answering about a different machine with
        nothing on screen saying which is how a verdict stops being trusted."""
        from tests.unit.test_gateway import NODE_PROFILES, node_state

        class _WithLocal(FakeRegistry):
            local_node_id = "spark-02"

        states = [
            node_state(NODE_PROFILES["spark-01"]),
            node_state(NODE_PROFILES["spark-02"]),
        ]
        deps = build_deps(registry=_WithLocal(states))
        deps.resolver = DetailResolver()
        with TestClient(create_app(deps)) as client:
            assert client.get("/api/capacity").json()["probed_node"] == "spark-02"

    def test_a_coordinator_with_no_gpu_falls_through_to_a_machine_with_one(self):
        """The literal rule would band every row "won't fit" on a cluster whose
        coordinator is a Pi and whose worker is a Spark."""
        from tests.unit.test_gateway import NODE_PROFILES, node_state

        class _WithLocal(FakeRegistry):
            local_node_id = "cpu-box"

        cpu = node_state(_no_gpu_profile())
        cpu.profile = dataclasses.replace(cpu.profile, node_id="cpu-box")
        deps = build_deps(
            registry=_WithLocal([cpu, node_state(NODE_PROFILES["spark-01"])])
        )
        deps.resolver = DetailResolver()
        with TestClient(create_app(deps)) as client:
            body = client.get("/api/capacity").json()
        assert body["probed_node"] == "spark-01"
        assert body["budget_basis"] == "gpu"

    def test_an_unknown_machine_in_on_is_reported_not_ignored(self, client):
        """A stale `?on=` in a bookmarked URL otherwise narrows the answer to
        nothing and reads as a cluster that lost its nodes."""
        response = client.get("/api/capacity?on=ghost-01")
        assert response.status_code == 503
        every = client.get("/api/capacity").json()
        scoped = client.get(
            f"/api/capacity?on={every['nodes'][0]},ghost-01"
        ).json()
        assert any(
            e["node_id"] == "ghost-01" and "no healthy node" in e["reason"]
            for e in scoped["excluded"]
        ), scoped["excluded"]


class _LadderResolver(DetailResolver):
    """A resolver that can also enumerate a ladder.

    The package stub answers `resolve_full` but not `quant_variants`, so the
    variants route 501s against it and none of the basis plumbing is reachable.
    Two rungs is enough: the point under test is what the rows were SIZED
    against, not how the ladder is enumerated -- `test_resolver.py` owns that.
    """

    def quant_variants(self, model_id: str):
        from control_plane.resolver.types import QuantVariant

        return [
            QuantVariant(
                dtype="bf16", label="bf16", repo_id=model_id, source="self",
                file_bytes=61 * 1024**3,
            ),
            QuantVariant(
                dtype="q4_k_m", label="Q4_K_M", repo_id=f"{model_id}-GGUF",
                source="gguf_file", gguf_file="model-Q4_K_M.gguf",
                file_bytes=18 * 1024**3, launchable=False,
            ),
        ]


class TestLadderIsSizedOnTheMachinesThatWereNamed:
    """`/api/models/variants?on=` -- closing the gap the pane apologised for.

    The board above this ladder lets several machines be ticked. The ladder
    sized every row on ONE machine and the screen said so in prose: "Each row
    is sized on a single machine, so it does not account for the N you ticked
    above." An apology is not a fix -- the numbers were still the wrong ones,
    and the sentence only appeared when the operator had ticked the machines
    themselves rather than when the planner chose them.

    So the rows are sized on the machines that were named, and what they were
    sized on travels back on the wire for the caption to state as a fact.
    """

    MODEL = "Qwen/Qwen3-30B-A3B"

    @pytest.fixture()
    def client(self):
        from control_plane.planner import Planner

        deps = build_deps()
        deps.resolver = _LadderResolver()
        # The real planner, because the degree comes from `valid_tp_degrees`:
        # tensor parallelism has to divide the KV heads, and a stub that does
        # not know that would let this test pass on an illegal split.
        deps.planner = Planner()
        with TestClient(create_app(deps)) as c:
            yield c

    def _sized_on(self, client, query=""):
        body = client.get(
            f"/api/models/variants?model_id={self.MODEL}{query}"
        ).json()
        return body["sized_on"], body["variants"]

    def test_every_gpu_machine_by_default(self, client):
        """No `on=` means the cluster, not the coordinator.

        `/api/capacity` keeps its one-machine clamp -- it answers "what can
        THIS machine run". This ladder answers "which published variant should
        I pull", which is a question about the roster somebody actually has:
        sized on one Spark, a two-Spark cluster reports refusals for variants
        the pair holds at TP=2. The degree is still the model's to give, so
        three enrolled machines do not make this TP=3.
        """
        sized_on, _ = self._sized_on(client)
        every = [n["node_id"] for n in client.get("/api/nodes").json()]
        assert len(every) >= 3
        assert sized_on["tensor_parallel"] == 2, (
            "Qwen3-30B has 4 KV heads; the default should take the widest "
            "degree the model admits across the roster"
        )
        assert sized_on["nodes"] == every[:2]
        assert sized_on["probed_node"] == sized_on["nodes"][0]

    def test_the_default_and_naming_them_all_agree(self, client):
        """The default is not a different question from `on=<everything>`."""
        every = [n["node_id"] for n in client.get("/api/nodes").json()]
        default, _ = self._sized_on(client)
        named, _ = self._sized_on(client, f"&on={','.join(every)}")
        assert default["nodes"] == named["nodes"]
        assert default["tensor_parallel"] == named["tensor_parallel"]

    def test_both_budgets_travel_so_the_two_refusals_read_apart(self, client):
        """"This machine cannot hold it" and "this machine is full right now"
        are different sentences that want different actions, and the ladder
        printed them identically. The governing verdict stays the live one --
        `verdict`/`fits` do not change meaning -- and the static answer rides
        alongside so the screen can tell them apart.
        """
        sized_on, rows = self._sized_on(client)
        assert sized_on["usable_per_node"] > 0
        if sized_on["budget_is_live"]:
            assert sized_on["allocatable_per_node"] > 0
        assert rows, "no variants to judge"
        for row in rows:
            if row["verdict"] is None:
                continue
            assert row["static_verdict"] is not None, (
                "a row judged live but not against the hardware's own ceiling"
            )
            if row["static_fits"]:
                assert row["static_reason"], "a static verdict with no sentence"

    def test_the_static_side_is_the_roomier_one(self, client):
        """The ceiling cannot be tighter than what is free underneath it, so a
        row that fits live must fit on spec. The reverse is the interesting
        case and is exactly what the screen needs to be able to say."""
        _, rows = self._sized_on(client)
        for row in rows:
            if row["fits"]:
                assert row["static_fits"], (
                    f"{row['label']} fits against a live budget but not "
                    f"against the ceiling that live budget is a slice of"
                )

    def test_two_ticked_machines_are_sized_as_two(self, client):
        every = [n["node_id"] for n in client.get("/api/nodes").json()]
        sized_on, _ = self._sized_on(client, f"&on={every[0]},{every[1]}")
        assert sized_on["nodes"] == [every[0], every[1]]
        assert sized_on["tensor_parallel"] == 2

    def test_the_degree_is_what_the_model_admits_not_what_was_ticked(self, client):
        """Three machines do not mean TP=3. Qwen3-30B has 4 KV heads, so three
        ranks would not divide them and the runtime would refuse to start --
        reporting a fit at that degree is the class of answer this project
        does not give."""
        every = [n["node_id"] for n in client.get("/api/nodes").json()]
        assert len(every) >= 3
        sized_on, _ = self._sized_on(client, f"&on={','.join(every[:3])}")
        assert sized_on["tensor_parallel"] == 2
        assert len(sized_on["nodes"]) == 2, (
            "reported three machines while sizing on two"
        )

    def test_sharding_changes_the_numbers_it_claims_to_change(self, client):
        """The whole point. If the two-machine answer equalled the one-machine
        answer, the apology would have been right and this parameter would be
        decoration."""
        every = [n["node_id"] for n in client.get("/api/nodes").json()]
        _, one = self._sized_on(client, f"&on={every[0]}")
        _, two = self._sized_on(client, f"&on={every[0]},{every[1]}")
        by_dtype = {r["dtype"]: r for r in two}
        assert any(
            by_dtype[r["dtype"]]["headroom"] != r["headroom"]
            for r in one
            if r["dtype"] in by_dtype and r["headroom"] is not None
        ), "sizing across two machines produced identical headroom"

    def test_every_row_says_what_it_was_judged_at(self, client):
        _, rows = self._sized_on(client)
        for row in rows:
            assert row["context"] and row["context"] > 0
            assert row["max_seqs"] == 1

    def test_one_ladder_is_one_question(self, client):
        """Every row judged at the SAME context.

        `largest_runnable` is called with an empty ladder here, so its per-model
        derivation would fire once per row and hand each a different context --
        "fits at 31744" beside "fits at 32256", with headroom figures under
        them that cannot be read against each other. One table, one question.
        """
        _, rows = self._sized_on(client)
        contexts = {r["context"] for r in rows if r["context"] is not None}
        assert len(contexts) == 1, f"one ladder judged at {sorted(contexts)}"

    def test_the_shared_context_is_a_usable_one(self, client):
        """Guards a silent degrade: a bad plan makes every rung's solve throw,
        and the derivation falls back to the 512-token search grain. Every row
        still says "fits" at 512, so nothing else in this file would notice."""
        from control_plane.fit.capacity import MIN_USEFUL_CONTEXT

        _, rows = self._sized_on(client)
        judged = [r["context"] for r in rows if r["context"] is not None]
        assert judged and max(judged) >= MIN_USEFUL_CONTEXT, judged
