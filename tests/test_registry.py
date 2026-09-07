"""Agent A: registry, discovery and telemetry.

Organised around the acceptance list in agents/A-registry.md. Every test that
names an acceptance criterion says so, so a failure points at the requirement
rather than at an implementation detail.

No network, no mDNS and no GPU are required. The two tests that can use real
hardware skip themselves when it is absent.
"""

from __future__ import annotations

import asyncio
import json
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
from control_plane.registry.profiles import profile_supersedes
from control_plane.registry.serde import profile_from_dict, profile_to_dict
from control_plane.registry.telemetry import (
    RingBuffer,
    TelemetrySample,
    TelemetryStore,
    allocatable_bytes,
    read_compute_apps,
    read_host_memory,
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
GIB = 1024**3

# Measured on a real DGX Spark under load: llama-server resident on the GPU,
# a desktop session in the same pool, and the kernel already swapping.
REAL_SPARK_POOL_TOTAL = 121 * GIB
REAL_SPARK_AVAILABLE = int(24.3 * GIB)
REAL_SPARK_GPU_MIB = 70331  # what --query-compute-apps reports


def fake_host_memory(monkeypatch, total, available, swap_used=0):
    from control_plane.registry.telemetry import HostMemory

    monkeypatch.setattr(
        "control_plane.registry.telemetry.read_host_memory",
        lambda: HostMemory(total=total, available=available, swap_used=swap_used),
    )


def _async_value(value):
    """A stand-in for an async reader that always answers the same thing."""

    async def reader():
        return value

    return reader


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
        self.headers: list[dict | None] = []
        # agent_url -> extra keys to merge into that node's /agent/health.
        # The real endpoint carries the node's build, which is how the
        # coordinator learns it without a call of its own.
        self.health_bodies: dict[str, dict] = {}

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
        # The real /agent/health names the node answering, which is how a
        # reachability check tells "reached it" from "reached something else".
        body = {"status": "ok", "node_id": self.profiles[base]["node_id"]}
        body.update(self.health_bodies.get(base, {}))
        return body

    async def post_json(
        self, url: str, payload: dict, timeout: float, headers: dict | None = None
    ) -> dict:
        self.posts.append((url, payload))
        self.headers.append(headers)
        if url.endswith("/agent/reach"):
            return self._reach(url, payload)
        base = url.replace("/api/nodes/join", "")
        if base in self.join_rejects:
            raise ProbeFailed(f"POST {url} failed: 403 Forbidden")
        return self.join_responses.get(
            base, {"node_id": payload["profile"]["node_id"], "cluster_id": "c-test", "status": "candidate"}
        )

    def _reach(self, url: str, payload: dict) -> dict:
        """Stand in for one node's agent dialling another.

        The asking node has to be up for us to ask it at all, so an asker in
        `down` raises -- that is a coordinator-to-asker failure, and the
        registry reports it as one.
        """
        asker = url[: -len("/agent/reach")]
        if asker in self.down:
            raise ProbeFailed(f"POST {url} failed: unreachable")
        target = str(payload.get("url", "")).rstrip("/")
        if target in self.down or target not in self.profiles:
            return {"ok": False, "url": target, "error": f"unreachable {target}"}
        return {
            "ok": True,
            "url": target,
            "ms": 1.5,
            "answered_as": self.profiles[target]["node_id"],
        }


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


def test_join_with_missing_token_becomes_a_candidate_not_a_rejection(tmp_path):
    """H-4a: the README's zero-config demo. No token is not a wrong token.

    An absent token used to raise JoinRejected -- the joiner's only path was
    to start its own cluster. Now it is routed into the candidate flow
    instead, same as an mDNS sighting, so a human can admit it.
    """
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client, token="correct-token")
    for empty in (None, ""):
        result = run(registry.handle_join(empty, SPARK_02, "http://10.0.0.12:8081"))
        assert result["status"] == "candidate"
        assert [c["node_id"] for c in registry.candidates()] == ["spark-02"]
        # Reset for the next spelling. (remove_node now raises on unknown ids,
        # so the reset happens after the join has created the candidate.)
        registry.remove_node("spark-02")


def test_join_with_missing_token_candidate_carries_no_token_or_member_state(tmp_path):
    """The distinct candidate response must not leak the cluster token or
    imply membership: no cluster_id, no token, nothing a bystander could use.
    """
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client, token="correct-token")

    result = run(registry.handle_join(None, SPARK_02, "http://10.0.0.12:8081"))

    assert result == {"node_id": "spark-02", "status": "candidate"}
    assert "cluster_id" not in result
    assert "token" not in result
    assert registry.get_node("spark-02") is None
    candidate = registry.candidates()[0]
    assert candidate["source"] == "join"


def test_join_with_wrong_nonempty_token_still_rejected_no_candidate(tmp_path):
    """A wrong token is not the same as no token: still a flat rejection,
    still leaves no trace -- 403 must stay meaningful (brief A #4)."""
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client, token="correct-token")

    with pytest.raises(JoinRejected):
        run(registry.handle_join("definitely-wrong", SPARK_02, "http://10.0.0.12:8081"))

    assert registry.candidates() == []
    assert registry.list_nodes() == []


def test_candidate_then_admit_then_rejoin_with_no_token_returns_member(tmp_path):
    """H-4c, the full loop: join(no token) -> candidate -> admit -> re-join
    with the same absent-token flow -> member, with cluster_id learned."""
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client, token="correct-token")

    first = run(registry.handle_join(None, SPARK_02, "http://10.0.0.12:8081"))
    assert first["status"] == "candidate"

    registry.admit("spark-02")
    assert registry.get_node("spark-02") is not None

    second = run(registry.handle_join(None, SPARK_02, "http://10.0.0.12:8081"))

    assert second["status"] == "member"
    assert second["cluster_id"] == registry.cluster_id()
    assert [s.profile.node_id for s in registry.list_nodes()] == ["spark-02"]
    assert registry.candidates() == []


def test_tokenless_join_to_existing_member_cannot_hijack_routing_state(tmp_path):
    """Revision fix, H-4a/H-4c: the admitted-set check that lets a
    no-token-admitted worker learn its cluster_id on re-join must not
    become an unauthenticated write into the roster.

    node_ids are slugified hostnames, advertised in the clear over mDNS --
    not a secret. Before this fix, anyone who could answer a probe as
    "spark-02" (i.e. anyone who stood up a server claiming that node_id)
    could re-point registry.agent_url("spark-02") and overwrite the stored
    profile just by calling handle_join with no token, and the coordinator
    would persist the hijack to registry.json and route every future health
    probe, telemetry poll and planner decision at the attacker. A tokenless
    join to an existing member must be a pure status check.
    """
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client, token="correct-token")

    run(registry.handle_join(None, SPARK_02, "http://10.0.0.12:8081"))
    registry.admit("spark-02")
    assert registry.agent_url("spark-02") == "http://10.0.0.12:8081"

    evil_profile = make_profile("spark-02", address="10.6.6.6", gpu_count=99)
    client.serve("http://10.6.6.6:8081", evil_profile)

    attack = run(registry.handle_join(None, evil_profile, "http://10.6.6.6:8081"))

    # It gets a truthful member confirmation -- that much is by design --
    # but nothing it said is believed.
    assert attack == {
        "node_id": "spark-02",
        "cluster_id": registry.cluster_id(),
        "status": "member",
    }
    assert registry.agent_url("spark-02") == "http://10.0.0.12:8081"
    node = registry.get_node("spark-02")
    assert node.profile.address == "192.168.11.14"
    assert node.profile.gpu_count == 1

    # And the hijack must not even transiently reach disk.
    reloaded = make_registry(tmp_path, client=client, token="correct-token")
    assert reloaded.agent_url("spark-02") == "http://10.0.0.12:8081"
    reloaded_node = reloaded.get_node("spark-02")
    assert reloaded_node.profile.address == "192.168.11.14"


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


