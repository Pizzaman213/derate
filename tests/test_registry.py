"""Agent A: registry, discovery and telemetry.

Organised around the acceptance list in agents/A-registry.md. Every test that
names an acceptance criterion says so, so a failure points at the requirement
rather than at an implementation detail.

No network, no mDNS and no GPU are required. The two tests that can use real
hardware skip themselves when it is absent.
"""

from __future__ import annotations

import asyncio
import os
import stat
import time

import pytest

from control_plane.contracts import DeviceClass, NodeProfile, NodeState
from control_plane.contracts import (
    GB10_ADDRESSABLE,
    GB10_MEM_BANDWIDTH,
    GB10_TOTAL_MEMORY,
)
from control_plane.registry import (
    Advertiser,
    BridgeNetworkError,
    JoinRejected,
    NodeAgent,
    NodeNotFound,
    ProbeFailed,
    Registry,
    RegistryConfig,
    StubRegistry,
    detect_bridge_networking,
    load_or_create_identity,
    probe_local,
    require_host_networking,
)
from control_plane.registry import probe as probe_mod
from control_plane.registry.bootstrap import RoleDecision, resolve_role
from control_plane.registry.config import (
    HEARTBEAT_INTERVAL_S,
    HEARTBEAT_MISSES_UNHEALTHY,
    HEARTBEAT_TIMEOUT_S,
    TELEMETRY_RING_SAMPLES,
)
from control_plane.registry.discovery import DiscoveredPeer
from control_plane.registry.net import normalize_agent_url
from control_plane.registry.probe import bandwidth_for
from control_plane.registry.serde import profile_from_dict, profile_to_dict
from control_plane.registry.telemetry import (
    RingBuffer,
    TelemetrySample,
    TelemetryStore,
    read_telemetry,
)

from tests.fixtures import SPARK_01, SPARK_02, WS_3090


def run(coro):
    """Run one coroutine. Avoids a hard dependency on pytest-asyncio."""
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

GB10_ROWS = [["NVIDIA GB10", "[N/A]", "580.173.02", "12.1"]]
RTX3090_ROWS = [["NVIDIA GeForce RTX 3090", "24576", "550.54.14", "8.6"]]


def make_profile(node_id: str = "spark-01", **overrides) -> NodeProfile:
    base = dict(
        node_id=node_id,
        hostname=node_id,
        address="10.0.0.11",
        device_class=DeviceClass.GB10,
        gpu_name="NVIDIA GB10",
        gpu_count=1,
        total_memory=GB10_TOTAL_MEMORY,
        addressable_memory=GB10_ADDRESSABLE,
        memory_bandwidth_gbps=GB10_MEM_BANDWIDTH,
        compute_capability="12.1",
        driver_version="580.173.02",
    )
    base.update(overrides)
    return NodeProfile(**base)


class FakeClient:
    """Stands in for other nodes' agents. Routes on the URL suffix."""

    def __init__(self) -> None:
        self.profiles: dict[str, dict] = {}
        self.telemetry: dict[str, dict] = {}
        self.down: set[str] = set()
        self.join_responses: dict[str, dict] = {}
        self.join_rejects: set[str] = set()
        self.posts: list[tuple[str, dict]] = []

    def serve(self, agent_url: str, profile: NodeProfile) -> None:
        self.profiles[agent_url.rstrip("/")] = profile_to_dict(profile)

    def kill(self, agent_url: str) -> None:
        self.down.add(agent_url.rstrip("/"))

    def revive(self, agent_url: str) -> None:
        self.down.discard(agent_url.rstrip("/"))

    async def get_json(self, url: str, timeout: float) -> dict:
        for suffix in ("/agent/profile", "/agent/telemetry", "/agent/health"):
            if url.endswith(suffix):
                base = url[: -len(suffix)]
                break
        else:
            raise ProbeFailed(f"unroutable {url}")
        if base in self.down:
            raise ProbeFailed(f"unreachable {url}")
        if suffix == "/agent/profile":
            if base not in self.profiles:
                raise ProbeFailed(f"no agent at {base}")
            return self.profiles[base]
        if suffix == "/agent/telemetry":
            return self.telemetry.get(base, {"available": False})
        if base not in self.profiles:
            raise ProbeFailed(f"no agent at {base}")
        return {"status": "ok"}

    async def post_json(self, url: str, payload: dict, timeout: float) -> dict:
        self.posts.append((url, payload))
        base = url.replace("/api/nodes/join", "")
        if base in self.join_rejects:
            raise ProbeFailed(f"POST {url} failed: 403 Forbidden")
        return self.join_responses.get(
            base, {"node_id": payload["profile"]["node_id"], "cluster_id": "c-test", "status": "candidate"}
        )


def make_registry(tmp_path, client=None, local=None, token="tok-123") -> Registry:
    config = RegistryConfig(data_dir=tmp_path, token=token, agent_port=8081)
    return Registry(config=config, local_profile=local, client=client or FakeClient())


# ----------------------------------------------------------------------
# 1. Hardware probe
# ----------------------------------------------------------------------


