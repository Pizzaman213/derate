"""Every admin mutation leaves a rich audit row (plan F2), plus the 3.3 logo
cache header and the 3.9 hash-verified install command."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from control_plane.gateway import create_app, internal_api
from control_plane.gateway.enroll_api import build_command, install_script_bytes
from control_plane.providers.logos import LogoCache
from tests.fixtures import SPARK_01, SPARK_02
from tests.unit.test_enrollment import FakeClient
from tests.unit.test_gateway import (
    SECRET_KEY,
    FakeJoinableRegistry,
    _real_providers,
    build_deps,
)
from control_plane.gateway import GatewayDeps
from control_plane.registry import Registry, RegistryConfig


def TC(app) -> TestClient:
    """A client on loopback: the first admin may only be created from there."""
    return TestClient(app, client=("127.0.0.1", 50000))


def coordinator_client(tmp_path) -> TestClient:
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    config = RegistryConfig(data_dir=tmp_path, token="cluster-token", agent_port=8081)
    registry = Registry(config=config, local_profile=SPARK_01, client=client)
    return TC(create_app(deps=GatewayDeps(registry=registry)))


def _rows(client, action: str) -> list[dict]:
    entries = client.get("/api/audit", params={"page_size": 500}).json()["entries"]
    return [e for e in entries if e["action"] == action]


def _one(client, action: str, target_id: str) -> dict:
    rows = [r for r in _rows(client, action) if r["target_id"] == target_id]
    assert len(rows) == 1, (action, target_id, rows)
    row = rows[0]
    assert row["request_id"] and row["request_id"].startswith("r-")
    return row


def _no_generic_row(client, request_id: str) -> None:
    assert not [r for r in _rows(client, "api_write") if r["request_id"] == request_id]


def test_provider_create_update_delete_are_audited_without_the_key(tmp_path):
    providers = _real_providers(tmp_path)
    with TC(create_app(build_deps(providers=providers))) as client:
        added = client.post("/api/providers", json={"kind": "openrouter", "api_key": SECRET_KEY})
        assert added.status_code == 201
        pid = added.json()["provider_id"]
        assert client.patch(f"/api/providers/{pid}", json={"priority": 5}).status_code == 200
        assert client.delete(f"/api/providers/{pid}").status_code == 200

        created = _one(client, "provider_created", pid)
        updated = _one(client, "provider_updated", pid)
        deleted = _one(client, "provider_deleted", pid)
        _no_generic_row(client, created["request_id"])
        assert updated["after"]["priority"] == 5
        assert deleted["before"]["provider_id"] == pid
        dump = client.get("/api/audit", params={"page_size": 500}).text
    assert SECRET_KEY not in dump


def test_settings_patch_records_before_and_after():
    with TC(create_app(build_deps())) as client:
        before = client.get("/api/settings").json()["local_only"]
        assert client.patch("/api/settings", json={"local_only": not before}).status_code == 200
        row = _one(client, "settings_changed", "local_only")
    assert row["before"] == {"local_only": before}
    assert row["after"] == {"local_only": not before}


def test_setup_complete_is_audited():
    with TC(create_app(build_deps())) as client:
        assert client.post("/api/setup/complete").status_code == 200
        _one(client, "settings_changed", "complete")


def test_enrollment_mint_and_revoke_rows_never_carry_the_token(tmp_path):
    api = coordinator_client(tmp_path)
    body = api.post("/api/enroll", json={}).json()
    token = body["command"].rsplit("--token ", 1)[1].strip()
    token_id = body["token_id"]
    assert api.delete(f"/api/enroll/{token_id}").status_code == 204

    minted = _one(api, "enrollment_minted", token_id)
    _one(api, "enrollment_revoked", token_id)
    assert minted["after"]["token_id"] == token_id
    assert minted["after"]["expires_at"] == body["expires_at"]
    assert token not in api.get("/api/audit", params={"page_size": 500}).text


def test_routing_put_and_delete_are_audited():
    with TC(create_app()) as client:
        assert client.put(
            "/api/routing/llama-3.3-70b", json={"policy": "failover"}
        ).status_code == 200
        assert client.delete("/api/routing/llama-3.3-70b").status_code == 200
        rows = [r for r in _rows(client, "routing_changed") if r["target_id"] == "llama-3.3-70b"]
    assert len(rows) == 2
    assert all(r["request_id"] for r in rows)
    assert {r["after"]["policy"] for r in rows if r["after"]["override"]} == {"failover"}


def test_node_admit_and_remove_are_audited():
    registry = FakeJoinableRegistry()
    with TC(create_app(build_deps(registry=registry))) as client:
        node_id = registry.states[0].profile.node_id
        assert client.post(f"/api/nodes/{node_id}/admit").status_code == 200
        assert client.delete(f"/api/nodes/{node_id}").status_code == 200
        _one(client, "node_admitted", node_id)
        _one(client, "node_removed", node_id)


def test_node_admitted_names_what_it_admitted(tmp_path):
    """N-15: the row says which machine the click let in, not only its name."""
    from tests.unit.test_registry import FakeClient as AgentFake, make_profile, make_registry

    candidate = make_profile("spark-04", address="10.0.0.14")
    registry = make_registry(
        tmp_path, client=AgentFake(), local=make_profile("spark-01", address="10.0.0.11")
    )
    registry.offer_candidate(candidate, "http://10.0.0.14:8081", source="join")
    with TC(create_app(build_deps(registry=registry))) as client:
        assert client.post("/api/nodes/spark-04/admit").status_code == 200
        row = _one(client, "node_admitted", "spark-04")
    after = row["after"]
    assert after["node_id"] == "spark-04"
    assert after["agent_url"] == "http://10.0.0.14:8081"
    assert after["address"] == "10.0.0.14"
    assert after["source"] == "join"
    assert isinstance(after["first_seen"], (int, float))


def test_deployment_launch_and_stop_are_audited():
    with TC(create_app(build_deps())) as client:
        launched = client.post(
            "/api/deployments", json={"model_id": "meta-llama/Llama-3.3-70B-Instruct"}
        )
        assert launched.status_code == 201, launched.text
        dep = launched.json()
        assert client.delete(f"/api/deployments/{dep['deployment_id']}").status_code == 200
        up = _one(client, "deployment_launched", dep["deployment_id"])
        down = _one(client, "deployment_stopped", dep["deployment_id"])
    assert up["after"]["model_id"] == "meta-llama/Llama-3.3-70B-Instruct"
    assert up["after"]["served_name"] == dep["served_name"]
    assert up["after"]["node_ids"]
    assert down["before"]["served_name"] == dep["served_name"]


def test_resolver_cache_clear_is_audited():
    class Cache:
        directory = None

        def clear(self):
            pass

    deps = build_deps()
    deps.resolver.cache = Cache()
    with TC(create_app(deps)) as client:
        assert client.delete("/api/storage/cache/resolver").status_code == 200
        _one(client, "storage_deleted", "-")


def test_logo_is_cached_privately(tmp_path, monkeypatch):
    cache = LogoCache(tmp_path)
    cache.put("openrouter", b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml")
    monkeypatch.setattr(internal_api, "_LOGOS", cache)
    with TC(create_app()) as client:
        reply = client.get("/api/providers/openrouter/logo")
    assert reply.status_code == 200
    assert reply.headers["cache-control"] == "private, max-age=86400"


def test_install_command_pins_the_served_bytes(tmp_path):
    api = coordinator_client(tmp_path)
    served = api.get("/install.sh").content
    assert served == install_script_bytes()
    command = api.post("/api/enroll", json={}).json()["command"]
    assert hashlib.sha256(served).hexdigest() in command
    assert "sha256sum -c -" in command


@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="needs sha256sum")
def test_install_command_fails_closed_on_a_tampered_script(tmp_path):
    script = b"#!/bin/sh\necho installed\n"
    command = build_command("http://10.0.0.1:8080", "ej_x_y", script)
    # No fixed /tmp name anywhere: the file is mktemp's.
    assert command.startswith("f=$(mktemp) && ")
    assert "/tmp/" not in command
    check = command.split(" && ")[2]  # echo "<sha>  $f" | sha256sum -c -
    good = tmp_path / "derate-install.sh"
    good.write_bytes(script)
    env = {**os.environ, "f": str(good)}
    assert subprocess.run(["sh", "-c", check], capture_output=True, env=env).returncode == 0
    good.write_bytes(script.replace(b"installed", b"pwned"))
    assert subprocess.run(["sh", "-c", check], capture_output=True, env=env).returncode != 0
    assert json.dumps(command).count("sha256sum") == 1


class _RuntimeClient:
    """The registry's agent client, answering as an Ollama on the node."""

    def __init__(self):
        self.posts = []

    async def get_json(self, url, timeout=None, headers=None, **_):
        if url.endswith("/api/tags"):
            return {"models": [{"name": "qwen3:0.6b", "size": 1}]}
        if url.endswith("/api/ps"):
            return {"models": []}
        raise ConnectionError(url)

    async def post_json(self, url, body, timeout=None, headers=None, **_):
        self.posts.append((url, body))
        return {"done_reason": "load" if body.get("keep_alive") else "unload"}