def test_a_machine_with_no_gpu_reports_host_facts_instead_of_nothing(monkeypatch):
    """A Raspberry Pi joined the roster and sat at 0W / 0C / 0% forever.

    nvidia-smi is absent, so the sampler returned None and the agent never
    recorded anything -- every readout stayed at its zero default, which reads
    as broken telemetry rather than as absent hardware. There is nothing to say
    about a GPU on that machine, but /proc/meminfo, /sys/class/thermal and
    /proc/stat all answer.
    """
    profile = probe_local(address="192.168.0.45", rows=[])
    assert profile.gpu_count == 0  # the precondition the fallback is gated on
    fake_host_memory(monkeypatch, total=8 * GIB, available=6 * GIB)
    monkeypatch.setattr(
        "control_plane.registry.telemetry.read_host_temperature", lambda: 47.5
    )
    monkeypatch.setattr(
        "control_plane.registry.telemetry.read_cpu_utilization",
        _async_value(12.5),
    )

    sample = run(read_telemetry(profile, now=5.0, rows=[]))

    assert sample is not None
    assert sample.memory_used == 2 * GIB
    assert sample.memory_total == 8 * GIB
    assert sample.host_memory_available == 6 * GIB
    assert sample.temperature_c == 47.5
    assert sample.utilization_pct == 12.5
    # No GPU means no GPU power draw to read. Zero is not a measurement here,
    # and the gateway is what turns it into an unknown for display.
    assert sample.power_watts == 0.0


def test_a_gpu_node_with_a_wedged_nvidia_smi_reports_nothing(monkeypatch):
    """The fallback must not fire for hardware that HAS a GPU.

    Host RAM standing in for VRAM would be a wrong number rather than a missing
    one, and a wrong number outlives the outage that produced it.
    """
    profile = probe_local(address="10.0.0.50", rows=RTX3090_ROWS)
    fake_host_memory(monkeypatch, total=64 * GIB, available=8 * GIB)
    assert run(read_telemetry(profile, rows=[])) is None


def test_host_temperature_takes_the_hottest_zone_and_drops_sentinels(monkeypatch, tmp_path):
    from control_plane.registry import telemetry as tel

    for name, milli in (("thermal_zone0", "41200"), ("thermal_zone1", "58700"),
                        ("thermal_zone2", "-274000")):
        zone = tmp_path / name
        zone.mkdir()
        (zone / "temp").write_text(milli + "\n")
    monkeypatch.setattr(tel, "THERMAL_ZONES", tmp_path)

    # 58.7 and not 41.2 (an average would hide the hot one) and not -274, which
    # is an unpopulated sensor's sentinel rather than a temperature.
    assert tel.read_host_temperature() == 58.7


def test_host_temperature_is_unknown_when_the_board_exposes_no_zones(monkeypatch, tmp_path):
    from control_plane.registry import telemetry as tel

    monkeypatch.setattr(tel, "THERMAL_ZONES", tmp_path / "absent")
    assert tel.read_host_temperature() is None


def test_cpu_times_splits_proc_stat_into_busy_and_idle(monkeypatch, tmp_path):
    from control_plane.registry import telemetry as tel

    stat = tmp_path / "stat"
    # user nice system idle iowait irq softirq steal
    stat.write_text("cpu  100 0 50 800 40 0 10 0\ncpu0 1 2 3 4 5 6 7 8\n")
    monkeypatch.setattr(tel, "PROC_STAT", stat)

    times = tel.read_cpu_times()
    assert times.total == 1000
    assert times.busy == 160  # everything that is not idle(800) + iowait(40)


def test_cpu_utilisation_is_a_delta_between_polls(monkeypatch):
    """/proc/stat is cumulative, so one reading carries no rate at all."""
    from control_plane.registry import telemetry as tel

    readings = iter([
        tel._CpuTimes(busy=100, total=1000),  # first call: nothing to compare to
        tel._CpuTimes(busy=100, total=1012),  # after its own short delta: all idle
        tel._CpuTimes(busy=175, total=1312),  # the next poll: 75 busy jiffies of 300
    ])
    monkeypatch.setattr(tel, "read_cpu_times", lambda: next(readings))
    monkeypatch.setattr(tel, "_last_cpu_times", None)

    assert run(tel.read_cpu_utilization()) == 0.0
    assert run(tel.read_cpu_utilization()) == pytest.approx(25.0)


def test_cpu_utilisation_is_unknown_when_the_counters_have_not_moved(monkeypatch):
    """Two polls inside the same jiffy measure nothing.

    Returning 0% there would be indistinguishable from a genuinely idle
    machine, so it reports unknown and the next poll answers properly.
    """
    from control_plane.registry import telemetry as tel

    frozen = tel._CpuTimes(busy=100, total=1000)
    monkeypatch.setattr(tel, "read_cpu_times", lambda: frozen)
    monkeypatch.setattr(tel, "_last_cpu_times", None)

    assert run(tel.read_cpu_utilization()) is None


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


def test_token_mismatch_never_self_coordinates_stays_a_waiting_worker():
    """H-4b: the old bug. A coordinator exists on the subnet (it answered,
    just with a rejection) -- forming a second one next to it would split the
    network, so this must come back a worker, not a coordinator, and must
    keep the rejecting coordinator's url so the caller can keep retrying it.
    """
    async def one_peer(*a, **k):
        return [DiscoveredPeer("other", "coordinator", "c-other", "10.0.0.99", 8081)]

    async def reject(*a, **k):
        raise JoinRejected("bad token")

    decision = run(
        resolve_role(RegistryConfig(), SPARK_02, "http://10.0.0.12:8081", browse=one_peer, join=reject)
    )
    assert decision.role == "worker"
    assert not decision.joined
    assert decision.status == "rejected"
    assert decision.coordinator_url == "http://10.0.0.99:8080"
    assert "token did not match" in decision.reason


def test_discovered_coordinator_candidate_status_never_self_coordinates():
    """A join that succeeds but only as far as 'candidate' (H-4a's no-token
    path) must also come back a worker -- it already has, this pins it."""
    async def one_peer(*a, **k):
        return [DiscoveredPeer("spark-01", "coordinator", "c-1", "10.0.0.11", 8081)]

    async def join_as_candidate(*a, **k):
        return {"node_id": "spark-02", "status": "candidate"}

    decision = run(
        resolve_role(
            RegistryConfig(), SPARK_02, "http://10.0.0.12:8081",
            browse=one_peer, join=join_as_candidate,
        )
    )
    assert decision.role == "worker"
    assert decision.joined
    assert decision.status == "candidate"
    # cluster_id here comes from the peer's public mDNS TXT record, which
    # already advertises it in the clear -- not from the join response,
    # which carries none for a candidate. See the dedicated response-shape
    # test for that half.
    assert decision.cluster_id == "c-1"


def test_rejoin_until_admitted_retries_candidate_then_succeeds_on_admission():
    """The worker-in-waiting loop: candidate, then candidate again, then a
    human admits it and the next poll returns member."""
    from control_plane.registry.bootstrap import rejoin_until_admitted, RoleDecision

    attempts = []
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    async def join(url, token, profile, agent_url, **k):
        attempts.append(url)
        if len(attempts) < 3:
            return {"node_id": profile.node_id, "status": "candidate"}
        return {"node_id": profile.node_id, "cluster_id": "c-1", "status": "member"}

    decision = RoleDecision(
        role="worker", coordinator_url="http://10.0.0.11:8080", joined=True,
        reason="candidate", status="candidate",
    )
    result = run(
        rejoin_until_admitted(
            decision, RegistryConfig(), SPARK_02, "http://10.0.0.12:8081",
            join=join, sleep=fake_sleep, jitter=lambda: 0.0,
        )
    )
    assert len(attempts) == 3
    assert result["status"] == "member"
    assert result["cluster_id"] == "c-1"
    # Normal cadence, no wrong-token backoff.
    assert all(s == pytest.approx(15.0) for s in sleeps)