def test_gb10_uses_constants_not_nvidia_smi():
    """Acceptance: probing a Spark yields GB10 and 119.7 GiB addressable.

    The row deliberately carries '[N/A]' for memory.total, which is what a real
    DGX Spark reports: unified memory has no framebuffer to describe.
    """
    profile = probe_local(node_id="spark-01", hostname="spark-01", address="10.0.0.11", rows=GB10_ROWS)
    assert profile.device_class is DeviceClass.GB10
    assert profile.addressable_memory == GB10_ADDRESSABLE
    assert profile.addressable_memory / 1024**3 == pytest.approx(119.7, abs=0.05)
    assert profile.total_memory == GB10_TOTAL_MEMORY
    assert profile.memory_bandwidth_gbps == GB10_MEM_BANDWIDTH
    assert profile.compute_capability == "12.1"


def test_gb10_addressable_is_not_the_nameplate():
    """The whole reason the constant exists: 119.7 is not 128."""
    profile = probe_local(address="10.0.0.11", rows=GB10_ROWS)
    assert profile.addressable_memory < profile.total_memory


def test_usable_memory_guardrail():
    """Acceptance: usable_memory(0.90) on GB10 is roughly 107.7 GiB."""
    profile = probe_local(address="10.0.0.11", rows=GB10_ROWS)
    assert profile.usable_memory(0.90) / 1024**3 == pytest.approx(107.7, abs=0.1)


def test_discrete_card_reserves_one_gib():
    profile = probe_local(node_id="ws", address="10.0.0.50", rows=RTX3090_ROWS)
    assert profile.device_class is DeviceClass.DISCRETE
    assert profile.total_memory == 24 * 1024**3
    assert profile.addressable_memory == 23 * 1024**3
    assert profile.memory_bandwidth_gbps == 936.0


def test_multi_gpu_discrete_sums_memory():
    profile = probe_local(address="10.0.0.50", rows=RTX3090_ROWS * 2)
    assert profile.gpu_count == 2
    assert profile.total_memory == 48 * 1024**3
    assert profile.addressable_memory == 46 * 1024**3
    # Bandwidth stays per-GPU; the strength score multiplies by gpu_count.
    assert profile.memory_bandwidth_gbps == 936.0


def test_unknown_card_returns_zero_bandwidth_not_a_guess():
    profile = probe_local(address="10.0.0.60", rows=[["Matrox Millennium", "8192", "1.0", "1.0"]])
    assert profile.device_class is DeviceClass.DISCRETE
    assert profile.memory_bandwidth_gbps == 0.0


@pytest.mark.parametrize(
    "name,expected",
    [
        ("NVIDIA GeForce RTX 3090", 936.0),
        ("NVIDIA GeForce RTX 4090", 1008.0),
        ("NVIDIA RTX A6000", 768.0),
        ("NVIDIA H100 PCIe", 3350.0),
        ("NVIDIA A100-SXM4-80GB", 2039.0),
        ("NVIDIA T4", 0.0),
    ],
)
def test_bandwidth_lookup_table(name, expected):
    assert bandwidth_for(name) == expected


def test_missing_nvidia_smi_returns_unknown_and_does_not_raise(monkeypatch):
    """Acceptance: no nvidia-smi returns UNKNOWN and does not raise."""
    monkeypatch.setattr(probe_mod, "run_nvidia_smi", lambda *a, **k: None)
    profile = probe_local(node_id="headless", hostname="headless", address="10.0.0.9")
    assert profile.device_class is DeviceClass.UNKNOWN
    assert profile.total_memory == 0
    assert profile.addressable_memory == 0
    assert profile.usable_memory(0.90) == 0
    assert profile.gpu_count == 0
    # Still identifiable: the planner skips it, the UI can still list it.
    assert profile.node_id == "headless"


def test_probe_never_raises_on_garbage(monkeypatch):
    def explode(*a, **k):
        raise RuntimeError("nvidia-smi segfaulted")

    monkeypatch.setattr(probe_mod, "run_nvidia_smi", explode)
    profile = probe_local(address="10.0.0.9")
    assert profile.device_class is DeviceClass.UNKNOWN


def test_discrete_card_with_unreadable_memory_is_zeroed_not_guessed():
    profile = probe_local(address="10.0.0.9", rows=[["NVIDIA GeForce RTX 3090", "[N/A]", "550", "8.6"]])
    assert profile.addressable_memory == 0
    assert profile.usable_memory() == 0


@pytest.mark.skipif(
    not probe_mod.run_nvidia_smi(probe_mod.PROBE_QUERY), reason="no usable nvidia-smi here"
)
def test_real_hardware_probe_is_coherent():
    """Runs against whatever GPU this machine actually has."""
    profile = probe_local()
    assert profile.device_class is not DeviceClass.UNKNOWN
    assert profile.gpu_count >= 1
    assert profile.driver_version
    if profile.device_class is DeviceClass.GB10:
        assert profile.addressable_memory == GB10_ADDRESSABLE
        assert profile.usable_memory(0.90) / 1024**3 == pytest.approx(107.7, abs=0.1)


# ----------------------------------------------------------------------
# 2. Networking
# ----------------------------------------------------------------------


def test_bridge_networking_is_detected():
    assert detect_bridge_networking(interfaces=["lo", "eth0"], container=True) is True


def test_host_networking_container_is_not_bridged():
    """--network host sees the host's real interfaces, not just eth0."""
    assert (
        detect_bridge_networking(
            interfaces=["lo", "eth0", "docker0", "enp1s0f0np0", "wlP9s9"], container=True
        )
        is False
    )


def test_bare_metal_is_never_bridged():
    assert detect_bridge_networking(interfaces=["lo", "eth0"], container=False) is False


