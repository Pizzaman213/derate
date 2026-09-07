"""Downloaded weights: seeing them, and getting rid of them safely.

The gap this covers: model weights are the largest thing on a serving box by
orders of magnitude -- 894 GiB across 51 repositories on the machine this was
written against, one of them 182 GiB -- and nothing in the product could see
them, let alone reclaim them.

The delete is the dangerous half, so most of this file is about what must not
happen: deleting outside the cache, deleting a model something is serving, or
deleting on an uncredentialled request.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from control_plane.contracts import DeviceClass, NodeProfile
from control_plane.registry import modelcache
from control_plane.registry.agent import NodeAgent, create_agent_app


def profile(node_id="spark-01"):
    return NodeProfile(
        node_id=node_id,
        hostname=node_id,
        address="10.0.0.1",
        device_class=DeviceClass.GB10,
        gpu_name="GB10",
        gpu_count=1,
        total_memory=128 * 1024**3,
        addressable_memory=119 * 1024**3,
        memory_bandwidth_gbps=273.0,
        compute_capability="12.1",
        driver_version="580.00",
    )


def make_repo(cache: Path, folder: str, blob_sizes=(1024,), revision="abc123") -> Path:
    """A repository in the cache's real layout: blobs hold the bytes, and
    snapshots are symlinks into them."""
    repo = cache / folder
    (repo / "blobs").mkdir(parents=True)
    (repo / "snapshots" / revision).mkdir(parents=True)
    (repo / "refs").mkdir(parents=True)
    (repo / "refs" / "main").write_text(revision)
    for i, size in enumerate(blob_sizes):
        blob = repo / "blobs" / f"blob{i}"
        blob.write_bytes(b"x" * size)
        (repo / "snapshots" / revision / f"file{i}.safetensors").symlink_to(
            Path("../..") / "blobs" / f"blob{i}"
        )
    return repo


# ---------------------------------------------------------------------------
# naming
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "repo_id,folder",
    [
        ("openai/gpt-oss-120b", "models--openai--gpt-oss-120b"),
        ("z-lab/gpt-oss-120b-DFlash", "models--z-lab--gpt-oss-120b-DFlash"),
        ("Qwen/Qwen2.5-0.5B-Instruct", "models--Qwen--Qwen2.5-0.5B-Instruct"),
    ],
)
def test_a_repo_id_encodes_to_the_cache_folder(repo_id, folder):
    """This direction is the one every decision uses, and it is unambiguous."""
    assert modelcache.folder_for(repo_id) == folder
    assert modelcache.repo_from_folder(folder) == repo_id


def test_decoding_is_display_only_and_may_be_ambiguous():
    """models--a--b--c could be a/b--c or a--b/c. Nothing decides on this."""
    assert modelcache.repo_from_folder("models--a--b--c") == "a/b--c"


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------


def test_only_blobs_are_counted(tmp_path):
    """Snapshots are symlink trees into blobs.

    Counting them would report the same weights once per cached revision --
    two revisions of a 180 GB model would read as 360 GB, and the number an
    operator uses to pick what to delete would be double.
    """
    make_repo(tmp_path, "models--org--big", blob_sizes=(4096, 2048), revision="r1")
    # A second revision reusing the same blobs, as the cache actually does.
    snap2 = tmp_path / "models--org--big" / "snapshots" / "r2"
    snap2.mkdir()
    (snap2 / "file0.safetensors").symlink_to(Path("../..") / "blobs" / "blob0")

    out = modelcache.scan(tmp_path)

    assert out["available"] is True
    repo = out["repos"][0]
    assert repo["bytes"] == 4096 + 2048
    assert repo["blob_count"] == 2
    assert sorted(repo["revisions"]) == ["r1", "r2"]


def test_repos_come_back_largest_first(tmp_path):
    make_repo(tmp_path, "models--a--small", blob_sizes=(10,))
    make_repo(tmp_path, "models--b--huge", blob_sizes=(9999,))
    make_repo(tmp_path, "models--c--mid", blob_sizes=(500,))

    repos = modelcache.scan(tmp_path)["repos"]

    assert [r["folder"] for r in repos] == [
        "models--b--huge",
        "models--c--mid",
        "models--a--small",
    ]


def test_non_model_directories_are_ignored(tmp_path):
    make_repo(tmp_path, "models--org--real")
    (tmp_path / "datasets--org--nope").mkdir()
    (tmp_path / "version.txt").write_text("1")

    repos = modelcache.scan(tmp_path)["repos"]

    assert [r["folder"] for r in repos] == ["models--org--real"]


def test_a_missing_cache_is_not_an_empty_one(tmp_path):
    """A container without the mount must not read as "nothing downloaded"."""
    out = modelcache.scan(tmp_path / "absent")

    assert out["available"] is False
    assert out["repos"] == []
    assert out["total_bytes"] is None
    assert out["reason"]


def test_cache_dir_prefers_the_deployment_override(monkeypatch, tmp_path):
    monkeypatch.setenv("DERATE_HF_CACHE", str(tmp_path))
    assert modelcache.cache_dir() == tmp_path
    monkeypatch.delenv("DERATE_HF_CACHE")
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    assert modelcache.cache_dir() == tmp_path / "hub"


# ---------------------------------------------------------------------------
# deleting: the guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "folder",
    [
        "../etc",
        "../../etc/passwd",
        "sub/dir",
        "",
        ".",
        "..",
        "/etc",
    ],
)
def test_a_folder_that_is_not_one_cache_name_is_refused(tmp_path, folder):
    make_repo(tmp_path, "models--org--real")
    with pytest.raises(modelcache.DeleteRefused):
        modelcache.resolve_target(folder, tmp_path)


def test_a_folder_outside_the_naming_convention_is_refused(tmp_path):
    (tmp_path / "something-else").mkdir()
    with pytest.raises(modelcache.DeleteRefused):
        modelcache.resolve_target("something-else", tmp_path)


def test_a_symlink_planted_in_the_cache_cannot_redirect_the_delete(tmp_path):
    """The resolved target must sit directly inside the resolved cache.

    Without this, a symlink named models--x--y pointing at somebody's home
    directory would be a delete of that directory.
    """
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep").write_text("do not delete")
    cache = tmp_path / "hub"
    cache.mkdir()
    (cache / "models--evil--link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(modelcache.DeleteRefused):
        modelcache.resolve_target("models--evil--link", cache)
    assert (outside / "keep").exists()


def test_deleting_reports_what_it_actually_reclaimed(tmp_path):
    make_repo(tmp_path, "models--org--gone", blob_sizes=(4096, 4096))

    out = modelcache.delete("models--org--gone", tmp_path)

    assert out["deleted"] is True
    assert out["bytes_freed"] == 8192
    assert out["repo_id"] == "org/gone"
    assert not (tmp_path / "models--org--gone").exists()


def test_deleting_one_repo_leaves_the_others(tmp_path):
    make_repo(tmp_path, "models--org--keep", blob_sizes=(100,))
    make_repo(tmp_path, "models--org--go", blob_sizes=(200,))

    modelcache.delete("models--org--go", tmp_path)

    assert (tmp_path / "models--org--keep" / "blobs" / "blob0").exists()


# ---------------------------------------------------------------------------
# the agent route
# ---------------------------------------------------------------------------


def test_the_agent_serves_the_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_HF_CACHE", str(tmp_path))
    make_repo(tmp_path, "models--org--m", blob_sizes=(2048,))

    client = TestClient(create_agent_app(NodeAgent(profile(), token="tok")))
    body = client.get("/agent/models/cache").json()

    assert body["available"] is True
    assert body["node_id"] == "spark-01"
    assert body["repos"][0]["bytes"] == 2048


def test_the_delete_is_token_gated(tmp_path, monkeypatch):
    """It removes hundreds of gigabytes; it is checked like the kill is."""
    monkeypatch.setenv("DERATE_HF_CACHE", str(tmp_path))
    make_repo(tmp_path, "models--org--m")
    client = TestClient(create_agent_app(NodeAgent(profile(), token="right")))

    assert client.delete("/agent/models/cache/models--org--m").status_code == 403
    assert (
        client.delete(
            "/agent/models/cache/models--org--m",
            headers={"X-Derate-Token": "wrong"},
        ).status_code
        == 403
    )
    # Still there.
    assert (tmp_path / "models--org--m").exists()


def test_the_token_is_checked_before_the_folder_is_looked_at(tmp_path, monkeypatch):
    """An uncredentialled caller must not learn what is cached from which
    refusal comes back."""
    monkeypatch.setenv("DERATE_HF_CACHE", str(tmp_path))
    client = TestClient(create_agent_app(NodeAgent(profile(), token="right")))

    present = client.delete("/agent/models/cache/models--org--real")
    absent = client.delete("/agent/models/cache/models--org--absent")

    assert present.status_code == absent.status_code == 403


def test_a_credentialled_delete_works_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_HF_CACHE", str(tmp_path))
    make_repo(tmp_path, "models--org--m", blob_sizes=(1024,))
    client = TestClient(create_agent_app(NodeAgent(profile(), token="tok")))

    res = client.delete(
        "/agent/models/cache/models--org--m", headers={"X-Derate-Token": "tok"}
    )

    assert res.status_code == 200
    assert res.json()["bytes_freed"] == 1024
    assert not (tmp_path / "models--org--m").exists()


# ---------------------------------------------------------------------------
# the coordinator's in-use refusal
# ---------------------------------------------------------------------------


def _gateway_with(deployments, agent_url="http://198.51.100.9:8081"):
    from control_plane.gateway.app import create_app
    from control_plane.gateway.deps import GatewayDeps

    class _Deployments:
        def list(self):
            return deployments

    class _Registry:
        def list_nodes(self):
            return []

        def agent_url(self, node_id):
            return agent_url

        def cluster_token(self):
            return "tok"

    deps = GatewayDeps()
    deps.deployments = _Deployments()
    deps.registry = _Registry()
    return TestClient(create_app(deps))


class _StubResponse:
    status_code = 200

    def json(self):
        return {"deleted": True, "bytes_freed": 195758080000}

    def raise_for_status(self):
        return None


class _StubClient:
    """Stands in for httpx.AsyncClient. Every test that reaches the agent hop
    must use this: the stub agent address is unroutable and the delete timeout
    is 120s, so a real call hangs the suite rather than failing it."""

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def delete(self, url, headers=None):
        return _StubResponse()


def _deployment(model_id, state, deployment_id="d-1"):
    from control_plane.contracts import DeploymentState

    class _Shape:
        pass

    shape = _Shape()
    shape.model_id = model_id

    class _Dep:
        pass

    dep = _Dep()
    dep.deployment_id = deployment_id
    dep.shape = shape
    dep.state = DeploymentState(state)
    return dep


@pytest.mark.parametrize("state", ["ready", "degraded", "launching", "planned", "stopping"])
def test_weights_a_live_deployment_is_serving_are_refused(state):
    """409, naming the deployment.

    Deleting these would leave the record claiming READY while the files are
    gone, and the model would keep serving until it next needed a shard --
    failing later, somewhere unrelated to the click that caused it.
    """
    client = _gateway_with([_deployment("openai/gpt-oss-120b", state)])

    res = client.delete(
        "/api/storage/nodes/spark-01/models/models--openai--gpt-oss-120b"
    )

    assert res.status_code == 409
    body = res.json()
    assert body["error"]["code"] == "model_in_use"
    assert "openai/gpt-oss-120b" in body["error"]["message"]
    assert "d-1" in body["error"]["message"]


@pytest.mark.parametrize("state", ["stopped", "failed"])
def test_weights_of_a_terminal_deployment_are_deletable(state, monkeypatch):
    """The orphan this exists to clear. It must not be protected forever."""
    import httpx

    client = _gateway_with([_deployment("openai/gpt-oss-120b", state)])

    monkeypatch.setattr(httpx, "AsyncClient", _StubClient)
    res = client.delete(
        "/api/storage/nodes/spark-01/models/models--openai--gpt-oss-120b"
    )

    assert res.status_code == 200
    assert res.json()["bytes_freed"] == 195758080000


def test_a_different_model_is_not_protected_by_an_unrelated_deployment(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _StubClient)
    client = _gateway_with([_deployment("openai/gpt-oss-120b", "ready")])
    # Not the served one, so it must not 409.
    res = client.delete("/api/storage/nodes/spark-01/models/models--meta--Llama-3-8B")

    assert res.status_code != 409


def test_the_match_is_on_the_encoded_folder_not_a_decoded_id():
    """A name containing a double hyphen decodes ambiguously, so the check
    compares the encoded form -- which is what the cache is keyed by."""
    client = _gateway_with([_deployment("org/weird--name", "ready")])

    res = client.delete("/api/storage/nodes/spark-01/models/models--org--weird--name")

    assert res.status_code == 409


def test_the_gateways_terminal_set_matches_the_fsm():
    """internal_api spells the terminal states rather than importing
    deploy.fsm, to keep the deployment manager out of the gateway's request
    module. This is what stops the two copies drifting."""
    from control_plane.deploy import fsm
    from control_plane.gateway.internal_api import _TERMINAL_STATES

    assert _TERMINAL_STATES == fsm.TERMINAL