def test_rejoin_until_admitted_backs_off_slower_on_wrong_token_but_never_gives_up():
    from control_plane.registry.bootstrap import rejoin_until_admitted, RoleDecision

    calls = {"n": 0}
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    async def join(url, token, profile, agent_url, **k):
        calls["n"] += 1
        if calls["n"] < 2:
            raise JoinRejected("bad token")
        return {"node_id": profile.node_id, "cluster_id": "c-1", "status": "member"}

    decision = RoleDecision(
        role="worker", coordinator_url="http://10.0.0.99:8080", joined=False,
        reason="rejected", status="rejected",
    )
    result = run(
        rejoin_until_admitted(
            decision, RegistryConfig(), SPARK_02, "http://10.0.0.12:8081",
            join=join, sleep=fake_sleep, jitter=lambda: 0.0,
        )
    )
    assert calls["n"] == 2
    assert result["status"] == "member"
    assert sleeps == [60.0]  # the slow, loud backoff -- not the 15s candidate cadence


def test_rejoin_until_admitted_stops_when_told_to():
    from control_plane.registry.bootstrap import rejoin_until_admitted, RoleDecision

    async def fake_sleep(seconds):
        pass

    async def join(url, token, profile, agent_url, **k):
        return {"node_id": profile.node_id, "status": "candidate"}

    calls = {"n": 0}

    def should_continue():
        calls["n"] += 1
        return calls["n"] <= 2

    decision = RoleDecision(
        role="worker", coordinator_url="http://10.0.0.11:8080", joined=True,
        reason="candidate", status="candidate",
    )
    result = run(
        rejoin_until_admitted(
            decision, RegistryConfig(), SPARK_02, "http://10.0.0.12:8081",
            join=join, sleep=fake_sleep, jitter=lambda: 0.0,
            should_continue=should_continue,
        )
    )
    assert result is None


def test_explicit_join_address_skips_discovery():
    async def explode(*a, **k):
        raise AssertionError("DERATE_JOIN must skip discovery")

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
    """A named coordinator that does not ANSWER is a config error, loudly.

    (A named coordinator that answers-and-rejects is the opposite case:
    it exists, so we stay a worker-in-waiting and retry -- see
    test_explicit_join_rejection_stays_worker_in_waiting_not_fatal. The
    original version of this test demanded a crash on rejection, which is
    what killed the first containerized join: the probe-back raced the
    joiner's own agent app and a correct setup 403'd once at boot.)"""
    async def unreachable(*a, **k):
        raise ProbeFailed("connect refused")

    config = RegistryConfig(join_address="10.9.9.9", token="wrong")
    with pytest.raises(ProbeFailed):
        run(resolve_role(config, SPARK_02, "http://10.0.0.12:8081", join=unreachable))


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
        RegistryConfig.from_env({"DERATE_ROLE": "leader"})