def test_bridge_networking_fails_loudly_naming_the_fix():
    """Acceptance: bridge networking fails at startup naming --network host."""
    with pytest.raises(BridgeNetworkError) as excinfo:
        require_host_networking(interfaces=["lo", "eth0"], container=True)
    message = str(excinfo.value)
    assert "--network host" in message
    assert "mDNS" in message


def test_bridge_check_can_be_overridden():
    require_host_networking(allow_bridge=True, interfaces=["lo", "eth0"], container=True)


@pytest.mark.parametrize(
    "address,expected",
    [
        ("10.0.0.5", "http://10.0.0.5:8081"),
        ("10.0.0.5:9000", "http://10.0.0.5:9000"),
        ("http://10.0.0.5:9000", "http://10.0.0.5:9000"),
        ("https://spark-02.local", "https://spark-02.local"),
        ("spark-02.local", "http://spark-02.local:8081"),
    ],
)
def test_normalize_agent_url(address, expected):
    assert normalize_agent_url(address, 8081) == expected


# ----------------------------------------------------------------------
# 3. Cluster identity and the token
# ----------------------------------------------------------------------


def test_token_is_generated_once_and_persists(tmp_path):
    first = load_or_create_identity(tmp_path)
    second = load_or_create_identity(tmp_path)
    assert first.token == second.token
    assert first.cluster_id == second.cluster_id
    assert first.persisted


def test_token_file_is_not_world_readable(tmp_path):
    identity = load_or_create_identity(tmp_path)
    mode = stat.S_IMODE(os.stat(identity.path).st_mode)
    assert mode == 0o600


def test_env_token_overrides_and_persists(tmp_path):
    load_or_create_identity(tmp_path)
    forced = load_or_create_identity(tmp_path, token="operator-supplied")
    assert forced.token == "operator-supplied"
    assert load_or_create_identity(tmp_path).token == "operator-supplied"


def test_unwritable_data_dir_still_yields_an_identity(tmp_path):
    unwritable = tmp_path / "nope"
    unwritable.write_text("i am a file, not a directory")
    identity = load_or_create_identity(unwritable / "sub")
    assert identity.token
    assert not identity.persisted


# ----------------------------------------------------------------------
# 4. Join, candidates and admission
# ----------------------------------------------------------------------


def test_join_with_wrong_token_is_rejected_and_leaves_no_trace(tmp_path):
    """Acceptance: a wrong token is rejected and the node appears nowhere."""
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client, token="correct-token")

    with pytest.raises(JoinRejected):
        run(registry.handle_join("wrong-token", SPARK_02, "http://10.0.0.12:8081"))

    assert registry.candidates() == []
    assert registry.list_nodes() == []
    assert registry.get_node("spark-02") is None


def test_join_with_missing_token_is_rejected(tmp_path):
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client)
    for empty in (None, ""):
        with pytest.raises(JoinRejected):
            run(registry.handle_join(empty, SPARK_02, "http://10.0.0.12:8081"))
    assert registry.candidates() == []


def test_join_is_rejected_when_probe_back_fails(tmp_path):
    """A joiner that will not answer as itself is not a node, it is a claim."""
    client = FakeClient()  # nothing served at that URL
    registry = make_registry(tmp_path, client=client, token="tok")
    with pytest.raises(JoinRejected):
        run(registry.handle_join("tok", SPARK_02, "http://10.0.0.12:8081"))
    assert registry.candidates() == []


def test_join_is_rejected_when_probe_back_disagrees(tmp_path):
    """The request body can claim any hardware. The probe-back is the truth."""
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client, token="tok")
    liar = make_profile("spark-99", address="10.0.0.12")
    with pytest.raises(JoinRejected):
        run(registry.handle_join("tok", liar, "http://10.0.0.12:8081"))
    assert registry.candidates() == []


def test_valid_join_creates_a_candidate_not_a_member(tmp_path):
    """Discovery proposes, a human accepts."""
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client, token="tok", local=SPARK_01)

    result = run(registry.handle_join("tok", SPARK_02, "http://10.0.0.12:8081"))

    assert result["node_id"] == "spark-02"
    assert result["cluster_id"] == registry.cluster_id()
    assert result["status"] == "candidate"
    # Separate collections, exposed separately.
    assert [c["node_id"] for c in registry.candidates()] == ["spark-02"]
    assert [s.profile.node_id for s in registry.list_nodes()] == ["spark-01"]
    assert registry.get_node("spark-02") is None


def test_candidate_carries_what_the_ui_needs_to_show_it(tmp_path):
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client, token="tok")
    run(registry.handle_join("tok", SPARK_02, "http://10.0.0.12:8081"))
    candidate = registry.candidates()[0]
    for key in ("node_id", "hostname", "address", "gpu_name", "device_class", "agent_url", "source"):
        assert key in candidate
    assert candidate["source"] == "join"


def test_admit_promotes_a_candidate(tmp_path):
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client, token="tok")
    run(registry.handle_join("tok", SPARK_02, "http://10.0.0.12:8081"))

    state = registry.admit("spark-02")

    assert isinstance(state, NodeState)
    assert state.healthy
    assert state.profile.node_id == "spark-02"
    assert registry.candidates() == []
    assert registry.get_node("spark-02") is state
    assert registry.agent_url("spark-02") == "http://10.0.0.12:8081"


