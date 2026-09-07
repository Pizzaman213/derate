"""First-run setup: the flag, what outranks it, and the endpoint over both.

The environment is pinned rather than inherited. `DERATE_DATA_DIR` decides
where the flag file lands, and a test that let it default would write into
whatever `control_plane/paths.py::data_dir()` resolves on the developer's box
-- passing or failing according to which machine ran it. Same spirit as
`test_links.py`'s `_clean_env`.
"""

from __future__ import annotations

import json
import os
import stat

import pytest
from fastapi.testclient import TestClient

from control_plane.gateway import GatewayDeps, GatewaySettings, create_app
from control_plane.gateway import setup_state
from control_plane.gateway.setup_state import Completion, SetupStore, is_complete


# ---------------------------------------------------------------------------
# Fakes. Deliberately minimal: this module tests one question, and a registry
# rich enough to answer others would only make its failures harder to read.
# ---------------------------------------------------------------------------


class _Node:
    def __init__(self, node_id: str, healthy: bool = True) -> None:
        from control_plane.contracts import DeviceClass, NodeProfile, NodeState

        self.profile = NodeProfile(
            node_id=node_id,
            hostname=node_id,
            address="10.0.0.1",
            device_class=DeviceClass.GB10,
            gpu_name="NVIDIA GB10",
            gpu_count=1,
            total_memory=137_438_953_472,
            addressable_memory=128_526_896_332,
            memory_bandwidth_gbps=273.0,
            compute_capability="12.1",
            driver_version="580.95.05",
        )
        self.state = NodeState(
            profile=self.profile,
            healthy=healthy,
            last_seen=1757193600.0,
            memory_used=0,
            power_watts=0.0,
            temperature_c=0.0,
            utilization_pct=0.0,
        )


class FakeRegistry:
    def __init__(self, nodes=(), local: str | None = None) -> None:
        self._nodes = [n.state for n in nodes]
        if local is not None:
            self.local_node_id = local

    def list_nodes(self):
        return list(self._nodes)

    def get_node(self, node_id):
        return next((n for n in self._nodes if n.profile.node_id == node_id), None)

    def healthy_nodes(self):
        return [n for n in self._nodes if n.healthy]


class FakeDeployments:
    """`count` deployments, built from the gateway's own stub.

    Not hand-rolled namespaces. A `Deployment` carries a ModelShape, a
    ParallelismPlan and a FitResult, and several background tasks walk the list
    on a timer reading fields this module never mentions -- `backend_url` in
    `targets.py`, `.plan.node_ids` and `.state` in `admission.py`. A placeholder
    fails in one of those tasks instead of in the assertion under test, which is
    a confusing way to learn that a fake was too thin.
    """

    def __init__(self, count: int = 0) -> None:
        import dataclasses

        from control_plane.gateway.stubs import StubDeployments

        base = StubDeployments().list()
        if not base:  # pragma: no cover - the stub ships fixtures
            self._deployments = []
            return
        out = list(base[:count])
        while len(out) < count:
            out.append(
                dataclasses.replace(base[0], deployment_id="dep-extra-%d" % len(out))
            )
        self._deployments = out

    def list(self):
        return list(self._deployments)

    def get(self, deployment_id):
        return next(
            (d for d in self._deployments if d.deployment_id == deployment_id), None
        )


class FakeProviders:
    def __init__(self, count: int = 0) -> None:
        self._count = count

    def list(self):
        return [object()] * self._count

    def models(self):
        return []


class BrokenRegistry:
    def list_nodes(self):
        raise RuntimeError("registry is down")

    def healthy_nodes(self):
        raise RuntimeError("registry is down")

    def get_node(self, node_id):
        raise RuntimeError("registry is down")


class BrokenDeployments:
    def list(self):
        raise RuntimeError("deployments are down")

    def get(self, deployment_id):
        raise RuntimeError("deployments are down")


SETUP_FUTURE = setup_state.SCHEMA_VERSION + 1


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    return tmp_path


def build(*, nodes=(), local=None, deployments=0, providers=0, registry=None):
    from control_plane.gateway.stubs import (
        StubFit,
        StubLinks,
        StubPlanner,
        StubResolver,
    )

    return GatewayDeps(
        registry=registry if registry is not None else FakeRegistry(nodes, local),
        links=StubLinks(),
        resolver=StubResolver(),
        fit=StubFit(),
        planner=StubPlanner(),
        deployments=FakeDeployments(deployments),
        providers=FakeProviders(providers),
        settings=GatewaySettings(),
    )


# ---------------------------------------------------------------------------
# is_complete: what outranks what, and why the reason travels with the verdict
# ---------------------------------------------------------------------------


def test_a_cluster_with_nothing_on_it_has_not_been_set_up():
    assert is_complete(flag=False, deployments=0, providers=0) == Completion(
        False, "nothing is configured on this coordinator yet"
    )