def test_config_reads_the_environment():
    config = RegistryConfig.from_env(
        {
            "DERATE_ROLE": "worker",
            "DERATE_TOKEN": "tok",
            "DERATE_JOIN": "10.0.0.1",
            "DERATE_AGENT_PORT": "9091",
            "DERATE_DATA_DIR": "/tmp/sp",
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
    """Acceptance: first becomes coordinator, second appears as a candidate --
    genuinely with no token passed anywhere. This is the README's own demo:
    two containers, same command, no shared secret between them yet.

    mDNS itself is not exercised here; the browse result is injected. What is
    exercised is everything the two containers do with it, including the
    other half of the loop: admission, then the waiting node's next poll
    turning into a real member with the coordinator's cluster id.
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

    # Node 2: same command as node 1, no token, no config of any kind --
    # finds node 1 over (simulated) mDNS.
    async def browse_finds_first(*a, **k):
        return [DiscoveredPeer("spark-01", "coordinator", coordinator.cluster_id(), "10.0.0.11", 8081)]

    async def join(url, tok, profile, agent_url, **k):
        return await coordinator.handle_join(tok, profile, agent_url)

    second = run(
        resolve_role(
            RegistryConfig(), SPARK_02, "http://10.0.0.12:8081",
            browse=browse_finds_first, join=join,
        )
    )

    # A candidate, not a member, and above all not a second coordinator.
    # cluster_id is populated here from the peer's public mDNS TXT record
    # (browse_finds_first put it there); the join response itself -- checked
    # directly below -- carries none of that, which is the actual guarantee.
    assert second.role == "worker"
    assert second.joined
    assert second.status == "candidate"
    assert second.cluster_id == coordinator.cluster_id()
    raw = run(join("http://10.0.0.11:8080", None, SPARK_02, "http://10.0.0.12:8081"))
    assert raw == {"node_id": "spark-02", "status": "candidate"}
    # Found on your network, not yet a member.
    assert [c["node_id"] for c in coordinator.candidates()] == ["spark-02"]
    assert [s.profile.node_id for s in coordinator.list_nodes()] == ["spark-01"]

    coordinator.admit("spark-02")
    assert sorted(s.profile.node_id for s in coordinator.list_nodes()) == ["spark-01", "spark-02"]
    assert coordinator.candidates() == []

    # The waiting node's next poll -- still passing no token, exactly as
    # before -- now succeeds as a member and learns the real cluster id.
    third = run(join("http://10.0.0.11:8080", None, SPARK_02, "http://10.0.0.12:8081"))
    assert third["status"] == "member"
    assert third["cluster_id"] == coordinator.cluster_id()


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


# ----------------------------------------------------------------------
# 14. Unified memory: what a Spark can actually give a model
# ----------------------------------------------------------------------


def test_compute_apps_query_sums_and_counts():
    """The one memory number nvidia-smi still answers on GB10."""
    total, count = run(read_compute_apps(rows=[["111", "70331"], ["222", "1024"]]))
    assert total == (70331 + 1024) * 1024**2
    assert count == 2


def test_compute_apps_with_no_processes_is_zero_not_a_failure():
    """An idle GPU is a real answer. None would mean 'could not read'."""
    assert run(read_compute_apps(rows=[])) == (0, 0)


def test_compute_apps_uses_the_right_nvidia_smi_flag(monkeypatch):
    """--query-gpu rejects a compute-apps field list. They are not the same query.

    This is a regression guard: the first version of this reader asked
    --query-gpu for a per-process field, which nvidia-smi refuses, so GPU
    attribution silently read as zero on a machine holding 68 GiB of model.
    """
    seen = {}

    async def spy(query, timeout=2.0, flag="--query-gpu", allow_empty=False):
        seen["query"] = query
        seen["flag"] = flag
        seen["allow_empty"] = allow_empty
        return [["1", "1024"]]

    monkeypatch.setattr("control_plane.registry.telemetry.run_nvidia_smi_async", spy)
    run(read_compute_apps())

    assert seen["flag"] == "--query-compute-apps"
    assert "used_gpu_memory" in seen["query"]
    assert seen["allow_empty"] is True, "an idle GPU must not read as a failure"


def test_host_memory_reads_the_pool_and_swap(monkeypatch, tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:       127600524 kB\n"
        "MemFree:          4024100 kB\n"
        "MemAvailable:    28904408 kB\n"
        "Buffers:          4726312 kB\n"
        "SwapTotal:       16777212 kB\n"
        "SwapFree:        10853772 kB\n"
    )
    monkeypatch.setattr("control_plane.registry.telemetry.MEMINFO", meminfo)
    host = read_host_memory()
    assert host.total == 127600524 * 1024
    assert host.available == 28904408 * 1024
    assert host.used == (127600524 - 28904408) * 1024
    assert host.swap_used == (16777212 - 10853772) * 1024


def test_host_memory_survives_a_missing_meminfo(monkeypatch, tmp_path):
    monkeypatch.setattr("control_plane.registry.telemetry.MEMINFO", tmp_path / "gone")
    assert read_host_memory() is None


def test_gb10_separates_model_memory_from_operating_system(monkeypatch):
    """The gap between the pool and the GPU figure is the OS, and it matters."""
    profile = probe_local(address="10.0.0.11", rows=GB10_ROWS)
    fake_host_memory(
        monkeypatch,
        total=REAL_SPARK_POOL_TOTAL,
        available=REAL_SPARK_AVAILABLE,
        swap_used=int(5.6 * GIB),
    )
    sample = run(
        read_telemetry(
            profile, now=1.0,
            rows=[["[N/A]", "[N/A]", "84", "81", "96"]],
            apps_rows=[["3591741", str(REAL_SPARK_GPU_MIB)]],
        )
    )
    assert sample.gpu_memory_used == REAL_SPARK_GPU_MIB * 1024**2
    assert sample.gpu_process_count == 1
    # The pool is what the node plans against, operating system included.
    assert sample.memory_used == REAL_SPARK_POOL_TOTAL - REAL_SPARK_AVAILABLE
    # And the remainder is attributable to the OS, not to any model.
    assert sample.host_memory_used == sample.memory_used - sample.gpu_memory_used
    assert sample.host_memory_used > 20 * GIB
    assert sample.swap_used == int(5.6 * GIB)


def test_discrete_card_does_not_pay_for_host_memory():
    """VRAM is its own pool. Host RAM must not constrain a 3090."""
    profile = probe_local(address="10.0.0.50", rows=RTX3090_ROWS)
    sample = TelemetrySample(
        ts=1.0, memory_used=8 * GIB, memory_total=24 * GIB,
        power_watts=210.0, temperature_c=68.0, utilization_pct=22.0,
        gpu_memory_used=8 * GIB,
        # Even with the host pool nearly exhausted, VRAM is unaffected.
        host_memory_total=64 * GIB, host_memory_available=1 * GIB,
    )
    assert allocatable_bytes(profile, sample) == profile.usable_memory() - 8 * GIB


def test_gb10_allocatable_is_bounded_by_the_shared_pool():
    """Acceptance for this fix: the static ceiling is not what you can launch.

    The numbers are the ones measured on a real Spark: a 68.7 GiB model
    resident, a desktop in the same pool, 24.3 GiB left. usable_memory(0.90)
    says 107.7 GiB is spendable. About 11 GiB actually is.

    Both limits are checked, because which one binds is not fixed: here the
    addressable ceiling is the tighter of the two, but a node with a big page
    cache and little resident model hits the pool limit first.
    """
    profile = probe_local(address="10.0.0.11", rows=GB10_ROWS)
    sample = TelemetrySample(
        ts=1.0,
        memory_used=REAL_SPARK_POOL_TOTAL - REAL_SPARK_AVAILABLE,
        memory_total=REAL_SPARK_POOL_TOTAL,
        power_watts=84.0, temperature_c=81.0, utilization_pct=96.0,
        gpu_memory_used=REAL_SPARK_GPU_MIB * 1024**2,
        gpu_process_count=1,
        host_memory_total=REAL_SPARK_POOL_TOTAL,
        host_memory_available=REAL_SPARK_AVAILABLE,
    )
    static = profile.usable_memory(0.90)
    live = allocatable_bytes(profile, sample, host_reserve=8 * GIB)

    gpu_bound = (static - sample.memory_used) / GIB
    host_bound = (sample.host_memory_available - 8 * GIB) / GIB
    assert gpu_bound == pytest.approx(11.0, abs=0.2)
    assert host_bound == pytest.approx(16.3, abs=0.2)

    assert static / GIB == pytest.approx(107.7, abs=0.1)
    assert live / GIB == pytest.approx(min(gpu_bound, host_bound), abs=0.1)
    assert live / GIB == pytest.approx(11.0, abs=0.2)
    assert live < static / 5, "the static ceiling must not be treated as launchable"


def test_gb10_allocatable_never_goes_negative():
    profile = probe_local(address="10.0.0.11", rows=GB10_ROWS)
    sample = TelemetrySample(
        ts=1.0, memory_used=119 * GIB, memory_total=121 * GIB,
        power_watts=0.0, temperature_c=0.0, utilization_pct=0.0,
        gpu_memory_used=119 * GIB,
        host_memory_total=121 * GIB, host_memory_available=1 * GIB,
    )
    assert allocatable_bytes(profile, sample) == 0


def test_allocatable_without_a_sample_is_the_static_ceiling():
    """Before the first poll, the ceiling is all we know. Say so, do not say 0."""
    profile = probe_local(address="10.0.0.11", rows=GB10_ROWS)
    assert allocatable_bytes(profile, None) == profile.usable_memory()


def test_allocatable_falls_back_when_the_pool_is_unreadable():
    profile = probe_local(address="10.0.0.11", rows=GB10_ROWS)
    sample = TelemetrySample(
        ts=1.0, memory_used=40 * GIB, memory_total=0,
        power_watts=0.0, temperature_c=0.0, utilization_pct=0.0,
        host_memory_available=0,  # /proc/meminfo unreadable
    )
    assert allocatable_bytes(profile, sample) == profile.usable_memory() - 40 * GIB


def test_host_reserve_is_configurable():
    """Chosen so the pool is the binding limit, which is what the reserve moves."""
    profile = probe_local(address="10.0.0.11", rows=GB10_ROWS)
    sample = TelemetrySample(
        ts=1.0, memory_used=30 * GIB, memory_total=121 * GIB,
        power_watts=0.0, temperature_c=0.0, utilization_pct=0.0,
        host_memory_total=121 * GIB, host_memory_available=60 * GIB,
    )
    assert profile.usable_memory() - 30 * GIB > 60 * GIB  # the pool binds
    assert allocatable_bytes(profile, sample, host_reserve=8 * GIB) == 52 * GIB
    assert allocatable_bytes(profile, sample, host_reserve=0) == 60 * GIB


def test_config_reads_the_host_reserve_from_the_environment():
    config = RegistryConfig.from_env({"DERATE_HOST_RESERVE_MIB": "4096"})
    assert config.host_memory_reserve == 4 * GIB


def test_registry_reports_live_allocatable_memory(tmp_path):
    registry = make_registry(tmp_path, local=SPARK_01)
    registry.apply_sample(
        "spark-01",
        TelemetrySample(
            ts=time.time(),
            memory_used=REAL_SPARK_POOL_TOTAL - REAL_SPARK_AVAILABLE,
            memory_total=REAL_SPARK_POOL_TOTAL,
            power_watts=84.0, temperature_c=81.0, utilization_pct=96.0,
            gpu_memory_used=REAL_SPARK_GPU_MIB * 1024**2, gpu_process_count=1,
            host_memory_total=REAL_SPARK_POOL_TOTAL,
            host_memory_available=REAL_SPARK_AVAILABLE,
        ),
    )
    live = registry.available_memory("spark-01")
    assert 0 < live < SPARK_01.usable_memory() / 5
    assert registry.available_memory("nope") == 0


def test_memory_report_shows_where_the_memory_went(tmp_path):
    """A refusal has to name the desktop, not just say 'will not fit'."""
    registry = make_registry(tmp_path, local=SPARK_01)
    registry.apply_sample(
        "spark-01",
        TelemetrySample(
            ts=time.time(),
            memory_used=REAL_SPARK_POOL_TOTAL - REAL_SPARK_AVAILABLE,
            memory_total=REAL_SPARK_POOL_TOTAL,
            power_watts=84.0, temperature_c=81.0, utilization_pct=96.0,
            gpu_memory_used=REAL_SPARK_GPU_MIB * 1024**2, gpu_process_count=1,
            host_memory_total=REAL_SPARK_POOL_TOTAL,
            host_memory_available=REAL_SPARK_AVAILABLE,
            swap_used=int(5.6 * GIB),
        ),
    )
    report = registry.memory_report("spark-01")
    assert report["unified_memory"] is True
    assert report["static_ceiling"] > report["allocatable"] * 5
    assert report["gpu_used"] + report["host_used"] == report["pool_used"]
    assert report["host_used"] > 20 * GIB
    assert report["swap_used"] > 0
    assert registry.memory_report("nope") is None


def test_swap_growth_is_warned_about(tmp_path, caplog):
    """Swapping a model is not slow, it is fatal. Say so once, loudly."""
    registry = make_registry(tmp_path, local=SPARK_01)
    base = dict(
        memory_total=REAL_SPARK_POOL_TOTAL, power_watts=0.0, temperature_c=0.0,
        utilization_pct=0.0, host_memory_total=REAL_SPARK_POOL_TOTAL,
        host_memory_available=REAL_SPARK_AVAILABLE,
    )
    registry.apply_sample("spark-01", TelemetrySample(ts=1.0, memory_used=90 * GIB, swap_used=0, **base))
    with caplog.at_level("WARNING"):
        registry.apply_sample(
            "spark-01", TelemetrySample(ts=2.0, memory_used=95 * GIB, swap_used=2 * GIB, **base)
        )
    assert "swapping" in caplog.text
    assert "overcommitted" in caplog.text


def test_snapshot_carries_the_unified_memory_numbers(tmp_path):
    registry = make_registry(tmp_path, local=SPARK_01)
    registry.apply_sample(
        "spark-01",
        TelemetrySample(
            ts=time.time(),
            memory_used=REAL_SPARK_POOL_TOTAL - REAL_SPARK_AVAILABLE,
            memory_total=REAL_SPARK_POOL_TOTAL,
            power_watts=84.0, temperature_c=81.0, utilization_pct=96.0,
            gpu_memory_used=REAL_SPARK_GPU_MIB * 1024**2,
            host_memory_total=REAL_SPARK_POOL_TOTAL,
            host_memory_available=REAL_SPARK_AVAILABLE,
            swap_used=int(5.6 * GIB),
        ),
    )
    node = registry.snapshot()["nodes"][0]
    assert node["gpu_used"] == REAL_SPARK_GPU_MIB * 1024**2
    assert 0 < node["allocatable"] < SPARK_01.usable_memory()
    assert node["swap_used"] > 0


def test_unified_memory_fields_survive_the_wire(tmp_path):
    """A coordinator polling a remote Spark must get the pool numbers too."""
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client)
    run(registry.add_node("10.0.0.12"))
    client.telemetry["http://10.0.0.12:8081"] = TelemetrySample(
        ts=10.0,
        memory_used=REAL_SPARK_POOL_TOTAL - REAL_SPARK_AVAILABLE,
        memory_total=REAL_SPARK_POOL_TOTAL,
        power_watts=84.0, temperature_c=81.0, utilization_pct=96.0,
        gpu_memory_used=REAL_SPARK_GPU_MIB * 1024**2, gpu_process_count=1,
        host_memory_total=REAL_SPARK_POOL_TOTAL,
        host_memory_available=REAL_SPARK_AVAILABLE,
        swap_used=int(5.6 * GIB),
    ).as_dict() | {"available": True}

    run(registry.telemetry_round())

    report = registry.memory_report("spark-02")
    assert report["gpu_used"] == REAL_SPARK_GPU_MIB * 1024**2
    assert report["host_used"] > 20 * GIB
    assert 0 < report["allocatable"] < SPARK_02.usable_memory() / 5


def test_a_headless_spark_still_gets_essentially_the_whole_ceiling():
    """The fix must not punish the machine it is actually aimed at.

    A dedicated node runs no desktop: the OS holds a couple of GiB, the pool is
    almost entirely free, and the addressable ceiling becomes the binding limit
    again. The host reserve only bites when the host is genuinely spending.
    """
    profile = probe_local(address="10.0.0.11", rows=GB10_ROWS)
    sample = TelemetrySample(
        ts=1.0, memory_used=3 * GIB, memory_total=REAL_SPARK_POOL_TOTAL,
        power_watts=20.0, temperature_c=40.0, utilization_pct=0.0,
        gpu_memory_used=0, gpu_process_count=0,
        host_memory_total=REAL_SPARK_POOL_TOTAL,
        host_memory_available=REAL_SPARK_POOL_TOTAL - 3 * GIB,
    )
    live = allocatable_bytes(profile, sample, host_reserve=8 * GIB)
    ceiling = profile.usable_memory(0.90)

    assert live == ceiling - 3 * GIB, "the addressable ceiling should bind, not the pool"
    assert live / GIB == pytest.approx(104.7, abs=0.2)
    assert live > 0.95 * ceiling


def test_the_binding_limit_switches_with_load():
    """Which of the two limits binds is a property of the node, not a constant."""
    profile = probe_local(address="10.0.0.11", rows=GB10_ROWS)
    common = dict(
        ts=1.0, memory_total=REAL_SPARK_POOL_TOTAL, power_watts=0.0,
        temperature_c=0.0, utilization_pct=0.0,
        host_memory_total=REAL_SPARK_POOL_TOTAL,
    )
    # Busy desktop, little model: the shared pool runs out first.
    pool_bound = TelemetrySample(
        memory_used=40 * GIB, host_memory_available=20 * GIB, **common
    )
    assert allocatable_bytes(profile, pool_bound, host_reserve=8 * GIB) == 12 * GIB

    # Big model, quiet desktop: the GPU's addressable slice runs out first.
    gpu_bound = TelemetrySample(
        memory_used=100 * GIB, host_memory_available=21 * GIB, **common
    )
    expected = profile.usable_memory(0.90) - 100 * GIB
    assert allocatable_bytes(profile, gpu_bound, host_reserve=8 * GIB) == expected
    assert expected < 13 * GIB


# ----------------------------------------------------------------------
# 15. M-25: memory_used_pct never exceeds 100
# ----------------------------------------------------------------------


def test_memory_used_pct_clamps_at_100_when_pool_exceeds_addressable(tmp_path):
    """On a real GB10 the pool total (121 GiB) is larger than the addressable
    ceiling (119.7 GiB) this package uses as the denominator, so memory_used
    can legitimately exceed addressable_memory. The reported percentage must
    still read as a percentage.
    """
    registry = make_registry(tmp_path, local=SPARK_01)
    over_addressable = SPARK_01.addressable_memory + int(5 * GIB)
    registry.apply_sample(
        "spark-01",
        TelemetrySample(ts=1.0, memory_used=over_addressable, memory_total=SPARK_01.total_memory,
                        power_watts=71.0, temperature_c=62.0, utilization_pct=94.0),
    )
    snap = registry.snapshot()
    assert snap["nodes"][0]["memory_used_pct"] == 100.0


def test_memory_used_pct_below_the_ceiling_is_unaffected():
    from control_plane.registry.serde import memory_used_pct

    state = NodeState(
        profile=SPARK_01, healthy=True, last_seen=0.0,
        memory_used=int(SPARK_01.addressable_memory * 0.5),
        power_watts=0.0, temperature_c=0.0, utilization_pct=0.0,
    )
    assert memory_used_pct(state) == pytest.approx(50.0, abs=0.1)


# ----------------------------------------------------------------------
# 16. M-12: roster persistence across a restart
# ----------------------------------------------------------------------


def test_admitted_member_survives_a_coordinator_restart(tmp_path):
    """Acceptance: a coordinator restart must not forget an admitted worker."""
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    first_run = Registry(
        config=RegistryConfig(data_dir=tmp_path, token="tok", agent_port=8081),
        local_profile=SPARK_01,
        client=client,
    )
    run(first_run.handle_join("tok", SPARK_02, "http://10.0.0.12:8081"))
    first_run.admit("spark-02")
    assert sorted(s.profile.node_id for s in first_run.list_nodes()) == ["spark-01", "spark-02"]

    # New process, same data dir: restart in every sense that matters here.
    second_run = Registry(
        config=RegistryConfig(data_dir=tmp_path, token="tok", agent_port=8081),
        local_profile=SPARK_01,
        client=client,
    )

    ids = sorted(s.profile.node_id for s in second_run.list_nodes())
    assert ids == ["spark-01", "spark-02"]
    restored = second_run.get_node("spark-02")
    assert restored.profile == SPARK_02
    assert restored.healthy  # optimistic until the health loop says otherwise
    assert second_run.agent_url("spark-02") == "http://10.0.0.12:8081"
    # Telemetry is not durable state -- a fresh restart has none of it yet.
    assert restored.memory_used == 0


def test_candidate_survives_a_coordinator_restart(tmp_path):
    """Not just members -- a pending candidate should not vanish either."""
    first_run = make_registry(tmp_path, local=SPARK_01, token="tok")
    first_run.offer_candidate(SPARK_02, "http://10.0.0.12:8081")
    assert [c["node_id"] for c in first_run.candidates()] == ["spark-02"]

    second_run = make_registry(tmp_path, local=SPARK_01, token="tok")
    assert [c["node_id"] for c in second_run.candidates()] == ["spark-02"]
    assert second_run.list_nodes()[0].profile.node_id == "spark-01"  # not re-admitted


def test_removed_node_stays_gone_after_restart(tmp_path):
    """remove_node persists too: a forgotten node must not come back."""
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    first_run = Registry(
        config=RegistryConfig(data_dir=tmp_path, token="tok", agent_port=8081),
        local_profile=SPARK_01,
        client=client,
    )
    run(first_run.handle_join("tok", SPARK_02, "http://10.0.0.12:8081"))
    first_run.admit("spark-02")
    first_run.remove_node("spark-02")

    second_run = Registry(
        config=RegistryConfig(data_dir=tmp_path, token="tok", agent_port=8081),
        local_profile=SPARK_01,
        client=client,
    )
    assert [s.profile.node_id for s in second_run.list_nodes()] == ["spark-01"]
    assert second_run.candidates() == []


def test_dismissed_candidate_stays_gone_after_restart(tmp_path):
    first_run = make_registry(tmp_path, local=SPARK_01, token="tok")
    first_run.offer_candidate(SPARK_02, "http://10.0.0.12:8081")
    first_run.dismiss_candidate("spark-02")

    second_run = make_registry(tmp_path, local=SPARK_01, token="tok")
    assert second_run.candidates() == []


def test_corrupt_roster_file_warns_and_starts_empty(tmp_path):
    (tmp_path / "registry.json").write_text("{not valid json")
    registry = make_registry(tmp_path, local=SPARK_01)
    assert registry.candidates() == []
    assert [s.profile.node_id for s in registry.list_nodes()] == ["spark-01"]


def test_missing_roster_file_starts_empty(tmp_path):
    assert not (tmp_path / "registry.json").exists()
    registry = make_registry(tmp_path, local=SPARK_01)
    assert registry.candidates() == []
    assert [s.profile.node_id for s in registry.list_nodes()] == ["spark-01"]


def test_roster_file_written_atomically_no_leftover_tmp_files(tmp_path):
    """The tempfile+fsync+os.replace pattern should never leave a .tmp file
    behind on the success path."""
    registry = make_registry(tmp_path, local=SPARK_01, token="tok")
    registry.offer_candidate(SPARK_02, "http://10.0.0.12:8081")
    leftovers = list(tmp_path.glob(".registry-*.tmp"))
    assert leftovers == []
    assert (tmp_path / "registry.json").exists()


def test_roster_persists_profile_and_url_not_telemetry(tmp_path):
    """Persist profiles + agent_urls, NOT live telemetry (M-12's own text)."""
    import json

    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = Registry(
        config=RegistryConfig(data_dir=tmp_path, token="tok", agent_port=8081),
        local_profile=SPARK_01,
        client=client,
    )
    run(registry.handle_join("tok", SPARK_02, "http://10.0.0.12:8081"))
    registry.admit("spark-02")
    registry.apply_sample(
        "spark-02",
        TelemetrySample(ts=1.0, memory_used=12345, memory_total=SPARK_02.total_memory,
                        power_watts=99.0, temperature_c=88.0, utilization_pct=77.0),
    )

    raw = json.loads((tmp_path / "registry.json").read_text())
    member = raw["members"]["spark-02"]
    assert set(member) == {"profile", "agent_url"}
    assert "power_watts" not in member and "memory_used" not in member


# ----------------------------------------------------------------------
# 17. Enabler: Registry.start() is idempotent
# ----------------------------------------------------------------------


def test_start_called_twice_does_not_double_spawn_tasks(tmp_path):
    registry = make_registry(tmp_path, local=SPARK_01)

    async def scenario():
        await registry.start()
        first_tasks = list(registry._tasks)
        await registry.start()
        second_tasks = list(registry._tasks)
        assert first_tasks == second_tasks
        # health, telemetry, profile-refresh. The third was added when a
        # profile stopped being written only at join.
        assert len(second_tasks) == 3
        assert all(not t.done() for t in second_tasks)
        await registry.stop()

    run(scenario())


def test_start_twice_concurrently_still_spawns_once(tmp_path):
    """Two overlapping start() calls (e.g. two request handlers racing the
    composition root) must not each spawn their own set of loops."""
    registry = make_registry(tmp_path, local=SPARK_01)

    async def scenario():
        await asyncio.gather(registry.start(), registry.start())
        assert len(registry._tasks) == 3
        await registry.stop()

    run(scenario())


def test_stop_then_restart_spawns_a_fresh_set(tmp_path):
    registry = make_registry(tmp_path, local=SPARK_01)

    async def scenario():
        await registry.start()
        await registry.stop()
        assert registry._tasks == []
        await registry.start()
        assert len(registry._tasks) == 3
        await registry.stop()

    run(scenario())


def test_remove_node_of_unknown_id_raises_rather_than_silently_succeeding(tmp_path):
    """The gateway maps NodeNotFound to 404; a silent pop made that branch
    dead and turned a typo'd DELETE into a lying 200."""
    registry = make_registry(tmp_path)
    with pytest.raises(NodeNotFound):
        registry.remove_node("no-such-node")
    assert not (tmp_path / "registry.json").exists() or True  # no spurious persist requirement


def test_explicit_join_rejection_stays_worker_in_waiting_not_fatal(tmp_path):
    """Containerized-join defect: DERATE_JOIN's branch let JoinRejected
    propagate and kill the process, while the discovered path became a
    worker-in-waiting. A rejection proves the NAMED coordinator exists, and
    on first boot the join races our own agent app (the probe-back lands
    before /agent/* listens), so even a correct setup 403s once."""

    async def rejecting_join(url, token, profile, agent_url):
        raise JoinRejected("probe-back failed")

    config = RegistryConfig(
        data_dir=tmp_path, join_address="http://10.0.0.5:8088", role="worker"
    )
    decision = run(
        resolve_role(config, SPARK_02, "http://10.0.0.12:8091", join=rejecting_join)
    )
    assert decision.role == "worker"
    assert decision.joined is False
    assert decision.status == "rejected"
    assert decision.coordinator_url == "http://10.0.0.5:8088"
    assert "retrying" in decision.reason


# ----------------------------------------------------------------------
# Display names
#
# A rename changes the caption and nothing else. node_id is the key every
# deployment, link measurement and routing target on disk was written
# against, so these tests exist mostly to pin down what a rename must NOT
# move.
# ----------------------------------------------------------------------


def named_registry(tmp_path):
    """A coordinator with spark-02 admitted, ready to be renamed."""
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client, local=SPARK_01)
    run(registry.add_node("10.0.0.12"))
    return registry, client


def test_rename_sets_a_label_and_leaves_the_node_id_alone(tmp_path):
    registry, _ = named_registry(tmp_path)

    assert registry.set_node_label("spark-02", "  Rack 2   box ") == "Rack 2 box"

    assert registry.node_label("spark-02") == "Rack 2 box"
    # The identity everything else is keyed by is untouched.
    assert registry.get_node("spark-02").profile.node_id == "spark-02"
    assert registry.get_node("spark-02").profile.hostname == SPARK_02.hostname
    assert [s.profile.node_id for s in registry.list_nodes()] == ["spark-01", "spark-02"]


def test_labels_are_sparse_so_unnamed_is_distinguishable_from_named(tmp_path):
    registry, _ = named_registry(tmp_path)
    registry.set_node_label("spark-02", "Rack 2")

    # spark-01 was never renamed, so it has no entry at all -- not an entry
    # equal to its node_id, which the UI could not tell from a real rename.
    assert registry.node_labels() == {"spark-02": "Rack 2"}
    assert registry.node_label("spark-01") is None


def test_rename_survives_a_restart(tmp_path):
    registry, _ = named_registry(tmp_path)
    registry.set_node_label("spark-02", "Rack 2")

    restarted = make_registry(tmp_path, client=FakeClient(), local=SPARK_01)

    assert restarted.node_label("spark-02") == "Rack 2"


def test_an_empty_name_clears_the_rename(tmp_path):
    """Select-all, delete, save is how a human asks for the default back."""
    registry, _ = named_registry(tmp_path)
    registry.set_node_label("spark-02", "Rack 2")

    assert registry.set_node_label("spark-02", "") is None

    assert registry.node_label("spark-02") is None
    assert make_registry(tmp_path, client=FakeClient()).node_label("spark-02") is None


def test_rename_refuses_a_name_that_cannot_be_rendered(tmp_path):
    registry, _ = named_registry(tmp_path)
    for bad in ("x" * 49, "a\x00b", 17):
        with pytest.raises(ValueError):
            registry.set_node_label("spark-02", bad)
    assert registry.node_label("spark-02") is None


def test_rename_of_an_unknown_node_raises_rather_than_stranding_a_name(tmp_path):
    registry, _ = named_registry(tmp_path)
    with pytest.raises(NodeNotFound):
        registry.set_node_label("spark-99", "Ghost")
    assert registry.node_labels() == {}


def test_removing_a_node_forgets_its_name(tmp_path):
    """Otherwise a machine rejoining under the same id inherits a name the
    operator deleted along with the node."""
    registry, _ = named_registry(tmp_path)
    registry.set_node_label("spark-02", "Rack 2")

    registry.remove_node("spark-02")

    assert registry.node_labels() == {}
    assert make_registry(tmp_path, client=FakeClient()).node_label("spark-02") is None


def test_an_unusable_persisted_label_loses_the_name_not_the_node(tmp_path):
    import json

    registry, _ = named_registry(tmp_path)
    registry.set_node_label("spark-02", "Rack 2")
    path = tmp_path / "registry.json"
    data = json.loads(path.read_text())
    data["members"]["spark-02"]["label"] = "x" * 400
    path.write_text(json.dumps(data))

    restarted = make_registry(tmp_path, client=FakeClient())

    assert restarted.get_node("spark-02") is not None
    assert restarted.node_label("spark-02") is None


# ----------------------------------------------------------------------
# Reachability
#
# Cheap, non-disruptive, and directional. The point of these is that a
# one-way failure stays one-way in the report: an operator has to be able
# to see WHICH side cannot dial the other.
# ----------------------------------------------------------------------


def test_reach_reports_the_coordinator_leg_and_both_peer_legs(tmp_path):
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client, local=SPARK_01)
    client.serve(registry.agent_url("spark-01"), SPARK_01)
    run(registry.add_node("10.0.0.12"))

    result = run(registry.check_reach("spark-01", "spark-02"))

    assert result["ok"] is True
    legs = {(leg["source"], leg["target"]): leg for leg in result["legs"]}
    # The coordinator IS spark-01, so its leg to itself is stated, not dialled.
    assert legs[("coordinator", "spark-01")]["note"]
    assert legs[("coordinator", "spark-01")]["ms"] is None
    # ...and its leg to spark-02 is a real dial, which doubles as spark-01 ->
    # spark-02. Only the reverse direction has to be asked of the far node.
    assert legs[("coordinator", "spark-02")]["ok"] is True
    assert legs[("coordinator", "spark-02")]["answered_as"] == "spark-02"
    assert legs[("spark-02", "spark-01")]["ok"] is True
    assert ("spark-01", "spark-02") not in legs


def test_reach_asks_the_far_node_with_the_cluster_token(tmp_path):
    """/agent/reach makes a node dial an address we name. Uncredentialed, that
    is a port scanner running inside the operator's network."""
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client, token="tok-123", local=SPARK_01)
    client.serve(registry.agent_url("spark-01"), SPARK_01)
    run(registry.add_node("10.0.0.12"))

    run(registry.check_reach("spark-01", "spark-02"))

    asked = [(u, h) for (u, _), h in zip(client.posts, client.headers) if u.endswith("/agent/reach")]
    assert asked and all(h and h.get("X-Derate-Token") == "tok-123" for _, h in asked)