def test_admitting_an_unknown_node_raises(tmp_path):
    registry = make_registry(tmp_path)
    with pytest.raises(NodeNotFound):
        registry.admit("ghost")


def test_mdns_discovery_only_proposes(tmp_path):
    """Trap: a found machine is a suggestion, never an automatic member."""
    registry = make_registry(tmp_path)
    registry.offer_candidate(SPARK_02, "http://10.0.0.12:8081")
    assert [c["node_id"] for c in registry.candidates()] == ["spark-02"]
    assert registry.list_nodes() == []
    assert registry.candidates()[0]["source"] == "mdns"


def test_dismiss_candidate(tmp_path):
    registry = make_registry(tmp_path)
    registry.offer_candidate(SPARK_02, "http://10.0.0.12:8081")
    registry.dismiss_candidate("spark-02")
    assert registry.candidates() == []


def test_rejoin_of_an_existing_member_refreshes_rather_than_duplicates(tmp_path):
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client, token="tok")
    run(registry.handle_join("tok", SPARK_02, "http://10.0.0.12:8081"))
    registry.admit("spark-02")
    registry.record_health("spark-02", False)
    registry.record_health("spark-02", False)
    registry.record_health("spark-02", False)
    assert not registry.get_node("spark-02").healthy

    result = run(registry.handle_join("tok", SPARK_02, "http://10.0.0.12:8081"))

    assert result["status"] == "member"
    assert len(registry.list_nodes()) == 1
    assert registry.get_node("spark-02").healthy


# ----------------------------------------------------------------------
# 5. Manual add
# ----------------------------------------------------------------------


def test_manual_add_probes_then_admits(tmp_path):
    """Discovery will fail for someone; a dead end is worse than a form."""
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client)

    state = run(registry.add_node("10.0.0.12"))

    assert state.profile.node_id == "spark-02"
    assert registry.get_node("spark-02") is state
    assert registry.candidates() == []  # a typed address is the admission


def test_manual_add_of_an_unreachable_address_fails(tmp_path):
    registry = make_registry(tmp_path, client=FakeClient())
    with pytest.raises(ProbeFailed):
        run(registry.add_node("10.0.0.99"))
    assert registry.list_nodes() == []


def test_remove_node_forgets_everything(tmp_path):
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client)
    run(registry.add_node("10.0.0.12"))
    registry.apply_sample("spark-02", TelemetrySample(1.0, 5, 10, 1.0, 2.0, 3.0))

    registry.remove_node("spark-02")

    assert registry.get_node("spark-02") is None
    assert registry.history("spark-02") == []


# ----------------------------------------------------------------------
# 6. Health
# ----------------------------------------------------------------------


def test_three_misses_marks_unhealthy_one_success_clears(tmp_path):
    registry = make_registry(tmp_path, local=SPARK_01)
    for _ in range(HEARTBEAT_MISSES_UNHEALTHY - 1):
        registry.record_health("spark-01", False)
        assert registry.get_node("spark-01").healthy, "must not flap on a single miss"
    registry.record_health("spark-01", False)
    assert not registry.get_node("spark-01").healthy

    registry.record_health("spark-01", True)
    assert registry.get_node("spark-01").healthy


def test_unhealthy_within_fifteen_seconds():
    """Acceptance: a dead node is unhealthy within 15 s."""
    assert HEARTBEAT_INTERVAL_S * HEARTBEAT_MISSES_UNHEALTHY <= 15.0
    assert HEARTBEAT_TIMEOUT_S <= 2.0


def test_going_unhealthy_keeps_the_node_and_its_last_telemetry(tmp_path):
    """Acceptance: unhealthy must not delete the node or its numbers.

    Greyed-out real values with a fault marker tell an operator more than a row
    that vanished.
    """
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client)
    run(registry.add_node("10.0.0.12"))
    registry.apply_sample(
        "spark-02",
        TelemetrySample(
            ts=time.time(), memory_used=99, memory_total=128,
            power_watts=71.0, temperature_c=62.0, utilization_pct=94.0,
        ),
    )

    client.kill("http://10.0.0.12:8081")
    for _ in range(HEARTBEAT_MISSES_UNHEALTHY):
        run(registry.health_round())

    state = registry.get_node("spark-02")
    assert state is not None, "the node must not be deleted"
    assert state.healthy is False
    assert state.memory_used == 99
    assert state.power_watts == 71.0
    assert state.temperature_c == 62.0
    assert registry.history("spark-02", 3600), "history must survive going unhealthy"


def test_health_recovers_on_one_success(tmp_path):
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client)
    run(registry.add_node("10.0.0.12"))

    client.kill("http://10.0.0.12:8081")
    for _ in range(HEARTBEAT_MISSES_UNHEALTHY):
        run(registry.health_round())
    assert not registry.get_node("spark-02").healthy

    client.revive("http://10.0.0.12:8081")
    run(registry.health_round())
    assert registry.get_node("spark-02").healthy


def test_healthy_nodes_filters(tmp_path):
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client, local=SPARK_01)
    run(registry.add_node("10.0.0.12"))
    for _ in range(HEARTBEAT_MISSES_UNHEALTHY):
        registry.record_health("spark-02", False)

    healthy = [s.profile.node_id for s in registry.healthy_nodes()]
    assert healthy == ["spark-01"]
    assert len(registry.list_nodes()) == 2


