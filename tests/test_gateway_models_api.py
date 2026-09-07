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

import pytest
from fastapi.testclient import TestClient

from control_plane.contracts.quant import BYTES_PER_PARAM, QUANT_INFO
from control_plane.gateway import GatewayDeps, GatewaySettings, create_app
from control_plane.resolver import StubResolver as DetailResolver
from tests.test_gateway import (
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
    """Every scheme, both runtimes. A missing entry would render as an empty
    cell that reads like "fine" rather than "nobody has said"."""
    rows = client.get("/api/models/quant-table").json()["schemes"]
    levels = {"supported", "unverified", "unsupported"}
    for row in rows:
        assert set(row["runtimes"]) == {"vllm", "sglang"}, row["key"]
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