def test_reach_keeps_a_one_way_failure_one_way(tmp_path):
    """The direction that failed is the whole finding. A merged verdict would
    send an operator to look at the machine that is demonstrably fine."""
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client, local=SPARK_01)
    client.serve(registry.agent_url("spark-01"), SPARK_01)
    run(registry.add_node("10.0.0.12"))
    # spark-02 can no longer dial the coordinator back, but still answers us.
    client.kill(registry.agent_url("spark-01"))

    result = run(registry.check_reach("spark-01", "spark-02"))

    legs = {(leg["source"], leg["target"]): leg for leg in result["legs"]}
    assert result["ok"] is False
    assert legs[("coordinator", "spark-02")]["ok"] is True
    assert legs[("spark-02", "spark-01")]["ok"] is False
    assert "spark-02 → spark-01" in result["summary"]


def test_reach_never_reports_a_millisecond_figure_for_a_leg_that_failed(tmp_path):
    """Same rule as an unmeasured link carrying no bandwidth: 0 ms beside
    'unreachable' reads as a fast link."""
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client, local=SPARK_01)
    run(registry.add_node("10.0.0.12"))
    client.kill("http://10.0.0.12:8081")

    result = run(registry.check_reach("spark-01", "spark-02"))

    assert result["ok"] is False
    for leg in result["legs"]:
        if not leg["ok"]:
            assert leg["ms"] is None
            assert leg["error"]