def test_telemetry_poll_failure_does_not_clear_the_last_sample(tmp_path):
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client)
    run(registry.add_node("10.0.0.12"))
    client.telemetry["http://10.0.0.12:8081"] = {
        "available": True, "ts": 10.0, "memory_used": 42, "memory_total": 100,
        "power_w": 70.0, "temp_c": 60.0, "util_pct": 90.0,
    }
    run(registry.telemetry_round())
    assert registry.get_node("spark-02").memory_used == 42

    client.kill("http://10.0.0.12:8081")
    run(registry.telemetry_round())
    assert registry.get_node("spark-02").memory_used == 42


# ----------------------------------------------------------------------
# 7. Telemetry and the ring buffer
# ----------------------------------------------------------------------


def test_ring_buffer_is_bounded():
    """Acceptance: 1 Hz for five minutes without leaking the ring buffer."""
    ring = RingBuffer()
    for i in range(5000):
        ring.add(TelemetrySample(float(i), i, 0, 0.0, 0.0, 0.0))
    assert len(ring) == TELEMETRY_RING_SAMPLES == 300
    assert ring.latest.ts == 4999.0


def test_ring_holds_five_minutes_at_one_hertz():
    ring = RingBuffer()
    for i in range(300):
        ring.add(TelemetrySample(float(i), 0, 0, 0.0, 0.0, 0.0))
    assert len(ring.window(60.0, now=299.0)) == 61  # a 60 s graph, inclusive


def test_store_does_not_leak_across_many_nodes():
    store = TelemetryStore()
    for node in ("a", "b", "c"):
        for i in range(1000):
            store.record(node, TelemetrySample(float(i), 0, 0, 0.0, 0.0, 0.0))
    assert all(store.size(n) == 300 for n in ("a", "b", "c"))


def test_history_returns_a_window(tmp_path):
    registry = make_registry(tmp_path, local=SPARK_01)
    for i in range(120):
        registry.apply_sample("spark-01", TelemetrySample(float(i), i, 0, 1.0, 2.0, 3.0))
    registry._clock = lambda: 119.0
    recent = registry.history("spark-01", seconds=60)
    assert len(recent) == 61
    assert recent[0]["ts"] == 59.0
    assert recent[-1]["ts"] == 119.0
    assert set(recent[0]) >= {"ts", "memory_used", "power_w", "temp_c", "util_pct"}


def test_telemetry_parses_a_normal_discrete_row():
    profile = probe_local(address="10.0.0.50", rows=RTX3090_ROWS)
    sample = run(read_telemetry(profile, now=5.0, rows=[["8192", "24576", "210.5", "68", "22"]]))
    assert sample.memory_used == 8192 * 1024**2
    assert sample.power_watts == 210.5
    assert sample.temperature_c == 68.0
    assert sample.utilization_pct == 22.0


def test_telemetry_aggregates_multiple_gpus():
    profile = probe_local(address="10.0.0.50", rows=RTX3090_ROWS * 2)
    sample = run(
        read_telemetry(
            profile, now=5.0,
            rows=[["8192", "24576", "200", "60", "20"], ["4096", "24576", "100", "75", "40"]],
        )
    )
    assert sample.memory_used == 12288 * 1024**2  # summed
    assert sample.power_watts == 300.0  # summed
    assert sample.temperature_c == 75.0  # hottest
    assert sample.utilization_pct == 30.0  # mean


def test_gb10_telemetry_falls_back_to_the_unified_pool(monkeypatch):
    """A real Spark reports [N/A] for memory.used. Zero would read as idle."""
    profile = probe_local(address="10.0.0.11", rows=GB10_ROWS)
    fake_host_memory(monkeypatch, total=121 * GIB, available=41 * GIB)
    sample = run(
        read_telemetry(
            profile, now=5.0,
            rows=[["[N/A]", "[N/A]", "59.15", "76", "96"]],
            apps_rows=[["1234", "70000"]],
        )
    )
    assert sample.memory_used == 80 * GIB  # the pool, OS included
    assert sample.power_watts == 59.15
    assert sample.temperature_c == 76.0
    assert sample.utilization_pct == 96.0


def test_telemetry_returns_none_when_nothing_can_be_read():
    profile = probe_local(address="10.0.0.11", rows=GB10_ROWS)
    assert run(read_telemetry(profile, rows=[])) is None


def test_snapshot_shape(tmp_path):
    registry = make_registry(tmp_path, local=SPARK_01)
    registry.apply_sample(
        "spark-01",
        TelemetrySample(ts=1.0, memory_used=int(SPARK_01.addressable_memory * 0.78),
                        memory_total=SPARK_01.total_memory, power_watts=71.0,
                        temperature_c=62.0, utilization_pct=94.0),
    )
    snap = registry.snapshot()
    assert set(snap) == {"ts", "cluster", "nodes"}
    assert snap["cluster"]["total_power_w"] == 71.0
    assert snap["cluster"]["node_count"] == 1
    node = snap["nodes"][0]
    assert node["node_id"] == "spark-01"
    assert node["memory_used_pct"] == pytest.approx(78.0, abs=0.2)
    assert node["util_pct"] == 94.0
    assert node["healthy"] is True


def test_snapshots_generator_ticks_and_is_cancellable(tmp_path):
    registry = make_registry(tmp_path, local=SPARK_01)

    async def collect():
        seen = []
        async for snap in registry.snapshots(interval=0.01):
            seen.append(snap)
            if len(seen) == 5:
                break
        return seen

    seen = run(collect())
    assert len(seen) == 5
    assert all(set(s) == {"ts", "cluster", "nodes"} for s in seen)


