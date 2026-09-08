"""Disk capacity and the estate, end to end.

The gap this covers: nothing in the product knew what a filesystem held, so a
launch could fail on a full disk with no warning anywhere. Three layers are
exercised -- the probe that reads the filesystem, the agent route that serves
it, and the coordinator fan-out that has to survive a node it cannot reach.

The regression that matters most is the first one. Our paths usually share a
device, and adding their usage up reports the same bytes several times.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from control_plane.contracts import DeviceClass, NodeProfile
from control_plane.paths import default_data_dir
from control_plane.registry import storage
from control_plane.registry.agent import NodeAgent, create_agent_app
from control_plane.registry.serde import storage_to_dict

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


# ---------------------------------------------------------------------------
# the probe
# ---------------------------------------------------------------------------


def test_paths_on_one_device_are_counted_once(tmp_path):
    """Several of our paths on one filesystem must report one filesystem.

    The data root, the resolver cache and the sparkrun cache normally share a
    disk. Summing them would report a 3.7 TB device as 11 TB used, and the
    number an operator reads to decide whether a model fits would be nonsense.
    """
    a = tmp_path / "data"
    b = tmp_path / "data" / "cache"
    b.mkdir(parents=True)

    filesystems, unreadable = storage.read_filesystems([a, b, tmp_path])

    assert unreadable == []
    assert len(filesystems) == 1
    entry = filesystems[0]
    # Every path that landed here is named, so a reader can see they share.
    assert set(entry["mount_paths"]) == {str(a), str(b), str(tmp_path)}


def test_a_missing_path_reports_a_reason_and_no_numbers(tmp_path):
    """A path we cannot read must not contribute a zero.

    0 free reads as an emergency and 0 used reads as an empty disk; both are
    worse than admitting the probe did not look.
    """
    filesystems, unreadable = storage.read_filesystems([tmp_path / "nope"])

    assert filesystems == []
    assert len(unreadable) == 1
    assert "does not exist" in unreadable[0]["reason"]


def test_used_pct_is_measured_against_what_we_can_allocate(tmp_path):
    """df's denominator, not the raw device size.

    A filesystem holds blocks back for root. Counting them as capacity puts
    this screen several points below every other tool on the machine.
    """
    filesystems, _ = storage.read_filesystems([tmp_path])
    fs = filesystems[0]

    capacity = fs["used"] + fs["free"]
    assert fs["used_pct"] == pytest.approx(100.0 * fs["used"] / capacity, abs=0.05)
    assert fs["reserved"] == fs["total"] - capacity
    assert fs["reserved"] >= 0


@pytest.mark.parametrize(
    "used_pct,expected",
    [(0.0, "ok"), (84.9, "ok"), (85.0, "warn"), (94.9, "warn"), (95.0, "critical")],
)
def test_severity_crosses_at_the_documented_thresholds(used_pct, expected):
    assert storage.severity(used_pct) == expected


def test_the_payload_carries_the_thresholds_it_was_judged_against(tmp_path):
    """Shipped from the server, so a client cannot hold a second opinion."""
    filesystems, _ = storage.read_filesystems([tmp_path])
    fs = filesystems[0]
    assert fs["warn_pct"] == storage.DISK_WARN_PCT
    assert fs["critical_pct"] == storage.DISK_CRITICAL_PCT


# ---------------------------------------------------------------------------
# the estate
# ---------------------------------------------------------------------------


def test_the_estate_measures_what_exists_and_nulls_what_does_not(tmp_path):
    (tmp_path / "telemetry").mkdir()
    (tmp_path / "telemetry" / "archive.db").write_bytes(b"x" * 4096)
    (tmp_path / "deployments").mkdir()
    (tmp_path / "deployments" / "d-1.json").write_text("{}")

    rows = {e["key"]: e for e in storage.read_estate(tmp_path)}

    assert rows["archive"]["exists"] is True
    assert rows["archive"]["bytes"] == 4096
    assert rows["deployments"]["bytes"] == 2
    # Absent keeps its row -- "nothing written yet" and "this build does not
    # write it" are different answers -- and reports null, never 0.
    assert rows["journal"]["exists"] is False
    assert rows["journal"]["bytes"] is None


def test_a_symlinked_component_is_not_counted_as_ours(tmp_path):
    """/data/sparkrun-cache is a symlink to another tool's cache.

    Following it would attribute sparkrun's bytes to this product.
    """
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "big").write_bytes(b"x" * 8192)
    link = tmp_path / "linked"
    link.symlink_to(real, target_is_directory=True)

    assert storage.path_bytes(link) is None
    assert storage.path_bytes(real) == 8192


def test_the_walk_does_not_follow_symlinks_out_of_the_tree(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "huge").write_bytes(b"x" * 65536)

    root = tmp_path / "root"
    root.mkdir()
    (root / "own").write_bytes(b"x" * 16)
    (root / "escape").symlink_to(outside, target_is_directory=True)

    # The link itself is counted as a link, and what it points at is not.
    assert storage.path_bytes(root) < 1024


def test_the_walk_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "_WALK_MAX_ENTRIES", 5)
    for i in range(50):
        (tmp_path / f"f{i}").write_bytes(b"x" * 10)
    # Returns a floor rather than refusing to answer: a partial size is useful
    # and "the tree was large" tells an operator nothing.
    assert 0 < storage.path_bytes(tmp_path) <= 60


def test_probe_paths_resolves_the_sparkrun_symlink(tmp_path):
    target = tmp_path / "real-cache"
    target.mkdir()
    (tmp_path / "sparkrun-cache").symlink_to(target, target_is_directory=True)

    paths = storage.probe_paths(tmp_path)

    # Resolved, so we measure the filesystem the bytes land on rather than the
    # one holding the link.
    assert target.resolve() in [Path(p) for p in paths]


# ---------------------------------------------------------------------------
# serde
# ---------------------------------------------------------------------------


def test_unavailable_is_not_an_empty_disk():
    payload = storage_to_dict(None, "spark-02")
    assert payload["available"] is False
    assert payload["filesystems"] == []
    assert payload["reason"]


# ---------------------------------------------------------------------------
# the agent route
# ---------------------------------------------------------------------------


def test_the_agent_serves_its_own_data_root(tmp_path):
    (tmp_path / "telemetry").mkdir()
    (tmp_path / "telemetry" / "journal.db").write_bytes(b"x" * 2048)

    client = TestClient(create_agent_app(NodeAgent(profile(), data_root=tmp_path)))
    res = client.get("/agent/storage")

    assert res.status_code == 200
    body = res.json()
    assert body["node_id"] == "spark-01"
    assert body["available"] is True
    assert len(body["filesystems"]) == 1
    journal = next(e for e in body["estate"] if e["key"] == "journal")
    assert journal["bytes"] == 2048


def test_the_storage_route_needs_no_token(tmp_path):
    """A read, like /agent/profile. Only the kill route is credentialed."""
    client = TestClient(
        create_agent_app(NodeAgent(profile(), token="secret", data_root=tmp_path))
    )
    assert client.get("/agent/storage").status_code == 200


def test_a_probe_that_raises_degrades_rather_than_500ing(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise OSError("disk went away")

    monkeypatch.setattr(storage, "storage_payload", boom)
    client = TestClient(create_agent_app(NodeAgent(profile(), data_root=tmp_path)))
    res = client.get("/agent/storage")

    assert res.status_code == 200
    assert res.json()["available"] is False


def test_the_agent_defaults_its_root_from_the_environment(monkeypatch, tmp_path):
    """The same env var every other component reads, with the same fallback.

    The fallback is now shared rather than re-typed: control_plane.paths picks
    /data when it is real and writable -- the container -- and this platform's
    application-state directory otherwise. Asserting the literal "/data" here
    would pass only on a machine that has one, which is the assumption the
    resolver exists to remove.
    """
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    assert NodeAgent(profile())._data_root == tmp_path
    monkeypatch.delenv("DERATE_DATA_DIR")
    assert NodeAgent(profile())._data_root == default_data_dir()


# ---------------------------------------------------------------------------
# the coordinator fan-out
# ---------------------------------------------------------------------------


def _gateway_client():
    from control_plane.gateway.app import create_app
    from control_plane.gateway.deps import GatewayDeps

    return TestClient(create_app(GatewayDeps()))


def test_storage_answers_with_retention_from_the_module_that_enforces_it():
    from control_plane.telemetry import config as tconfig

    res = _gateway_client().get("/api/storage")

    assert res.status_code == 200
    body = res.json()
    assert body["retention"]["archive_max_bytes"] == tconfig.ARCHIVE_MAX_BYTES
    assert body["retention"]["journal_max_bytes"] == tconfig.JOURNAL_MAX_BYTES
    assert body["retention"]["samples_raw_s"] == tconfig.SAMPLES_RAW_RETENTION_S


def test_a_node_with_no_agent_url_degrades_and_says_so():
    """One unreachable worker must not blank the whole screen.

    The node that cannot be read is exactly the node an operator opened this
    tab to look at.
    """
    body = _gateway_client().get("/api/storage").json()

    assert body["nodes"], "the stub registry should still list its nodes"
    for row in body["nodes"]:
        assert row["available"] is False
        assert row["reason"]
        assert row["filesystems"] == []


def test_clearing_a_cache_that_does_not_exist_is_a_sentence_not_a_traceback():
    res = _gateway_client().delete("/api/storage/cache/resolver")

    assert res.status_code == 503
    body = res.json()
    assert body["error"]["code"] == "resolver_cache_unavailable"
    assert "nothing to clear" in body["error"]["message"]


def test_clearing_a_real_cache_reports_what_it_freed(tmp_path):
    from control_plane.gateway.app import create_app
    from control_plane.gateway.deps import GatewayDeps
    from control_plane.resolver.cache import ShapeCache

    cache = ShapeCache(directory=tmp_path)
    (tmp_path / "a.json").write_bytes(b"x" * 4096)

    class _Resolver:
        def __init__(self, cache):
            self.cache = cache

    deps = GatewayDeps()
    deps.resolver = _Resolver(cache)
    client = TestClient(create_app(deps))

    res = client.delete("/api/storage/cache/resolver")

    assert res.status_code == 200
    assert res.json()["cleared"] is True
    assert res.json()["bytes_freed"] == 4096
    assert not list(tmp_path.glob("*.json"))


def test_a_node_on_an_older_build_says_so_rather_than_echoing_a_404():
    """404 here means one thing: that agent predates the storage probe.

    Every other /agent route on it works, so this is a rolling upgrade rather
    than a fault, and an operator needs the sentence that says which.
    """
    import httpx

    from control_plane.gateway.app import create_app
    from control_plane.gateway.deps import GatewayDeps

    class _Registry:
        def list_nodes(self):
            return [type("N", (), {"profile": type("P", (), {"node_id": "old-node"})()})()]

        def agent_url(self, node_id):
            return "http://198.51.100.9:8081"

    deps = GatewayDeps()
    deps.registry = _Registry()
    client = TestClient(create_app(deps))

    class _Response:
        status_code = 404

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Response()

    original = httpx.AsyncClient
    httpx.AsyncClient = _Client
    try:
        body = client.get("/api/storage").json()
    finally:
        httpx.AsyncClient = original

    row = body["nodes"][0]
    assert row["available"] is False
    assert "running a build from before disk was measured" in row["reason"]
    # The HTTP client's own 404 string, and the MDN link it carries, must not
    # reach an operator.
    assert "404" not in row["reason"]