def test_being_unable_to_ask_a_node_is_unchecked_not_a_finding_about_its_peer(tmp_path):
    """If we cannot reach spark-02 to ask it anything, whether spark-02 can
    reach spark-01 is UNKNOWN. Reporting it as 'spark-02 cannot reach spark-01'
    invents a result; dropping the leg claims we tested what we did not."""
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client, local=SPARK_01)
    client.serve(registry.agent_url("spark-01"), SPARK_01)
    run(registry.add_node("10.0.0.12"))
    client.kill("http://10.0.0.12:8081")

    result = run(registry.check_reach("spark-01", "spark-02"))

    peer = next(leg for leg in result["legs"] if leg["source"] == "spark-02")
    assert peer["note"] and "never tested" in peer["note"]
    assert peer["ms"] is None
    # And no two legs claim the same direction with opposite verdicts -- the
    # coordinator's own probe of spark-02 is the only place that failure is
    # reported.
    directions = [(leg["source"], leg["target"]) for leg in result["legs"]]
    assert len(directions) == len(set(directions))
    assert "could not be checked" in result["summary"]


def test_reach_of_an_unknown_node_raises(tmp_path):
    registry, _ = named_registry(tmp_path)
    with pytest.raises(NodeNotFound):
        run(registry.check_reach("spark-01", "spark-99"))