def test_snapshots_do_not_grow_the_ring(tmp_path):
    """The generator reads state; only the poll loop writes to the ring."""
    registry = make_registry(tmp_path, local=SPARK_01)
    for i in range(400):
        registry.apply_sample("spark-01", TelemetrySample(float(i), 0, 0, 0.0, 0.0, 0.0))

    async def collect():
        n = 0
        async for _ in registry.snapshots(interval=0.001):
            n += 1
            if n == 50:
                break

    run(collect())
    assert registry._telemetry.size("spark-01") == TELEMETRY_RING_SAMPLES


# ----------------------------------------------------------------------
# 8. The node agent
# ----------------------------------------------------------------------


def test_node_agent_serves_three_payloads():
    agent = NodeAgent(SPARK_01, role="worker", cluster_id="c-1", clock=lambda: 100.0)
    assert agent.profile_payload()["node_id"] == "spark-01"
    assert agent.profile_payload()["device_class"] == "gb10"

    health = agent.health_payload()
    assert health["status"] == "ok"
    assert health["role"] == "worker"
    assert health["uptime_s"] == 0.0

    telemetry = agent.telemetry_payload()
    assert telemetry["available"] is False  # nothing sampled yet


def test_node_agent_telemetry_after_a_sample(monkeypatch):
    agent = NodeAgent(SPARK_01, clock=lambda: 100.0)
    async def fake_read(profile, now=None):
        return TelemetrySample(now or 0.0, 5, 10, 1.0, 2.0, 3.0)

    monkeypatch.setattr("control_plane.registry.agent.read_telemetry", fake_read)
    run(agent.sample_once())
    payload = agent.telemetry_payload()
    assert payload["available"] is True
    assert payload["node_id"] == "spark-01"
    assert payload["memory_used"] == 5


def test_node_agent_start_stop_is_clean(monkeypatch):
    calls = []

    async def fake_read(profile, now=None):
        calls.append(1)
        return TelemetrySample(0.0, 1, 2, 0.0, 0.0, 0.0)

    monkeypatch.setattr("control_plane.registry.agent.read_telemetry", fake_read)

    async def cycle():
        agent = NodeAgent(SPARK_01)
        await agent.start(interval=0.001)
        await asyncio.sleep(0.05)
        await agent.stop()
        return agent

    agent = run(cycle())
    assert calls, "the sample loop must have run"
    assert agent._task is None


def test_agent_app_serves_the_three_endpoints():
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from control_plane.registry import create_agent_app

    agent = NodeAgent(SPARK_01, role="coordinator", cluster_id="c-1")
    with TestClient(create_agent_app(agent)) as http:
        assert http.get("/agent/profile").json()["node_id"] == "spark-01"
        assert http.get("/agent/health").json()["role"] == "coordinator"
        assert http.get("/agent/telemetry").status_code == 200


def test_advertiser_is_inert_without_zeroconf():
    """The package must import and test on a machine with no zeroconf."""
    advertiser = Advertiser("spark-01", "coordinator", "c-1", "10.0.0.11", 8081)
    from control_plane.registry.discovery import HAVE_ZEROCONF

    if not HAVE_ZEROCONF:
        assert advertiser.start() is False
        assert advertiser.active is False
    advertiser.stop()  # must never raise


# ----------------------------------------------------------------------
# 9. Role resolution
# ----------------------------------------------------------------------


async def _no_peers(*a, **k):
    return []


def test_no_coordinator_found_becomes_coordinator():
    config = RegistryConfig()
    decision = run(resolve_role(config, SPARK_01, "http://10.0.0.11:8081", browse=_no_peers))
    assert decision.role == "coordinator"
    assert not decision.joined


def test_forced_coordinator_skips_discovery():
    async def explode(*a, **k):
        raise AssertionError("must not browse when the role is forced")

    config = RegistryConfig(role="coordinator")
    decision = run(resolve_role(config, SPARK_01, "http://10.0.0.11:8081", browse=explode))
    assert decision.role == "coordinator"


def test_coordinator_found_starts_as_worker_and_joins():
    """Acceptance: the second container joins with no configuration."""
    peer = DiscoveredPeer("spark-01", "coordinator", "c-1", "10.0.0.11", 8081)
    joined = {}

    async def one_peer(*a, **k):
        return [peer]

    async def join(url, token, profile, agent_url, **k):
        joined.update(url=url, token=token, node_id=profile.node_id)
        return {"node_id": profile.node_id, "cluster_id": "c-1", "status": "candidate"}

    config = RegistryConfig(token="tok")
    decision = run(
        resolve_role(config, SPARK_02, "http://10.0.0.12:8081", browse=one_peer, join=join)
    )
    assert decision.role == "worker"
    assert decision.joined
    assert decision.cluster_id == "c-1"
    assert decision.status == "candidate"
    assert joined["token"] == "tok"
    assert joined["url"] == "http://10.0.0.11:8080"


def test_workers_are_ignored_when_browsing_for_a_coordinator():
    async def workers_only(*a, **k):
        return [DiscoveredPeer("spark-03", "worker", "c-1", "10.0.0.13", 8081)]

    decision = run(
        resolve_role(RegistryConfig(), SPARK_02, "http://10.0.0.12:8081", browse=workers_only)
    )
    assert decision.role == "coordinator"