def test_a_cluster_serving_a_model_is_set_up_even_with_no_flag():
    """The derived signal is the point. A data directory that was wiped, or a
    coordinator that never wrote the flag, must not drop a working cluster back
    into onboarding."""
    verdict = is_complete(flag=False, deployments=1, providers=0)
    assert verdict.completed
    assert verdict.reason == "this cluster is serving 1 model"


def test_a_cluster_with_a_provider_is_set_up_even_with_no_flag():
    verdict = is_complete(flag=False, deployments=0, providers=2)
    assert verdict.completed
    assert verdict.reason == "this cluster has 2 providers configured"


def test_the_flag_alone_completes_an_otherwise_empty_cluster():
    """Someone who clicked through and chose to add nothing has answered the
    question. Asking again on the next load would be the product ignoring
    them."""
    verdict = is_complete(flag=True, deployments=0, providers=0)
    assert verdict.completed
    assert verdict.reason == "setup was completed on this coordinator"


def test_the_reason_names_the_strongest_evidence_not_the_flag():
    """Both are true here. "serving a model" is the more useful sentence to put
    in front of somebody asking why they are not being offered setup."""
    verdict = is_complete(flag=True, deployments=3, providers=1)
    assert verdict.reason == "this cluster is serving 3 models"


def test_the_reason_is_singular_or_plural_as_the_count_requires():
    assert is_complete(flag=False, deployments=1, providers=0).reason.endswith("1 model")
    assert is_complete(flag=False, deployments=2, providers=0).reason.endswith("2 models")
    assert is_complete(flag=False, deployments=0, providers=1).reason.endswith(
        "1 provider configured"
    )


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


def test_a_missing_file_reads_as_not_completed(data_dir):
    assert SetupStore().load() is False


def test_completion_survives_a_new_store(data_dir):
    SetupStore().mark_complete()
    assert SetupStore().load() is True


def test_the_file_is_written_0600(data_dir):
    store = SetupStore()
    store.mark_complete()
    mode = stat.S_IMODE(store.path.stat().st_mode)
    if hasattr(os, "fchmod"):
        assert mode == 0o600
    else:  # pragma: no cover - platforms fsutil documents it cannot enforce
        pytest.skip("this platform cannot enforce a file mode")


def test_a_corrupt_file_reads_as_not_completed_rather_than_raising(data_dir):
    """An operator locked out by a bad config file is worse off than one whose
    wizard is offered a second time."""
    store = SetupStore()
    store.path.write_text("{not json", encoding="utf-8")
    assert store.load() is False


def test_a_file_from_a_future_schema_is_ignored(data_dir):
    store = SetupStore()
    store.path.write_text(
        json.dumps({"version": SETUP_FUTURE, "completed": True}), encoding="utf-8"
    )
    assert store.load() is False


def test_a_non_object_file_is_ignored(data_dir):
    store = SetupStore()
    store.path.write_text("[]", encoding="utf-8")
    assert store.load() is False


def test_reset_offers_the_wizard_again(data_dir):
    store = SetupStore()
    store.mark_complete()
    store.reset()
    assert store.load() is False


def test_reset_on_a_missing_file_is_not_an_error(data_dir):
    SetupStore().reset()


def test_a_half_written_file_never_replaces_a_good_one(data_dir, monkeypatch):
    """The temp file is unlinked and the original stands. Mirrors the guarantee
    settings_store.py documents for the same write."""
    store = SetupStore()
    store.mark_complete()
    before = store.path.read_text(encoding="utf-8")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(setup_state.os, "replace", boom)
    with pytest.raises(OSError):
        store.mark_complete()

    assert store.path.read_text(encoding="utf-8") == before
    strays = [p for p in data_dir.iterdir() if p.name.startswith(".setup-")]
    assert strays == []


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


def test_a_fresh_coordinator_reports_incomplete(data_dir):
    with TestClient(create_app(build())) as client:
        body = client.get("/api/setup").json()
    assert body["completed"] is False
    assert body["reason"] == "nothing is configured on this coordinator yet"
    assert body["deployments"] == 0
    assert body["providers"] == 0


def test_the_machine_is_the_coordinator_not_merely_the_first_node(data_dir):
    """`list_nodes()` order guarantees nothing, so a setup screen that took
    nodes[0] would introduce the coordinator's own hardware as somebody
    else's."""
    nodes = [_Node("worker-01"), _Node("spark-coordinator")]
    with TestClient(create_app(build(nodes=nodes, local="spark-coordinator"))) as client:
        body = client.get("/api/setup").json()
    assert body["machine"]["node_id"] == "spark-coordinator"
    assert body["cluster"] == {"nodes": 2, "healthy": 2}