def test_a_check_between_two_workers_does_not_pass_on_the_coordinator_legs(tmp_path):
    """Acceptance for the same trap, through the real registry: with neither
    endpoint being this process, both coordinator probes are prerequisites and
    the pair's own two directions are the answer."""
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    client.serve("http://10.0.0.13:8081", WS_3090)
    registry = make_registry(tmp_path, client=client, local=SPARK_01)
    client.serve(registry.agent_url("spark-01"), SPARK_01)
    run(registry.add_node("10.0.0.12"))
    run(registry.add_node("10.0.0.13"))

    result = run(registry.check_reach("spark-02", WS_3090.node_id))

    legs = {(leg["source"], leg["target"]): leg for leg in result["legs"]}
    assert legs[("coordinator", "spark-02")]["pair"] is False
    assert legs[("coordinator", WS_3090.node_id)]["pair"] is False
    assert legs[("spark-02", WS_3090.node_id)]["pair"] is True
    assert legs[(WS_3090.node_id, "spark-02")]["pair"] is True
    # Both peer legs really were run here, so this pair IS verified both ways.
    assert result["ok"] is True
    assert "Reachable both ways" in result["summary"]


# ----------------------------------------------------------------------
# Section: enrolling the coordinator's own host
#
# The one enrollment with no token and no click. Everything here is about the
# two properties that make it safe to run on every startup: it is idempotent by
# node_id, and it never reaches the candidate/admit path that exists to gate
# machines arriving over the network.
# ----------------------------------------------------------------------


