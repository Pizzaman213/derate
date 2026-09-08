"""Provider-served models on the cluster screen.

`index.remotes` has been populated since the router was written -- one entry
per model of every enabled provider, created when the provider is added and
needing no traffic at all -- and nothing ever read it. So `/api/topology` knew
only about local deployments, and a model routed to a provider reached the
cluster screen as a substring inside the provider rail's sublabel and as
nothing else.

The fact that makes this worth reporting is `node_id`. A provider can BE a
machine on the roster: the Pi enrols as a GPU-less node and is also registered
as an Ollama provider, and a model pulled onto it runs on a box that is already
drawn on the screen. Until now the only thing that knew the provider was that
machine was a private helper inside a POST handler.

Its own file rather than more of `test_gateway.py`, which several sessions are
editing at once.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from control_plane.contracts.providers import Provider, ProviderKind, ProviderModel
from control_plane.gateway import create_app
from tests.fixtures import node_state
from tests.test_gateway import (
    FakeProviders,
    FakeRegistry,
    build_deps,
    make_node_profile,
)


def _model(served_name="qwen2.5-0.5b", upstream_id="qwen2.5:0.5b"):
    return ProviderModel(
        served_name=served_name,
        upstream_id=upstream_id,
        context_length=32768,
        supports_streaming=True,
        supports_tools=False,
        input_cost_per_mtok=None,
        output_cost_per_mtok=None,
    )


def _provider(
    provider_id="pi-ollama",
    *,
    base_url="http://192.168.11.99:11434/v1",
    enabled=True,
    models=None,
    healthy=True,
):
    return Provider(
        provider_id=provider_id,
        kind=ProviderKind.OLLAMA,
        display_name=provider_id,
        base_url=base_url,
        api_key_ref="",
        enabled=enabled,
        priority=10,
        models=list(models if models is not None else [_model()]),
        healthy=healthy,
        last_error=None,
        last_refreshed=0.0,
    )


def _client(providers, *, node_id="spark-99"):
    registry = FakeRegistry([node_state(make_node_profile(node_id))])
    deps = build_deps(registry=registry, providers=FakeProviders(providers))
    return TestClient(create_app(deps))


def _remotes(client):
    r = client.get("/api/topology")
    assert r.status_code == 200
    return r.json()["remotes"]


def test_a_provider_model_appears_in_topology_remotes():
    with _client([_provider()]) as c:
        remotes = _remotes(c)
    assert len(remotes) == 1
    row = remotes[0]
    assert row["provider_id"] == "pi-ollama"
    assert row["served_name"] == "qwen2.5-0.5b"
    assert row["upstream_id"] == "qwen2.5:0.5b"


def test_a_provider_hosted_on_a_roster_node_names_that_node():
    """`make_node_profile` sits at 192.168.11.99, so this provider IS that box.

    This is the whole point: the model is running on a machine already drawn on
    the cluster screen, and the screen could not say so.
    """
    with _client([_provider(base_url="http://192.168.11.99:11434/v1")]) as c:
        remotes = _remotes(c)
    assert remotes[0]["node_id"] == "spark-99"


def test_a_provider_matched_by_hostname_also_names_the_node():
    with _client([_provider(base_url="http://spark-99:11434/v1")]) as c:
        remotes = _remotes(c)
    assert remotes[0]["node_id"] == "spark-99"


def test_a_provider_not_on_the_roster_reports_no_node():
    """Null, not a hostname and not a guess. "Somebody else's machine" must
    stay distinguishable from "a machine we failed to recognise"."""
    with _client([_provider(base_url="https://openrouter.ai/api/v1")]) as c:
        remotes = _remotes(c)
    assert remotes[0]["node_id"] is None


def test_a_near_miss_address_is_not_matched():
    """No subnet inference, no DNS, no reverse lookup -- an exact match only."""
    with _client([_provider(base_url="http://192.168.11.98:11434/v1")]) as c:
        remotes = _remotes(c)
    assert remotes[0]["node_id"] is None


def test_a_disabled_provider_contributes_no_remote():
    with _client([_provider(enabled=False)]) as c:
        assert _remotes(c) == []


def test_no_provider_configured_reports_an_empty_list():
    """`[]`, never absent. A missing key reads as "this build does not support
    remotes" and a client would be right to hide the whole idea."""
    with _client([]) as c:
        r = c.get("/api/topology")
    body = r.json()
    assert "remotes" in body
    assert body["remotes"] == []


def test_every_model_of_a_provider_gets_a_row():
    provider = _provider(
        models=[
            _model("a-model", "a:1"),
            _model("b-model", "b:1"),
            _model("c-model", "c:1"),
        ]
    )
    with _client([provider]) as c:
        remotes = _remotes(c)
    assert [r["served_name"] for r in remotes] == ["a-model", "b-model", "c-model"]


def test_rows_are_ordered_stably():
    provider = _provider(models=[_model("z-model", "z:1"), _model("a-model", "a:1")])
    with _client([provider]) as c:
        first = _remotes(c)
        second = _remotes(c)
    assert [r["served_name"] for r in first] == ["a-model", "z-model"]
    assert first == second


def test_remotes_and_routing_agree_about_health():
    """Two endpoints describing one target must not disagree, which is why the
    state is read off the RouteTarget rather than off Provider.healthy."""
    with _client([_provider()]) as c:
        remotes = _remotes(c)
        routing = c.get("/api/routing").json()

    by_target = {
        t["target_id"]: t
        for cfg in routing
        for t in cfg["targets"]
    }
    for row in remotes:
        target = by_target.get(row["target_id"])
        assert target is not None, f"{row['target_id']} is not in /api/routing"
        assert (row["state"] == "healthy") == bool(target["healthy"])


def test_topology_keeps_every_key_it_had():
    """Additive. `remotes` is a new key beside the old ones, not a reshape."""
    with _client([_provider()]) as c:
        body = c.get("/api/topology").json()
    assert {
        "cluster_id",
        "coordinator",
        "nodes",
        "edges",
        "deployments",
    } <= set(body)


def test_a_remote_row_carries_a_throughput_figure():
    """Same call and window a deployment uses, so the two cannot report
    throughput in different units."""
    with _client([_provider()]) as c:
        remotes = _remotes(c)
    assert isinstance(remotes[0]["tokens_per_sec"], (int, float))


def test_no_key_material_reaches_the_payload():
    provider = _provider()
    provider.api_key_ref = "OLLAMA_API_KEY"
    with _client([provider]) as c:
        body = c.get("/api/topology").text
    assert "OLLAMA_API_KEY" not in body
    # base_url is deliberately omitted: /api/providers already carries it via
    # the path that is reviewed for redaction, and a second copy is a second
    # thing to keep scrubbed.
    assert "11434" not in body
