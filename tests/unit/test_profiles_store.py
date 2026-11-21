"""Plan step 5: saved profiles and pins, on the server, shared by every admin."""

from __future__ import annotations

import json
import stat

import pytest
from fastapi.testclient import TestClient

from control_plane.gateway import GatewayDeps, GatewaySettings, create_app
from control_plane.gateway.profiles_store import (
    MAX_PINS,
    MAX_RUNS_PER_MODEL,
    ProfileError,
    ProfileStore,
)

REQ = {"model_id": "Qwen/Qwen3-8B", "context": 32768, "concurrency": 4,
       "engine_options": {"async_scheduling": True}, "target": "local"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html>")
    app = create_app(GatewayDeps(), settings=GatewaySettings(cluster_id="c-test", ui_dir=str(ui)))
    with TestClient(app) as c:
        yield c


def _create(c, name="A", is_default=False, model_id="Qwen/Qwen3-8B"):
    r = c.post("/api/profiles", json={"model_id": model_id, "name": name,
                                       "request": {**REQ, "model_id": model_id},
                                       "is_default": is_default})
    assert r.status_code == 201, r.text
    assert r.headers["content-type"].startswith("application/json")
    return r.json()


def test_crud_round_trip(client, tmp_path):
    p = _create(client)
    assert p["profile_id"].startswith("p-") and p["request"] == REQ
    assert client.get("/api/profiles", params={"model_id": "Qwen/Qwen3-8B"}).json()["profiles"] == [p]
    assert client.get("/api/profiles", params={"model_id": "other"}).json()["profiles"] == []

    r = client.patch(f"/api/profiles/{p['profile_id']}", json={"name": "Renamed"})
    assert r.status_code == 200 and r.json()["name"] == "Renamed"

    path = tmp_path / "profiles.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["version"] == 1

    assert client.delete(f"/api/profiles/{p['profile_id']}").status_code == 204
    assert client.get("/api/profiles").json()["profiles"] == []
    r = client.patch(f"/api/profiles/{p['profile_id']}", json={"name": "x"})
    assert r.status_code == 404
    assert client.delete(f"/api/profiles/{p['profile_id']}").status_code == 404


def test_newest_first_across_models(client):
    a = _create(client, "A")
    b = _create(client, "B", model_id="Qwen/Qwen3-4B")
    ids = [p["profile_id"] for p in client.get("/api/profiles").json()["profiles"]]
    assert ids == [b["profile_id"], a["profile_id"]]


def test_default_is_exclusive_per_model(client):
    a = _create(client, "A", is_default=True)
    other = _create(client, "O", is_default=True, model_id="Qwen/Qwen3-4B")
    b = _create(client, "B", is_default=True)
    rows = {p["profile_id"]: p for p in client.get("/api/profiles").json()["profiles"]}
    assert rows[b["profile_id"]]["is_default"] is True
    assert rows[a["profile_id"]]["is_default"] is False
    assert rows[other["profile_id"]]["is_default"] is True

    client.patch(f"/api/profiles/{a['profile_id']}", json={"is_default": True})
    rows = {p["profile_id"]: p for p in client.get("/api/profiles").json()["profiles"]}
    assert rows[a["profile_id"]]["is_default"] and not rows[b["profile_id"]]["is_default"]


@pytest.mark.parametrize("body", [
    {"model_id": "m", "name": "n", "request": {"model_id": "m", "bogus": 1}},
    {"model_id": "m", "name": "", "request": {"model_id": "m"}},
    {"model_id": "m", "name": "x" * 81, "request": {"model_id": "m"}},
    {"model_id": "m", "name": "n", "request": {"model_id": "m", "extra_args": "x" * 17000}},
    {"model_id": "m", "name": "n", "request": {"model_id": "m"}, "extra": 1},
])
def test_bad_profile_is_400(client, body):
    r = client.post("/api/profiles", json=body)
    assert r.status_code == 400, r.text


def test_unknown_request_key_names_it(client):
    r = client.post("/api/profiles", json={"model_id": "m", "name": "n",
                                            "request": {"model_id": "m", "lora": "x"}})
    assert r.status_code == 400 and "lora" in r.text


def test_corrupt_file_loads_empty(tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text("{not json")
    store = ProfileStore(path)
    assert store.list() == [] and store.pins() == []
    path.write_text(json.dumps({"version": 99, "profiles": [], "pins": ["x"]}))
    assert store.pins() == []


def test_pins(client, tmp_path):
    assert client.get("/api/pins").json() == {"pins": []}
    r = client.put("/api/pins", json={"pins": ["a/b", "c/d"]})
    assert r.status_code == 200 and r.json() == {"pins": ["a/b", "c/d"]}
    # Survives a fresh store on the same file, i.e. a restart.
    assert ProfileStore(tmp_path / "profiles.json").pins() == ["a/b", "c/d"]
    assert client.put("/api/pins", json={"pins": ["a", "a"]}).status_code == 400
    assert client.put("/api/pins", json={"pins": [f"m{i}" for i in range(MAX_PINS + 1)]}).status_code == 400
    assert client.put("/api/pins", json={"pins": "a"}).status_code == 400
    assert client.get("/api/pins").json() == {"pins": ["a/b", "c/d"]}


def test_pins_limit_in_store(tmp_path):
    store = ProfileStore(tmp_path / "p.json")
    store.set_pins([f"m{i}" for i in range(MAX_PINS)])
    with pytest.raises(ProfileError):
        store.set_pins([f"m{i}" for i in range(MAX_PINS + 1)])


def _run(store, i, model_id="Qwen/Qwen3-8B"):
    return store.record_run(model_id=model_id, request={**REQ, "served_name": f"s{i}"},
                            deployment_id=f"d{i}", served_name=f"s{i}", actor="t")


def test_runs_are_pruned_per_model_and_a_starred_run_survives(tmp_path):
    store = ProfileStore(tmp_path / "p.json")
    first = _run(store, 0)
    store.patch(first["profile_id"], {"is_default": True})
    oldest = _run(store, 1)
    for i in range(2, 2 + MAX_RUNS_PER_MODEL):
        _run(store, i)
    other = _run(store, 99, model_id="Qwen/Qwen3-4B")
    ids = {p["profile_id"] for p in store.list()}
    assert first["profile_id"] in ids and other["profile_id"] in ids
    assert oldest["profile_id"] not in ids
    runs = [p for p in store.list("Qwen/Qwen3-8B") if not p["is_default"]]
    assert len(runs) == MAX_RUNS_PER_MODEL
    assert all(p["kind"] == "run" and p["name"].startswith("Run s") for p in runs)


def test_a_runs_request_is_not_patchable(tmp_path):
    store = ProfileStore(tmp_path / "p.json")
    run = _run(store, 0)
    with pytest.raises(ProfileError):
        store.patch(run["profile_id"], {"request": REQ})
    assert store.patch(run["profile_id"], {"name": "kept"})[1]["name"] == "kept"
    store.delete(run["profile_id"])
    assert store.list() == []


def test_an_old_file_without_kind_reads_as_saved(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"version": 1, "pins": [], "profiles": [
        {"profile_id": "p-1", "model_id": "m", "name": "n", "request": {}}]}))
    (p,) = ProfileStore(path).list()
    assert p["kind"] == "saved" and p["deployment_id"] is None and p["last_result"] is None


def test_post_cannot_create_a_run(client):
    r = client.post("/api/profiles", json={"model_id": "m", "name": "n", "kind": "run",
                                            "request": {"model_id": "m"}})
    assert r.status_code == 400


def test_attach_result_matches_across_served_names(tmp_path):
    store = ProfileStore(tmp_path / "p.json")
    run = _run(store, 0)
    saved = store.create({"model_id": "Qwen/Qwen3-8B", "name": "n", "request": REQ}, actor="t")
    differs = store.create({"model_id": "Qwen/Qwen3-8B", "name": "x",
                            "request": {**REQ, "context": 8192}}, actor="t")
    assert store.attach_result({**REQ, "served_name": "bench-1"}, {"tps_each_1": 9.0}) == 2
    got = {p["profile_id"]: p["last_result"] for p in store.list()}
    assert got[run["profile_id"]] == got[saved["profile_id"]] == {"tps_each_1": 9.0}
    assert got[differs["profile_id"]] is None