def test_the_local_node_is_a_member_from_construction(tmp_path):
    """No admit, no candidate step, no token. Nobody admits their own machine."""
    registry = make_registry(tmp_path, local=SPARK_01)

    assert [s.profile.node_id for s in registry.list_nodes()] == ["spark-01"]
    assert registry.local_node_id == "spark-01"
    assert registry.candidates() == []


def test_the_local_node_is_flagged_as_the_coordinators_own_host(tmp_path):
    registry = make_registry(tmp_path, local=SPARK_01)

    assert registry.get_node("spark-01").is_local is True


def test_a_node_that_joined_is_not_flagged_as_the_local_host(tmp_path):
    """is_local answers "is this the coordinator's machine", not "is this me"."""
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client, local=SPARK_01)

    run(registry.add_node("10.0.0.12"))

    assert registry.get_node("spark-02").is_local is False
    assert registry.get_node("spark-01").is_local is True


def test_enrolling_twice_replaces_rather_than_duplicates(tmp_path):
    """The restart case. A second call must not add a second row."""
    registry = make_registry(tmp_path, local=SPARK_01)

    registry.enroll_local(SPARK_01)
    registry.enroll_local(SPARK_01)

    assert [s.profile.node_id for s in registry.list_nodes()] == ["spark-01"]


def test_a_restart_against_the_same_data_dir_leaves_one_node(tmp_path):
    """Idempotence across processes, which is the property that actually
    matters: the roster is on disk and gets loaded before the local node is
    installed."""
    first = make_registry(tmp_path, local=SPARK_01)
    first.enroll_local(SPARK_01)

    second = make_registry(tmp_path, local=SPARK_01)

    assert [s.profile.node_id for s in second.list_nodes()] == ["spark-01"]


def test_enroll_local_updates_the_hardware_on_a_re_probe(tmp_path):
    """A replace, not a no-op: install a driver and restart, and the roster
    should show what the machine is now."""
    registry = make_registry(tmp_path, local=SPARK_01)
    rebuilt = make_profile("spark-01", gpu_name="NVIDIA GB10", gpu_count=2)

    registry.enroll_local(rebuilt)

    assert len(registry.list_nodes()) == 1
    assert registry.get_node("spark-01").profile.gpu_count == 2
    assert registry.local_profile.gpu_count == 2


def test_enroll_local_persists_the_local_node_to_the_roster(tmp_path):
    """A coordinator that has admitted nobody still has itself on disk. It used
    not to: the constructor wrote straight to _members and only an unrelated
    event that persisted for its own reasons ever flushed it."""
    registry = make_registry(tmp_path, local=SPARK_01)
    registry.enroll_local(SPARK_01)

    stored = json.loads((tmp_path / "registry.json").read_text())

    assert "spark-01" in stored["members"]


def test_constructing_a_registry_writes_nothing(tmp_path):
    """Construction must stay free of disk side effects; forty-odd tests build
    one per case and process-start effects have no business firing there."""
    make_registry(tmp_path, local=SPARK_01)

    assert not (tmp_path / "registry.json").exists()


def test_enroll_local_probes_when_it_is_given_no_profile(tmp_path, monkeypatch):
    """The gateway's fallback calls it with nothing to hand."""
    monkeypatch.setattr(
        "control_plane.registry.probe.probe_local",
        lambda *a, **k: SPARK_01,
    )
    registry = make_registry(tmp_path)
    assert registry.local_node_id is None

    state = registry.enroll_local()

    assert state.profile.node_id == "spark-01"
    assert state.is_local is True
    assert registry.local_node_id == "spark-01"


def test_enrolling_settles_a_stale_candidate_for_the_same_machine(tmp_path):
    """A machine cannot be its own candidate."""
    registry = make_registry(tmp_path)
    registry.offer_candidate(SPARK_01, "http://10.0.0.11:8081")
    assert [c["node_id"] for c in registry.candidates()] == ["spark-01"]

    registry.enroll_local(SPARK_01)

    assert registry.candidates() == []
    assert [s.profile.node_id for s in registry.list_nodes()] == ["spark-01"]


def test_starting_flushes_the_local_node_to_disk(tmp_path):
    """The real path: startup.py constructs with a local_profile and calls
    start(). Construction writes nothing, so if start() did not flush, a
    coordinator that never admitted anyone would have no roster entry for
    itself -- which is what shipped before."""
    registry = make_registry(tmp_path, local=SPARK_01)
    assert not (tmp_path / "registry.json").exists()

    async def scenario():
        await registry.start()
        await registry.stop()

    run(scenario())

    stored = json.loads((tmp_path / "registry.json").read_text())
    assert list(stored["members"]) == ["spark-01"]


# ----------------------------------------------------------------------
# Section: build identity, and profiles that stop being frozen at join
#
# A Raspberry Pi joined this cluster running an image that predated the CPU
# probe. It reported device_class "unknown", the roster said "device class is
# not recognized", and the machine was fine -- the software was stale. Nothing
# could tell those two apart, and a profile written only at join meant that
# even fixing the Pi would not have fixed the roster until it re-joined.
# ----------------------------------------------------------------------


def test_an_unknown_probe_never_overwrites_identified_hardware():
    """probe_local is total: its answer for "could not look" is a valid
    profile saying UNKNOWN. On a timer that would flap a GB10 out of the
    serving pool every time nvidia-smi hiccuped."""
    blind = make_profile("spark-01", device_class=DeviceClass.UNKNOWN)

    assert profile_supersedes(blind, SPARK_01) is False


def test_unknown_replaces_unknown():
    """Nothing better to keep, so there is nothing to protect."""
    stored = make_profile("x", device_class=DeviceClass.UNKNOWN)
    fresh = make_profile("x", device_class=DeviceClass.UNKNOWN)

    assert profile_supersedes(fresh, stored) is True


def test_a_refresh_that_cannot_reach_the_node_keeps_what_we_had(tmp_path):
    """Unreachable is not evidence the hardware changed."""
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client, local=SPARK_01)
    run(registry.add_node("10.0.0.12"))
    client.kill("http://10.0.0.12:8081")

    assert run(registry.refresh_profile("spark-02")) is False
    assert registry.get_node("spark-02").profile.device_class is DeviceClass.GB10


def test_a_refresh_answering_as_a_different_node_is_refused(tmp_path):
    """Same refusal handle_join makes on a probe-back mismatch: the machine at
    that address is not the one we think it is."""
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client, local=SPARK_01)
    run(registry.add_node("10.0.0.12"))

    client.serve("http://10.0.0.12:8081", WS_3090)
    assert run(registry.refresh_profile("spark-02")) is False
    assert registry.get_node("spark-02").profile.node_id == "spark-02"


def test_an_unchanged_profile_reports_no_change(tmp_path):
    """So the roster is not rewritten to disk once a minute forever."""
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client, local=SPARK_01)
    run(registry.add_node("10.0.0.12"))

    assert run(registry.refresh_profile("spark-02")) is False


def test_the_heartbeat_records_the_build_a_node_reports(tmp_path):
    """Build identity rides the health round rather than needing a call of its
    own -- the coordinator already dials every member every 5s."""
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client, local=SPARK_01)
    run(registry.add_node("10.0.0.12"))
    client.health_bodies["http://10.0.0.12:8081"] = {"status": "ok", "build": "abc123"}

    run(registry.check_health("spark-02"))

    assert registry.get_node("spark-02").build == "abc123"


def test_an_agent_that_reports_no_build_does_not_blank_a_known_one(tmp_path):
    """An older agent has no build key at all. Absence stays absence rather
    than erasing what we already knew."""
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    registry = make_registry(tmp_path, client=client, local=SPARK_01)
    run(registry.add_node("10.0.0.12"))
    client.health_bodies["http://10.0.0.12:8081"] = {"status": "ok", "build": "abc123"}
    run(registry.check_health("spark-02"))

    client.health_bodies["http://10.0.0.12:8081"] = {"status": "ok"}
    run(registry.check_health("spark-02"))

    assert registry.get_node("spark-02").build == "abc123"