def test_token_mismatch_starts_our_own_cluster_rather_than_joining_theirs():
    async def one_peer(*a, **k):
        return [DiscoveredPeer("other", "coordinator", "c-other", "10.0.0.99", 8081)]

    async def reject(*a, **k):
        raise JoinRejected("bad token")

    decision = run(
        resolve_role(RegistryConfig(), SPARK_02, "http://10.0.0.12:8081", browse=one_peer, join=reject)
    )
    assert decision.role == "coordinator"
    assert "token did not match" in decision.reason


def test_explicit_join_address_skips_discovery():
    async def explode(*a, **k):
        raise AssertionError("SPARKPLANE_JOIN must skip discovery")

    async def join(url, token, profile, agent_url, **k):
        assert url == "http://10.9.9.9:8080"
        return {"node_id": profile.node_id, "cluster_id": "c-far", "status": "candidate"}

    config = RegistryConfig(join_address="10.9.9.9", token="tok")
    decision = run(
        resolve_role(config, SPARK_02, "http://10.0.0.12:8081", browse=explode, join=join)
    )
    assert decision.role == "worker"
    assert decision.cluster_id == "c-far"


def test_explicit_join_address_failure_is_not_swallowed():
    """A named coordinator that rejects us is a config error, not a new cluster."""
    async def reject(*a, **k):
        raise JoinRejected("bad token")

    config = RegistryConfig(join_address="10.9.9.9", token="wrong")
    with pytest.raises(JoinRejected):
        run(resolve_role(config, SPARK_02, "http://10.0.0.12:8081", join=reject))


def test_forced_worker_with_no_coordinator_waits_to_be_found():
    config = RegistryConfig(role="worker")
    decision = run(resolve_role(config, SPARK_02, "http://10.0.0.12:8081", browse=_no_peers))
    assert decision.role == "worker"
    assert not decision.joined


def test_role_is_sticky_for_the_process(tmp_path):
    registry = make_registry(tmp_path, local=SPARK_01)
    assert registry.role() == "coordinator"
    assert registry.role() == "coordinator"


def test_config_rejects_an_unknown_role():
    with pytest.raises(ValueError):
        RegistryConfig.from_env({"SPARKPLANE_ROLE": "leader"})


def test_config_reads_the_environment():
    config = RegistryConfig.from_env(
        {
            "SPARKPLANE_ROLE": "worker",
            "SPARKPLANE_TOKEN": "tok",
            "SPARKPLANE_JOIN": "10.0.0.1",
            "SPARKPLANE_AGENT_PORT": "9091",
            "SPARKPLANE_DATA_DIR": "/tmp/sp",
        }
    )
    assert config.role == "worker"
    assert config.token == "tok"
    assert config.join_address == "10.0.0.1"
    assert config.agent_port == 9091
    assert str(config.data_dir) == "/tmp/sp"


# ----------------------------------------------------------------------
# 10. Two containers, no configuration: the demo
# ----------------------------------------------------------------------


def test_two_nodes_form_a_cluster_with_no_configuration(tmp_path):
    """Acceptance: first becomes coordinator, second appears as a candidate.

    mDNS itself is not exercised here; the browse result is injected. What is
    exercised is everything the two containers do with it.
    """
    client = FakeClient()
    client.serve("http://10.0.0.11:8081", SPARK_01)
    client.serve("http://10.0.0.12:8081", SPARK_02)

    # Node 1: nothing on the network, so it coordinates.
    first = run(resolve_role(RegistryConfig(), SPARK_01, "http://10.0.0.11:8081", browse=_no_peers))
    assert first.role == "coordinator"
    coordinator = Registry(
        config=RegistryConfig(data_dir=tmp_path, agent_port=8081),
        local_profile=SPARK_01,
        client=client,
    )
    token = coordinator.cluster_token()

    # Node 2: same command, finds node 1, joins with the shared token.
    async def browse_finds_first(*a, **k):
        return [DiscoveredPeer("spark-01", "coordinator", coordinator.cluster_id(), "10.0.0.11", 8081)]

    async def join(url, tok, profile, agent_url, **k):
        return await coordinator.handle_join(tok, profile, agent_url)

    second = run(
        resolve_role(
            RegistryConfig(token=token), SPARK_02, "http://10.0.0.12:8081",
            browse=browse_finds_first, join=join,
        )
    )

    assert second.role == "worker"
    assert second.joined
    assert second.cluster_id == coordinator.cluster_id()
    # Found on your network, not yet a member.
    assert [c["node_id"] for c in coordinator.candidates()] == ["spark-02"]
    assert [s.profile.node_id for s in coordinator.list_nodes()] == ["spark-01"]

    coordinator.admit("spark-02")
    assert sorted(s.profile.node_id for s in coordinator.list_nodes()) == ["spark-01", "spark-02"]
    assert coordinator.candidates() == []


# ----------------------------------------------------------------------
# 11. Serialisation
# ----------------------------------------------------------------------


def test_profile_round_trips():
    for profile in (SPARK_01, WS_3090):
        assert profile_from_dict(profile_to_dict(profile)) == profile


def test_profile_from_dict_tolerates_an_unknown_device_class():
    payload = profile_to_dict(SPARK_01)
    payload["device_class"] = "quantum"
    payload["something_from_a_newer_build"] = True
    assert profile_from_dict(payload).device_class is DeviceClass.UNKNOWN


