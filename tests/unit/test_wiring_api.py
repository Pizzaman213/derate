"""``POST /api/wiring``: one batch of provider and routing changes.

Validated whole before anything is written; applied in order and stopped at
the first failure. A real ProviderService sits behind it, because catalogue
membership is the refusal the editor most needs to see verbatim.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from control_plane.contracts import RoutingPolicy
from control_plane.gateway import create_app
from tests.unit.test_gateway import build_deps, two_unequal_replicas
from tests.unit.test_providers import add_openrouter, make_service

LOCAL = "llama-3.3-70b"
SONNET = "anthropic/claude-sonnet-4.5"
MINI = "openai/gpt-4o-mini"
LLAMA = "meta-llama/llama-3.3-70b-instruct"


def _app(tmp_path):
    service = make_service(tmp_path)
    add_openrouter(service)
    service.update("openrouter", {"enabled_models": [SONNET, MINI]})
    app = create_app(build_deps(deployments=two_unequal_replicas(), providers=service))
    return app, service


def _enabled(service) -> set[str]:
    return {m["upstream_id"] for m in service.catalogue("openrouter") if m["enabled"]}


def _routing(client, name):
    return next(c for c in client.get("/api/routing").json() if c["served_name"] == name)


def test_a_dry_run_reports_an_unpublished_model_and_changes_nothing(tmp_path):
    app, service = _app(tmp_path)
    ops = [
        {"op": "provider.patch", "provider_id": "openrouter",
         "patch": {"enabled_models": [SONNET, "nobody/such-model"]}},
        {"op": "route.set", "served_name": LOCAL, "policy": "failover", "limits": {"rpm": 3}},
    ]
    with TestClient(app) as client:
        reply = client.post("/api/wiring", json={"dry_run": True, "ops": ops})
        assert reply.status_code == 200, reply.text
        body = reply.json()
        assert body["dry_run"] is True and body["applied"] == 0
        first, second = body["results"]
        assert first["index"] == 0 and first["op"] == "provider.patch"
        assert first["ok"] is False
        assert "does not publish nobody/such-model" in first["error"]["message"]
        assert second == {"index": 1, "op": "route.set", "ok": True, "error": None}
        assert _routing(client, LOCAL)["limits"] == {}
        assert _routing(client, LOCAL)["auto_selected"] is True
    assert _enabled(service) == {SONNET, MINI}


def test_one_invalid_op_refuses_the_whole_batch(tmp_path):
    app, service = _app(tmp_path)
    ops = [
        {"op": "route.set", "served_name": LOCAL, "policy": "failover", "limits": {"rpm": 3}},
        {"op": "provider.patch", "provider_id": "openrouter",
         "patch": {"enabled_models": ["nobody/such-model"]}},
        {"op": "route.set", "served_name": LOCAL, "policy": "failover", "limits": {"rps": 1}},
        {"op": "teleport"},
    ]
    with TestClient(app) as client:
        reply = client.post("/api/wiring", json={"ops": ops})
        assert reply.status_code == 400
        body = reply.json()
        assert body["error"]["code"] == "wiring_invalid"
        assert body["error"]["message"] == "Nothing was applied: 3 change(s) failed validation."
        oks = [r["ok"] for r in body["results"]]
        assert oks == [True, False, False, False]
        assert body["results"][2]["error"]["message"] == "limits may only set rpm and tpm."
        assert body["results"][3]["error"]["message"] == "unknown op 'teleport'"
        assert _routing(client, LOCAL)["limits"] == {}
    assert _enabled(service) == {SONNET, MINI}


def test_a_valid_batch_applies_every_op_in_order(tmp_path):
    app, service = _app(tmp_path)
    with TestClient(app) as client:
        # A served name with a slash has no PUT route of its own; the batch
        # is how the editor reaches it.
        app.state.ctx.router.set_policy(SONNET, RoutingPolicy.ROUND_ROBIN)
        assert _routing(client, SONNET)["auto_selected"] is False
        ops = [
            {"op": "provider.patch", "provider_id": "openrouter",
             "patch": {"enabled_models": [SONNET, LLAMA], "aliases": {LLAMA: LOCAL}}},
            {"op": "route.set", "served_name": LOCAL, "policy": "failover",
             "limits": {"rpm": 3, "tpm": 900}},
            {"op": "route.clear", "served_name": SONNET},
        ]
        reply = client.post("/api/wiring", json={"ops": ops})
        assert reply.status_code == 200, reply.text
        body = reply.json()
        assert body["dry_run"] is False and body["applied"] == 3
        assert all(r["ok"] for r in body["results"])

        local = _routing(client, LOCAL)
        assert local["policy"] == "failover" and local["auto_selected"] is False
        assert local["limits"] == {"rpm": 3, "tpm": 900}
        # The aliased cloud model now sits behind the local name as a target.
        assert any(t["kind"] == "remote" for t in local["targets"])
        assert _routing(client, SONNET)["auto_selected"] is True
        entries = client.get("/api/audit").json()["entries"]
        actions = [e["action"] for e in entries]
        assert actions.count("routing_changed") >= 2 and "provider_updated" in actions
    assert _enabled(service) == {SONNET, LLAMA}
    served = {m.upstream_id: m.served_name for m in service.get("openrouter").models}
    assert served[LLAMA] == LOCAL


def test_a_key_cannot_ride_in_a_wiring_batch(tmp_path):
    app, _service = _app(tmp_path)
    with TestClient(app) as client:
        reply = client.post(
            "/api/wiring",
            json={"dry_run": True, "ops": [
                {"op": "provider.patch", "provider_id": "openrouter",
                 "patch": {"api_key": "sk-or-v1-anotherkey"}},
            ]},
        )
    row = reply.json()["results"][0]
    assert row["ok"] is False
    assert row["error"]["message"] == (
        "api_key cannot be set through /api/wiring; use PATCH /api/providers/{id}."
    )


def test_an_apply_failure_stops_the_batch_and_names_what_was_not_attempted(tmp_path):
    app, service = _app(tmp_path)
    ops = [
        {"op": "provider.patch", "provider_id": "openrouter", "patch": {"enabled_models": [SONNET]}},
        {"op": "route.set", "served_name": LOCAL, "policy": "failover", "limits": {"rpm": 3}},
        {"op": "route.clear", "served_name": SONNET},
    ]
    with TestClient(app) as client:
        router = app.state.ctx.router

        def broken(*_a, **_k):
            raise RuntimeError("routing.json is on a read-only disk")

        router.set_policy = broken
        reply = client.post("/api/wiring", json={"ops": ops})
        assert reply.status_code == 200
        body = reply.json()
        assert body["applied"] == 1
        first, second, third = body["results"]
        assert first["ok"] is True
        assert second["ok"] is False
        assert "routing.json is on a read-only disk" in second["error"]["message"]
        assert third["ok"] is False
        assert third["error"] == {
            "message": "Not attempted: an earlier change failed.", "code": "not_applied",
        }
    assert _enabled(service) == {SONNET}


def test_a_batch_is_capped_at_200_ops(tmp_path):
    app, _service = _app(tmp_path)
    ops = [{"op": "route.clear", "served_name": LOCAL}] * 201
    with TestClient(app) as client:
        reply = client.post("/api/wiring", json={"dry_run": True, "ops": ops})
    assert reply.status_code == 400
    assert reply.json()["error"]["message"] == "at most 200 ops per request"


def test_a_dry_run_pin_to_an_unlisted_host_is_refused_with_the_sentence(tmp_path):
    app, service = _app(tmp_path)
    ops = [{"op": "provider.patch", "provider_id": "openrouter",
            "patch": {"backend_pins": {SONNET: "nope"}}}]
    with TestClient(app) as client:
        reply = client.post("/api/wiring", json={"dry_run": True, "ops": ops})
    assert reply.status_code == 200, reply.text
    row = reply.json()["results"][0]
    assert row["ok"] is False
    assert row["error"]["message"] == (
        f"openrouter lists no backend 'nope' for {SONNET}; pick one from its backend list"
    )
    assert service.backend_pin_state("openrouter") == {}


def test_an_applied_pin_shows_on_the_provider_with_the_hosts_price(tmp_path):
    app, _service = _app(tmp_path)
    ops = [{"op": "provider.patch", "provider_id": "openrouter",
            "patch": {"backend_pins": {SONNET: "google-vertex"},
                      "backend_fallbacks": {SONNET: True}}}]
    with TestClient(app) as client:
        reply = client.post("/api/wiring", json={"ops": ops})
        assert reply.status_code == 200, reply.text
        assert reply.json()["applied"] == 1
        provider = next(
            p for p in client.get("/api/providers").json() if p["provider_id"] == "openrouter"
        )
        bad = client.patch("/api/providers/openrouter", json={"backend_pins": {SONNET: "nope"}})
    rows = {m["upstream_id"]: m for m in provider["models"]}
    assert rows[SONNET]["backend_pin"] == "google-vertex"
    assert rows[SONNET]["backend_fallback"] is True
    assert rows[SONNET]["input_cost_per_mtok"] == 3.5
    assert rows[SONNET]["output_cost_per_mtok"] == 17.5
    assert rows[MINI]["backend_pin"] is None and rows[MINI]["backend_fallback"] is False
    assert bad.status_code == 400
    assert "lists no backend 'nope'" in bad.json()["error"]["message"]