def test_the_machine_row_is_the_same_shape_the_cluster_serves(data_dir):
    """Reused from serialize.node_payload rather than assembled here, so the
    setup screen and the cluster graph cannot disagree about whether this
    machine can be used."""
    nodes = [_Node("spark-01")]
    with TestClient(create_app(build(nodes=nodes, local="spark-01"))) as client:
        machine = client.get("/api/setup").json()["machine"]
    for key in ("node_id", "gpu_name", "eligible", "ineligible_reason", "build_skew"):
        assert key in machine, key
    assert machine["memory_bandwidth_gbps"] == 273.0


def test_a_lone_node_is_this_machine_without_needing_the_field(data_dir):
    """The common first-run case: one machine that has just installed itself.
    A roster of one is unambiguous, so no authoritative id is required."""
    with TestClient(create_app(build(nodes=[_Node("spark-01")]))) as client:
        body = client.get("/api/setup").json()
    assert body["machine"]["node_id"] == "spark-01"


def test_an_ambiguous_roster_reports_no_machine_rather_than_guessing(data_dir):
    """Without `local_node_id`, `settings.coordinator_node_id` is derived from
    `list_nodes()[0]` -- an order nothing guarantees. For a capacity report a
    wrong pick costs a slightly-off number; here it would introduce somebody
    else's GPU as the box under the reader's desk, on the one screen they have
    no way to check. An honest absence instead."""
    nodes = [_Node("somebody-else"), _Node("another-one")]
    with TestClient(create_app(build(nodes=nodes))) as client:
        body = client.get("/api/setup").json()
    assert body["machine"] is None
    assert body["cluster"]["nodes"] == 2


def test_an_unhealthy_coordinator_is_still_the_machine_in_front_of_you(data_dir):
    """`list_nodes`, not `healthy_nodes`: on a fresh install the coordinator has
    often only just probed itself, and "no machine found" to somebody looking
    straight at it is the worst answer available."""
    nodes = [_Node("spark-01", healthy=False)]
    with TestClient(create_app(build(nodes=nodes, local="spark-01"))) as client:
        body = client.get("/api/setup").json()
    assert body["machine"]["node_id"] == "spark-01"
    assert body["cluster"] == {"nodes": 1, "healthy": 0}


def test_a_serving_cluster_reports_complete_without_the_flag(data_dir):
    with TestClient(create_app(build(deployments=2))) as client:
        body = client.get("/api/setup").json()
    assert body["completed"] is True
    assert body["reason"] == "this cluster is serving 2 models"


def test_completing_setup_persists_across_a_restart(data_dir):
    with TestClient(create_app(build())) as client:
        assert client.post("/api/setup/complete").json() == {"completed": True}
    # A second app, as a restart would be.
    with TestClient(create_app(build())) as client:
        body = client.get("/api/setup").json()
    assert body["completed"] is True
    assert body["reason"] == "setup was completed on this coordinator"


def test_a_broken_registry_still_answers(data_dir):
    """The one screen where a 500 is unaffordable: nobody has any context yet
    for what a fresh install is supposed to look like."""
    with TestClient(create_app(build(registry=BrokenRegistry()))) as client:
        response = client.get("/api/setup")
    assert response.status_code == 200
    body = response.json()
    assert body["machine"] is None
    assert body["cluster"] == {"nodes": 0, "healthy": 0}


def test_a_broken_port_counts_as_zero_not_as_configured(data_dir):
    """Reading a failure as "configured" would suppress the wizard on exactly
    the boot where something is already wrong."""
    deps = build()
    deps.deployments = BrokenDeployments()
    with TestClient(create_app(deps)) as client:
        body = client.get("/api/setup").json()
    assert body["deployments"] == 0
    assert body["completed"] is False


def test_provider_routing_is_reported_from_the_kinds_table(data_dir):
    """The setup screen renders no cloud step at all when this is False, so it
    has to mean "there is something to add", not "providers exist as a
    concept"."""
    with TestClient(create_app(build())) as client:
        assert client.get("/api/setup").json()["provider_routing"] is True


def test_setup_routes_are_registered_above_the_static_mount(tmp_path, data_dir):
    """The trap every router in this package documents. With a UI directory
    mounted at "/", a router registered below it answers index.html -- and
    /api/setup is the FIRST call a fresh install makes, so below the mount the
    product's opening screen parses markup as JSON and shows nothing."""
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / "index.html").write_text("<!doctype html>ui", encoding="utf-8")
    settings = GatewaySettings(ui_dir=str(tmp_path / "ui"))
    deps = build()
    deps.settings = settings
    with TestClient(create_app(deps, settings=settings)) as client:
        response = client.get("/api/setup")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        # The mount is genuinely there, so this test would catch the ordering
        # rather than a missing mount.
        assert client.get("/").text.startswith("<!doctype html>")
