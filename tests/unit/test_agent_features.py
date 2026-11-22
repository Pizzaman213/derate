"""What each node's agent can do, carried to the coordinator and the screen.

A node on an older build and a node on this build whose copy receiver failed
to bind both lack the copy routes, and only the first is cured by upgrading.
`features` on the trusted /agent/health is how the coordinator tells them
apart, and None ("has not said") must never collapse into [] ("said, none").
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from control_plane.gateway import serialize
from control_plane.gateway.app import create_app
from control_plane.registry.agent import AGENT_FEATURES, NodeAgent, create_agent_app
from control_plane.version import build_id
from tests.unit.test_agent_tokens import AGENT, profile
from tests.unit.test_gateway import NODE_PROFILES, FakeRegistry, build_deps
from tests.unit.test_registry import SPARK_01, SPARK_02, FakeClient, make_registry, run

URL_02 = "http://10.0.0.12:8081"


class _Receiver:
    """Stands in for a bound TransferReceiver: all the agent asks is that it
    exists and has a capacity reading."""

    def capacity(self) -> dict:
        return {}


def test_an_agent_with_no_receiver_reports_only_link_health():
    agent = NodeAgent(profile(), token=AGENT)
    assert agent.features() == ["link_health"]


def test_a_bound_receiver_adds_the_copy_features():
    agent = NodeAgent(profile(), token=AGENT)
    agent.transfer_receiver = _Receiver()
    assert agent.features() == list(AGENT_FEATURES)


def test_features_ride_the_trusted_health_and_profile_only():
    agent = NodeAgent(profile(), token=AGENT)
    agent.transfer_receiver = _Receiver()
    http = TestClient(create_agent_app(agent))

    trusted = http.get("/agent/health", headers={"X-Derate-Token": AGENT}).json()
    assert trusted["features"] == list(AGENT_FEATURES)
    assert http.get("/agent/profile", headers={"X-Derate-Token": AGENT}).json()["features"] == list(
        AGENT_FEATURES
    )
    # Open callers learn neither the version nor the capabilities.
    assert "features" not in http.get("/agent/health").json()
    assert "features" not in http.get("/agent/profile").json()


def _member(tmp_path):
    client = FakeClient()
    client.serve(URL_02, SPARK_02)
    registry = make_registry(tmp_path, client=client, local=SPARK_01)
    run(registry.add_node("10.0.0.12"))
    return client, registry


def test_the_heartbeat_records_the_features_a_node_reports(tmp_path):
    client, registry = _member(tmp_path)
    client.health_bodies[URL_02] = {"build": "abc", "features": ["link_health", "copy_receiver"]}

    run(registry.check_health("spark-02"))

    assert registry.get_node("spark-02").features == ("link_health", "copy_receiver")


def test_an_agent_without_the_field_is_unknown_not_empty(tmp_path):
    client, registry = _member(tmp_path)
    run(registry.check_health("spark-02"))
    assert registry.get_node("spark-02").features is None

    client.health_bodies[URL_02] = {"features": []}
    run(registry.check_health("spark-02"))
    assert registry.get_node("spark-02").features == ()


def test_a_node_rolled_back_to_an_older_agent_stops_claiming_features(tmp_path):
    """Unlike `build`, a vanished list is not kept: the older agent really
    does lack the routes, and a stale list would offer it as a copy target."""
    client, registry = _member(tmp_path)
    client.health_bodies[URL_02] = {"features": list(AGENT_FEATURES)}
    run(registry.check_health("spark-02"))

    client.health_bodies[URL_02] = {"build": "older"}
    run(registry.check_health("spark-02"))

    assert registry.get_node("spark-02").features is None


def test_a_malformed_features_value_is_unknown(tmp_path):
    client, registry = _member(tmp_path)
    client.health_bodies[URL_02] = {"features": "copy_receiver"}
    run(registry.check_health("spark-02"))
    assert registry.get_node("spark-02").features is None


def test_a_failed_probe_keeps_what_the_node_last_said(tmp_path):
    client, registry = _member(tmp_path)
    client.health_bodies[URL_02] = {"features": ["link_health"]}
    run(registry.check_health("spark-02"))
    client.kill(URL_02)

    assert run(registry.check_health("spark-02")) is False
    assert registry.get_node("spark-02").features == ("link_health",)


def test_the_coordinators_own_node_reports_its_local_agent(tmp_path):
    registry = make_registry(tmp_path, local=SPARK_01)
    agent = NodeAgent(SPARK_01)
    registry.set_local_features(agent.features)

    run(registry.check_health("spark-01"))

    state = registry.get_node("spark-01")
    assert state.features == ("link_health",)
    assert state.build == build_id()


def test_the_node_row_keeps_unknown_apart_from_empty():
    from tests.fixtures import node_state

    state = node_state(NODE_PROFILES["spark-01"])
    assert serialize.node_payload(state)["features"] is None
    state.features = ()
    assert serialize.node_payload(state)["features"] == []
    state.features = ("link_health", "copy_receiver")
    assert serialize.node_payload(state)["features"] == ["link_health", "copy_receiver"]


def test_the_cluster_payload_carries_the_coordinator_build_and_node_features():
    from tests.fixtures import node_state

    current = node_state(NODE_PROFILES["spark-01"])
    current.build = build_id() or "same"
    current.features = tuple(AGENT_FEATURES)
    older = node_state(NODE_PROFILES["spark-02"])
    older.build = "0123456789ab"
    deps = build_deps(registry=FakeRegistry([current, older]))

    with TestClient(create_app(deps)) as client:
        body = client.get("/api/cluster").json()

    assert body["build"] == (build_id() or None)
    assert body["agent_features"] == list(AGENT_FEATURES)
    rows = {n["node_id"]: n for n in body["nodes"]}
    assert rows["spark-01"]["features"] == list(AGENT_FEATURES)
    assert rows["spark-02"]["features"] is None
    assert rows["spark-02"]["build"] == "0123456789ab"
    assert rows["spark-02"]["build_skew"] is bool(build_id())