class _AdoptingProviders:
    def __init__(self):
        from tests.unit.test_gateway import FakeProviders

        self._inner = FakeProviders()

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def add_async(self, spec):
        from tests.unit.test_gateway import make_provider

        provider = make_provider(spec["provider_id"], base_url=spec["base_url"])
        self._inner.providers.append(provider)
        return provider


def _runtime_registry():
    from tests.unit.test_gateway import FakeRegistry

    registry = FakeRegistry()
    registry._client = _RuntimeClient()
    return registry


def test_adopting_a_node_runtime_is_audited_as_a_provider_created():
    """CAP-5: the one click that adds a routing target leaves its own row."""
    registry = _runtime_registry()
    node_id = registry.states[0].profile.node_id
    with TC(create_app(build_deps(registry=registry, providers=_AdoptingProviders()))) as client:
        r = client.post(f"/api/nodes/{node_id}/runtime")
        assert r.status_code == 201, r.text
        row = _one(client, "provider_created", f"{node_id}-ollama")
        _no_generic_row(client, row["request_id"])
    assert row["after"]["provider_id"] == f"{node_id}-ollama"


def test_changing_a_node_runtime_models_residency_is_audited():
    """CAP-5: loading or evicting a model on a node names the model."""
    registry = _runtime_registry()
    node_id = registry.states[0].profile.node_id
    with TC(create_app(build_deps(registry=registry))) as client:
        r = client.post(
            f"/api/nodes/{node_id}/runtime/model",
            json={"model": "qwen3:0.6b", "resident": True},
        )
        assert r.status_code == 200, r.text
        row = _one(client, "node_runtime_model_changed", node_id)
        _no_generic_row(client, row["request_id"])
    assert row["after"] == {"model": "qwen3:0.6b", "resident": True}