def test_nodes_payload_labels_the_coordinator(tmp_path):
    registry = make_registry(tmp_path, local=SPARK_01)
    row = registry.nodes_payload()[0]
    assert row["role"] == "coordinator"
    assert row["node_id"] == "spark-01"
    assert "profile" in row and "healthy" in row


# ----------------------------------------------------------------------
# 12. Day-0 stub
# ----------------------------------------------------------------------


def test_stub_returns_the_three_fixture_nodes():
    stub = StubRegistry()
    ids = sorted(s.profile.node_id for s in stub.list_nodes())
    assert ids == ["spark-01", "spark-02", "ws-3090"]
    assert len(stub.healthy_nodes()) == 3


def test_stub_matches_the_frozen_fixtures():
    """The stub must not drift from tests/fixtures."""
    stub = StubRegistry()
    assert stub.get_node("spark-01").profile == SPARK_01
    assert stub.get_node("spark-02").profile == SPARK_02
    assert stub.get_node("ws-3090").profile == WS_3090


def test_stub_telemetry_is_plausible():
    stub = StubRegistry()
    for state in stub.list_nodes():
        assert 0 < state.memory_used < state.profile.addressable_memory
        assert state.power_watts > 0
        assert 0 <= state.utilization_pct <= 100
        assert state.temperature_c > 0


def test_stub_satisfies_the_registry_port():
    """Downstream agents code against this within the first hour."""
    stub = StubRegistry()
    assert isinstance(stub.list_nodes(), list)
    assert isinstance(stub.get_node("spark-01"), NodeState)
    assert stub.get_node("nope") is None
    assert stub.role() == "coordinator"
    assert stub.cluster_token()
    assert stub.candidates() == []
    assert len(stub.history("spark-01", 60)) > 0


def test_stub_snapshots():
    stub = StubRegistry()

    async def first():
        async for snap in stub.snapshots(interval=0.001):
            return snap

    snap = run(first())
    assert len(snap["nodes"]) == 3
    assert snap["cluster"]["total_power_w"] > 0


# ----------------------------------------------------------------------
# 13. Two node agents over real HTTP
# ----------------------------------------------------------------------


def test_join_admit_health_and_telemetry_over_real_http(tmp_path, monkeypatch):
    """The whole path with nothing faked but the GPU and the wire's transport.

    Two node agents are served as real ASGI apps. The coordinator joins one of
    them, probes it back over HTTP, admits it, health-checks it, polls its
    telemetry, then watches it go unhealthy when the app stops answering.
    """
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    from control_plane.registry import HttpAgentClient, create_agent_app

    async def fake_read(profile, now=None):
        return TelemetrySample(
            ts=now or 1000.0, memory_used=int(profile.addressable_memory * 0.78),
            memory_total=profile.total_memory, power_watts=71.0,
            temperature_c=62.0, utilization_pct=94.0,
        )

    monkeypatch.setattr("control_plane.registry.agent.read_telemetry", fake_read)

    async def scenario():
        agent_a = NodeAgent(SPARK_01, role="coordinator", cluster_id="c-http", port=8081)
        agent_b = NodeAgent(SPARK_02, role="worker", cluster_id="c-http", port=8081)
        await agent_a.start(interval=3600)  # primed once, no repeat polling
        await agent_b.start(interval=3600)

        class Killable:
            """An ASGI app whose host can be taken off the network."""

            def __init__(self, app):
                self.app = app
                self.down = False

            async def __call__(self, scope, receive, send):
                if self.down:
                    raise ConnectionError("host is unreachable")
                await self.app(scope, receive, send)

        node_b = Killable(create_agent_app(agent_b))
        http = httpx.AsyncClient(
            mounts={
                "all://node-a": httpx.ASGITransport(app=create_agent_app(agent_a)),
                "all://node-b": httpx.ASGITransport(app=node_b),
            }
        )
        registry = Registry(
            config=RegistryConfig(data_dir=tmp_path, token="shared", agent_port=8081),
            local_profile=SPARK_01,
            client=HttpAgentClient(http),
        )

        url_b = "http://node-b:8081"

        # A wrong token never reaches the probe-back, and leaves no trace.
        with pytest.raises(JoinRejected):
            await registry.handle_join("nope", SPARK_02, url_b)
        assert registry.candidates() == []

        result = await registry.handle_join("shared", SPARK_02, url_b)
        assert result["status"] == "candidate"
        assert [c["node_id"] for c in registry.candidates()] == ["spark-02"]

        registry.admit("spark-02")
        await registry.health_round()
        assert registry.get_node("spark-02").healthy

        await registry.telemetry_round()
        state = registry.get_node("spark-02")
        assert state.memory_used == int(SPARK_02.addressable_memory * 0.78)
        assert state.power_watts == 71.0
        assert state.utilization_pct == 94.0

        # The far side stops answering. Three rounds, then unhealthy.
        await agent_b.stop()
        node_b.down = True
        for _ in range(HEARTBEAT_MISSES_UNHEALTHY):
            await registry.health_round()

        state = registry.get_node("spark-02")
        assert state.healthy is False
        assert state.memory_used == int(SPARK_02.addressable_memory * 0.78)
        assert registry.get_node("spark-02") is not None

        await agent_a.stop()
        await http.aclose()

    run(scenario())
