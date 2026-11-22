"""Fabric port settings: the agent's reader, the pair rules, and both routes.

The reader is driven through the CommandRunner seam with the exact text a
ConnectX-7 rail printed on spark-26af (ring 1024 of 8192, rx_cqe_compress off);
the rules are checked one at a time -- each fires on its condition and stays
silent otherwise, because a settings panel that always says something is one
nobody reads.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from control_plane.gateway import create_app
from control_plane.gateway.settings import GatewaySettings
from control_plane.links import nichealth
from control_plane.links.nichealth import (
    RailHealth,
    compare_health,
    link_health_report,
    pair_rails,
    read_nic_health,
)
from control_plane.links.runner import CommandResult
from control_plane.registry.agent import NodeAgent, create_agent_app
from tests.unit.test_agent_tokens import profile
from tests.unit.test_gateway import FakeRegistry, build_deps
from tests.unit.test_links import FakeRunner

REPO = Path(__file__).resolve().parents[2]

RINGS = """Ring parameters for enp1s0f0np0:
Pre-set maximums:
RX:\t\t\t8192
RX Mini:\t\tn/a
RX Jumbo:\t\tn/a
TX:\t\t\t8192
TX push buff len:\tn/a
Current hardware settings:
RX:\t\t\t1024
RX Mini:\t\tn/a
RX Jumbo:\t\tn/a
TX:\t\t\t1024
RX Buf Len:\t\tn/a
CQE Size:\t\tn/a
"""
FLAGS = """Private flags for enp1s0f0np0:
rx_cqe_moder       : off
tx_cqe_moder       : off
rx_cqe_compress    : off
rx_striding_rq     : on
"""
STATS = """NIC statistics:
     rx_packets: 123
     rx_out_of_buffer: 77453
     rx_crc_errors_phy: 0
     rx_discards_phy: 0
     link_down_events_phy: 1
     rx_out_of_buffer_extra: 9
"""
IP = (
    "3: enp1s0f0np0    inet 10.100.152.1/24 brd 10.100.152.255 scope global "
    "noprefixroute enp1s0f0np0\\       valid_lft forever preferred_lft forever\n"
)
IP6 = (
    "3: enp1s0f0np0    inet6 fe80::4ebb:47ff:fe2b:4d39/64 scope link \\       "
    "valid_lft forever preferred_lft forever\n"
)


class ArgvRunner(FakeRunner):
    """FakeRunner keyed by whole argv: three ethtool calls share every token
    but the flag, and the base class picks an answer per token."""

    def __init__(self, *, by_argv: dict[tuple[str, ...], str], failing=(), **kw) -> None:
        super().__init__(**kw)
        self._by_argv = by_argv
        self._failing = set(failing)

    def run(self, argv, timeout=60.0, env=None):
        self.calls.append(tuple(argv))
        key = tuple(argv)
        if key in self._failing:
            return CommandResult(key, 1, "", "Operation not supported", 0.01)
        if key not in self._by_argv:
            return CommandResult(key, 127, "", "not found", 0.01)
        return CommandResult(key, 0, self._by_argv[key], "", 0.01)


def _sysfs(iface: str, dev: str, *, carrier="1", speed="200000", mtu="1500") -> dict[str, str]:
    base = f"/sys/class/net/{iface}"
    return {
        f"{base}/device/infiniband/{dev}/ports/1/state": "4: ACTIVE",
        f"{base}/carrier": carrier + "\n",
        f"{base}/speed": speed + "\n",
        f"{base}/mtu": mtu + "\n",
    }


def _rail_runner(**kw) -> ArgvRunner:
    files = {
        **_sysfs("enp1s0f0np0", "rocep1s0f0"),
        # The management NIC: no RDMA device, so it is not a rail at all.
        "/sys/class/net/enP7s7/carrier": "1",
        "/sys/class/net/enP7s7/mtu": "1500",
    }
    return ArgvRunner(
        files=files,
        by_argv={
            ("ip", "-o", "-4", "addr", "show", "dev", "enp1s0f0np0"): IP,
            ("ip", "-o", "-6", "addr", "show", "dev", "enp1s0f0np0", "scope", "link"): IP6,
            ("ethtool", "-g", "enp1s0f0np0"): RINGS,
            ("ethtool", "--show-priv-flags", "enp1s0f0np0"): FLAGS,
            ("ethtool", "-S", "enp1s0f0np0"): STATS,
        },
        **kw,
    )


# ---------------------------------------------------------------------- reader


def test_reader_reads_an_mlx5_rail_completely():
    out = read_nic_health(_rail_runner(binaries=("ethtool",)))
    assert out["ethtool"] is True
    assert [r["iface"] for r in out["interfaces"]] == ["enp1s0f0np0"]
    rail = out["interfaces"][0]
    assert rail["rdma_device"] == "rocep1s0f0"
    assert rail["carrier"] is True
    assert rail["speed_mbps"] == 200000
    assert rail["mtu"] == 1500
    assert (rail["ring_rx"], rail["ring_tx"], rail["ring_rx_max"], rail["ring_tx_max"]) == (
        1024, 1024, 8192, 8192,
    )
    assert rail["rx_cqe_compress"] is False
    # Exactly the four named counters; a name that merely starts like one is not one.
    assert rail["counters"] == {
        "rx_out_of_buffer": 77453,
        "rx_crc_errors_phy": 0,
        "rx_discards_phy": 0,
        "link_down_events_phy": 1,
    }
    assert rail["ipv4"] == ["10.100.152.1/24"]
    assert rail["ipv6_link_local"] == ["fe80::4ebb:47ff:fe2b:4d39"]
    assert rail["unread"] == []


def test_reader_without_ethtool_leaves_its_fields_null_and_says_why():
    runner = _rail_runner(binaries=())
    out = read_nic_health(runner)
    rail = out["interfaces"][0]
    assert out["ethtool"] is False
    assert rail["ring_rx"] is None and rail["ring_rx_max"] is None
    assert rail["rx_cqe_compress"] is None
    assert rail["counters"] == {}
    # The sysfs and ip halves still arrive.
    assert rail["mtu"] == 1500 and rail["ipv4"] == ["10.100.152.1/24"]
    assert any("ethtool is not installed" in s for s in rail["unread"])
    assert not any(c[0] == "ethtool" for c in runner.calls)


def test_reader_survives_one_ethtool_query_failing():
    runner = _rail_runner(
        binaries=("ethtool",), failing=[("ethtool", "-g", "enp1s0f0np0")]
    )
    rail = read_nic_health(runner)["interfaces"][0]
    assert rail["ring_rx"] is None
    assert rail["rx_cqe_compress"] is False  # the other two still read
    assert rail["counters"]["rx_out_of_buffer"] == 77453
    assert any("ethtool -g enp1s0f0np0" in s for s in rail["unread"])


def test_a_port_with_no_link_reports_no_speed_and_no_carrier():
    runner = ArgvRunner(files=_sysfs("enp1s0f1np1", "rocep1s0f1", carrier="0", speed="-1"), by_argv={})
    rail = read_nic_health(runner)["interfaces"][0]
    assert rail["carrier"] is False and rail["speed_mbps"] is None


def test_rail_json_round_trips_and_rejects_junk():
    r = RailHealth("a", "d", True, 200000, 9000, 1, 2, 3, 4, True, {"rx_discards_phy": 5}, ("10.0.0.1/24",))
    assert RailHealth.from_json(r.to_json()) == r
    junk = RailHealth.from_json({"iface": "x", "mtu": "9000", "carrier": 1, "counters": {"a": "1"}})
    assert junk.mtu is None and junk.carrier is False and junk.counters == {}


# ----------------------------------------------------------------------- rules


def rail(iface="enp1s0f0np0", *, mtu=9000, ipv4=("10.100.152.1/24",), carrier=True, ring=(8192, 8192),
         cqe=True, counters=None) -> RailHealth:
    return RailHealth(
        iface=iface, rdma_device="d", carrier=carrier, speed_mbps=200000, mtu=mtu,
        ring_rx=ring[0], ring_tx=ring[1], ring_rx_max=8192, ring_tx_max=8192,
        rx_cqe_compress=cqe, counters=counters or {"rx_out_of_buffer": 0}, ipv4=tuple(ipv4),
    )


def _clean_pair():
    a = [rail(ipv4=("10.100.152.2/24",)), rail("enP2p1s0f0np0", ipv4=("10.100.153.2/24",))]
    b = [rail(ipv4=("10.100.152.1/24",)), rail("enP2p1s0f0np0", ipv4=("10.100.153.1/24",))]
    return a, b


def test_a_well_set_pair_has_no_findings():
    a, b = _clean_pair()
    assert compare_health("x", a, "y", b) == []


def test_mtu_mismatch_names_the_lower_end_and_the_stanza_raises_it():
    a, b = _clean_pair()
    b[0] = rail(mtu=1500, ipv4=("10.100.152.1/24",))
    (f,) = compare_health("x", a, "y", b)
    assert (f.severity, f.node_id, f.iface) == ("warning", "y", "enp1s0f0np0")
    assert "MTU 1500" in f.sentence and "9000" in f.sentence
    assert "    enp1s0f0np0:\n      mtu: 9000" in f.fix
    assert "netplan try" in f.fix


def test_equal_low_mtu_on_both_ends_is_not_a_mismatch():
    a, b = _clean_pair()
    a[0] = rail(mtu=1500, ipv4=("10.100.152.2/24",))
    b[0] = rail(mtu=1500, ipv4=("10.100.152.1/24",))
    assert compare_health("x", a, "y", b) == []


def test_ring_below_max_fires_with_the_command_and_a_udev_line():
    a, b = _clean_pair()
    a[1] = rail("enP2p1s0f0np0", ipv4=("10.100.153.2/24",), ring=(8192, 1024))
    (f,) = compare_health("x", a, "y", b)
    assert (f.severity, f.node_id, f.iface) == ("info", "x", "enP2p1s0f0np0")
    assert f.fix.splitlines()[0] == "sudo ethtool -G enP2p1s0f0np0 rx 8192 tx 8192"
    assert 'KERNEL=="enP2p1s0f0np0"' in f.fix and "/etc/udev/rules.d/" in f.fix


def test_ring_unknown_is_not_a_finding():
    a, b = _clean_pair()
    a[0] = RailHealth("enp1s0f0np0", "d", True, 200000, 9000, ipv4=("10.100.152.2/24",))
    assert compare_health("x", a, "y", b) == []


def test_cqe_compress_off_fires_and_unknown_does_not():
    a, b = _clean_pair()
    b[0] = rail(ipv4=("10.100.152.1/24",), cqe=False)
    (f,) = compare_health("x", a, "y", b)
    assert (f.node_id, f.iface, f.severity) == ("y", "enp1s0f0np0", "info")
    assert f.fix.splitlines()[0] == "sudo ethtool --set-priv-flags enp1s0f0np0 rx_cqe_compress on"
    b[0] = rail(ipv4=("10.100.152.1/24",), cqe=None)
    assert compare_health("x", a, "y", b) == []


@pytest.mark.parametrize("counter", ["rx_out_of_buffer", "rx_discards_phy", "rx_crc_errors_phy"])
def test_each_drop_counter_fires_when_nonzero_and_says_since_boot(counter):
    a, b = _clean_pair()
    b[1] = rail("enP2p1s0f0np0", ipv4=("10.100.153.1/24",), counters={counter: 53099})
    (f,) = compare_health("x", a, "y", b)
    assert (f.severity, f.node_id, f.iface) == ("warning", "y", "enP2p1s0f0np0")
    assert counter in f.sentence and "53,099" in f.sentence and "since boot" in f.sentence
    assert counter in f.fix


def test_link_down_events_are_reported_not_judged():
    a, b = _clean_pair()
    b[0] = rail(ipv4=("10.100.152.1/24",), counters={"link_down_events_phy": 3})
    assert compare_health("x", a, "y", b) == []


def test_no_shared_subnet_suggests_addresses_in_the_peers_subnets():
    # Today's real addressing: spark-26af 10.100.0.2/30 and NetworkManager's
    # 169.254 on the second rail; spark-4d38 a /24 on each.
    a = [rail(ipv4=("10.100.0.2/30",)), rail("enP2p1s0f0np0", ipv4=("169.254.212.153/16",))]
    b = [rail(ipv4=("10.100.152.1/24",)), rail("enP2p1s0f0np0", ipv4=("10.100.153.1/24",))]
    (f,) = compare_health("spark-26af", a, "spark-4d38", b)
    assert (f.severity, f.node_id) == ("warning", "spark-26af")
    assert "IPv6 link-local" in f.sentence and "IPv4" in f.sentence
    assert "addresses: [10.100.152.2/24]" in f.fix
    assert "addresses: [10.100.153.2/24]" in f.fix
    assert "never a second file" in f.fix


def test_one_unaddressed_rail_of_two_says_how_many_rails_carry_ipv4():
    a, b = _clean_pair()
    a[1] = rail("enP2p1s0f0np0", ipv4=())
    (f,) = compare_health("x", a, "y", b)
    assert f.node_id == "x" and f.iface == "enP2p1s0f0np0"
    assert "1 of 2 rails" in f.sentence
    assert "addresses: [10.100.153.2/24]" in f.fix


def test_neither_end_addressed_gives_each_end_its_half_of_a_fresh_subnet():
    a = [rail(ipv4=())]
    b = [rail(ipv4=())]
    findings = compare_health("x", a, "y", b)
    fixes = {f.node_id: f.fix for f in findings}
    assert "addresses: [10.100.152.1/24]" in fixes["x"]
    assert "addresses: [10.100.152.2/24]" in fixes["y"]


def test_both_ends_link_local_ipv4_counts_as_shared():
    a = [rail(ipv4=("169.254.1.1/16",))]
    b = [rail(ipv4=("169.254.9.9/16",))]
    assert compare_health("x", a, "y", b) == []


def test_ports_without_carrier_are_never_judged():
    a, b = _clean_pair()
    a.append(rail("enp1s0f1np1", carrier=False, mtu=1500, ipv4=(), cqe=False, ring=(1024, 1024)))
    assert compare_health("x", a, "y", b) == []
    assert compare_health("x", [rail(carrier=False)], "y", b) == []


def test_rails_pair_by_subnet_before_name():
    # Crossed cabling: x's enp1 sits in y's enP2 subnet.
    a = [rail("enp1s0f0np0", ipv4=("10.100.153.2/24",)), rail("enP2p1s0f0np0", ipv4=("10.100.152.2/24",))]
    b = [rail("enp1s0f0np0", ipv4=("10.100.152.1/24",)), rail("enP2p1s0f0np0", ipv4=("10.100.153.1/24",))]
    pairs = {(x.iface, y.iface) for x, y in pair_rails(a, b)}
    assert pairs == {("enp1s0f0np0", "enP2p1s0f0np0"), ("enP2p1s0f0np0", "enp1s0f0np0")}


def test_report_skips_pairs_without_a_fabric_and_lists_unreadable_nodes():
    a, b = _clean_pair()
    b[0] = rail(mtu=1500, ipv4=("10.100.152.1/24",))
    health = {
        "x": {"interfaces": [r.to_json() for r in a]},
        "y": {"interfaces": [r.to_json() for r in b]},
        "gpu-box": {"interfaces": []},  # no RDMA port: no fabric to judge
    }
    out = link_health_report(
        [("x", "y"), ("x", "gpu-box"), ("x", "old")], health, {"old": "too old"}, 12.0
    )
    assert [(link["a"], link["b"]) for link in out["links"]] == [("x", "y")]
    assert [f["node_id"] for f in out["links"][0]["findings"]] == ["y"]
    assert out["unreadable"] == [{"node_id": "old", "reason": "too old"}]
    assert out["measured_at"] == 12.0


# ---------------------------------------------------------------- agent route


def test_agent_route_is_token_gated(monkeypatch):
    monkeypatch.setattr(nichealth, "read_nic_health", lambda: {"interfaces": [], "ethtool": True})
    agent = NodeAgent(profile(), token="tok")
    http = TestClient(create_agent_app(agent))
    assert http.get("/agent/links/health").status_code == 403
    assert http.get("/agent/links/health", headers={"X-Derate-Token": "wrong"}).status_code == 403
    ok = http.get("/agent/links/health", headers={"X-Derate-Token": "tok"})
    assert ok.status_code == 200 and ok.json() == {"interfaces": [], "ethtool": True}


# -------------------------------------------------------------- gateway route


class AgentRegistry(FakeRegistry):
    def agent_url(self, node_id):
        return f"http://{node_id}.agent:18081"

    def agent_token(self, node_id):
        return f"tok-{node_id}"


def _ok_probe(request: httpx.Request) -> httpx.Response:
    """An agent whose every rail connects: one ok row per target it was sent."""
    import json

    targets = json.loads(request.content)["targets"]
    return httpx.Response(200, json={"results": [
        {"iface": t["iface"], "target": t["address"], "ok": True, "reached": True, "ms": 0.05, "error": None}
        for t in targets
    ]})


def _gateway(monkeypatch, answers, *, probes=None, coordinator="coord-elsewhere"):
    """A coordinator whose agents answer from *answers* (node_id -> response).

    *probes* maps node_id to its `/agent/links/probe` answer (an int status,
    or a callable on the request); a node not named connects on every rail.
    *coordinator* is the gateway's own node id: a pair that includes it is
    probed in-process, through `nichealth.probe_rails`, which tests replace.
    """
    seen = []
    probes = probes or {}

    def handler(request: httpx.Request) -> httpx.Response:
        node = request.url.host.removesuffix(".agent")
        seen.append((node, request.url.path, request.headers.get("x-derate-token")))
        if request.url.path == "/agent/links/probe":
            answer = probes.get(node, _ok_probe)
            if isinstance(answer, int):
                return httpx.Response(answer, json={"detail": "no"})
            return answer(request)
        answer = answers.get(node)
        if isinstance(answer, int):
            return httpx.Response(answer, json={"detail": "no"})
        return httpx.Response(200, json=answer)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    settings = GatewaySettings(coordinator_node_id=coordinator)
    return TestClient(create_app(build_deps(registry=AgentRegistry(), settings=settings))), seen


def test_gateway_compares_both_agents_for_one_pair(monkeypatch):
    a, b = _clean_pair()
    b[0] = rail(mtu=1500, ipv4=("10.100.152.1/24",))
    client, seen = _gateway(
        monkeypatch,
        {
            "spark-01": {"interfaces": [r.to_json() for r in a]},
            "spark-02": {"interfaces": [r.to_json() for r in b]},
        },
    )
    with client:
        res = client.get("/api/links/health", params={"a": "spark-01", "b": "spark-02"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert [(link["a"], link["b"]) for link in body["links"]] == [("spark-01", "spark-02")]
    (f,) = body["links"][0]["findings"]
    assert f["node_id"] == "spark-02" and f["iface"] == "enp1s0f0np0" and "mtu: 9000" in f["fix"]
    assert set(f) == {"severity", "node_id", "iface", "sentence", "fix"}
    # Each agent is asked with its own credential, and only the two named;
    # the coordinator is neither, so the first end probes through its agent.
    assert sorted(seen) == [
        ("spark-01", "/agent/links/health", "tok-spark-01"),
        ("spark-01", "/agent/links/probe", "tok-spark-01"),
        ("spark-02", "/agent/links/health", "tok-spark-02"),
    ]
    assert {(r["node_id"], r["iface"], r["ok"]) for r in body["links"][0]["reach"]} == {
        ("spark-01", "enp1s0f0np0", True), ("spark-01", "enP2p1s0f0np0", True),
    }


def test_gateway_names_an_agent_too_old_to_answer(monkeypatch):
    a, b = _clean_pair()
    client, _ = _gateway(
        monkeypatch,
        {"spark-01": {"interfaces": [r.to_json() for r in a]}, "spark-02": {"interfaces": [r.to_json() for r in b]},
         "ws-3090": 404},
    )
    with client:
        body = client.get("/api/links/health").json()
    assert [(link["a"], link["b"]) for link in body["links"]] == [("spark-01", "spark-02")]
    assert body["links"][0]["findings"] == []
    (row,) = body["unreadable"]
    assert row["node_id"] == "ws-3090" and "Upgrade that node" in row["reason"]


def test_gateway_refuses_half_a_pair(monkeypatch):
    client, seen = _gateway(monkeypatch, {})
    with client:
        assert client.get("/api/links/health", params={"a": "spark-01"}).status_code == 400
        assert client.get("/api/links/health", params={"a": "spark-01", "b": "spark-01"}).status_code == 400
        unknown = client.get("/api/links/health", params={"a": "spark-01", "b": "nope"}).json()
    assert unknown["links"] == [] and {r["node_id"] for r in unknown["unreadable"]} == {"nope"}
    assert seen == []


def test_the_node_image_installs_ethtool():
    lines = {
        line.strip().rstrip("\\").strip()
        for line in (REPO / "Dockerfile").read_text().splitlines()
        if not line.lstrip().startswith("#")
    }
    assert "ethtool" in lines


# ----------------------------------------------------------------------- reach

LL_A = ("fe80::4ebb:47ff:fe80:26b0",)
LL_B = ("fe80::4ebb:47ff:fe2b:4d39",)


def _ll(r: RailHealth, ll: tuple[str, ...]) -> RailHealth:
    from dataclasses import replace

    return replace(r, ipv6_link_local=ll)


def test_plan_dials_ipv4_only_in_a_shared_subnet_and_link_local_scoped_to_the_local_rail():
    # The measured spark-26af state: a stale /30 that shares nothing with the peer.
    a = [_ll(rail(ipv4=("10.100.0.2/30",)), LL_A), rail("enP2p1s0f0np0", ipv4=("10.100.153.2/24",))]
    b = [_ll(rail(ipv4=("10.100.152.1/24",)), LL_B), rail("enP2p1s0f0np0", ipv4=("10.100.153.1/24",))]
    plan = nichealth.probe_plan(a, b)
    assert [(t.iface, t.address, t.port) for t in plan] == [
        ("enP2p1s0f0np0", "10.100.153.1", 47101),
        ("enp1s0f0np0", "fe80::4ebb:47ff:fe2b:4d39%enp1s0f0np0", 47101),
    ]


def test_plan_skips_ports_without_carrier_and_is_capped():
    a = [rail(ipv4=("10.0.0.1/8",))]
    b = [rail(ipv4=tuple(f"10.0.0.{i}/8" for i in range(2, 40)))]
    assert len(nichealth.probe_plan(a, b)) == nichealth.MAX_PROBE_TARGETS
    assert nichealth.probe_plan(a, [rail(ipv4=("10.0.0.2/8",), carrier=False)]) == []


@pytest.mark.parametrize(
    "target, why",
    [
        ({"iface": "enp1s0f0np0", "address": "8.8.8.8", "port": 47101}, "not a private"),
        ({"iface": "enp1s0f0np0", "address": "127.0.0.1", "port": 47101}, "not a private"),
        ({"iface": "enp1s0f0np0", "address": "2606:4700::1111", "port": 47101}, "link-local"),
        ({"iface": "enp1s0f0np0", "address": "fe80::1%eth0", "port": 47101}, "link-local"),
        ({"iface": "enp1s0f0np0", "address": "10.100.152.1", "port": 22}, "47101"),
        ({"iface": "enP7s7", "address": "10.100.152.1", "port": 47101}, "not an RDMA port"),
        ({"iface": "enp1s0f0np0", "address": "example.com", "port": 47101}, "not an IP"),
    ],
)
def test_probe_targets_refuse_anything_but_a_fabric_address(target, why):
    with pytest.raises(nichealth.ProbeRefused, match=why):
        nichealth.validate_probe_targets([target], {"enp1s0f0np0"})


def test_probe_targets_accept_private_and_scoped_link_local_and_are_bounded():
    ok = nichealth.validate_probe_targets(
        [{"iface": "enp1s0f0np0", "address": "10.100.152.1", "port": 47101},
         {"iface": "enp1s0f0np0", "address": "169.254.1.2", "port": 47101},
         {"iface": "enp1s0f0np0", "address": "fe80::1%enp1s0f0np0", "port": 47101}],
        {"enp1s0f0np0"},
    )
    assert len(ok) == 3
    many = [{"iface": "enp1s0f0np0", "address": "10.0.0.2", "port": 47101}] * 17
    with pytest.raises(nichealth.ProbeRefused, match="At most 16"):
        nichealth.validate_probe_targets(many, {"enp1s0f0np0"})
    with pytest.raises(nichealth.ProbeRefused):
        nichealth.validate_probe_targets(None, {"enp1s0f0np0"})


def test_probe_connects_over_a_real_socket_and_tells_refused_from_listening():
    import socket

    lo = RailHealth("lo", None, True, None, None, ipv4=("127.0.0.1/8",))
    server = socket.create_server(("127.0.0.2", 0))
    port = server.getsockname()[1]
    closed = socket.create_server(("127.0.0.2", 0))
    dead = closed.getsockname()[1]
    closed.close()
    try:
        up, refused, nowhere = nichealth.probe_rails(
            [nichealth.ProbeTarget("lo", "127.0.0.2", port),
             nichealth.ProbeTarget("lo", "127.0.0.2", dead),
             nichealth.ProbeTarget("enp9", "10.9.9.9", port)],
            [lo],
        )
    finally:
        server.close()
    assert up.ok and up.reached and up.ms is not None and up.error is None
    assert not refused.ok and refused.reached and "nothing is listening" in refused.error
    assert not nowhere.reached and "no IPv4 address" in nowhere.error


def _res(iface, target, *, ok=False, reached=False, error="no answer in 1.5 s"):
    return nichealth.ProbeResult(iface, target, ok, reached, 0.1 if reached else None,
                                 None if ok else error)


def test_a_rail_warns_only_when_every_target_on_it_failed():
    reach = nichealth.RailReach("spark-26af", "spark-4d38", (
        _res("enp1s0f0np0", "10.100.152.1"),
        _res("enp1s0f0np0", "fe80::1%enp1s0f0np0", error="No route to host"),
        _res("enP2p1s0f0np0", "10.100.153.1"),
        _res("enP2p1s0f0np0", "fe80::2%enP2p1s0f0np0", ok=True, reached=True),
    ))
    (f,) = reach.findings()
    assert f.severity == "warning" and f.node_id == "spark-26af" and f.iface == "enp1s0f0np0"
    assert f.sentence == (
        "enp1s0f0np0 on spark-26af has a link but carried nothing to spark-4d38 "
        "(10.100.152.1: no answer in 1.5 s; fe80::1%enp1s0f0np0: No route to host)."
    )
    assert f.fix == "ping -c 3 -I enp1s0f0np0 10.100.152.1"


def test_a_refused_connection_proves_the_rail_and_says_the_receiver_is_missing():
    refused = "connection refused: nothing is listening on port 47101"
    reach = nichealth.RailReach("x", "y", (
        _res("enp1s0f0np0", "10.100.152.1", reached=True, error=refused),
        _res("enP2p1s0f0np0", "10.100.153.1", reached=True, error=refused),
    ))
    (f,) = reach.findings()
    assert f.severity == "info" and f.node_id == "y" and "port 47101" in f.sentence
    # One rail listening is enough: the receiver is there.
    ok = nichealth.RailReach("x", "y", (
        _res("enp1s0f0np0", "10.100.152.1", ok=True, reached=True),
        _res("enP2p1s0f0np0", "10.100.153.1", reached=True, error=refused),
    ))
    assert ok.findings() == []


def test_report_carries_reach_rows_and_its_findings():
    a, b = _clean_pair()
    health = {"x": {"interfaces": [r.to_json() for r in a]}, "y": {"interfaces": [r.to_json() for r in b]}}
    reach = {("x", "y"): nichealth.RailReach("x", "y", (
        _res("enp1s0f0np0", "10.100.152.1", ok=True, reached=True),
        _res("enP2p1s0f0np0", "10.100.153.1"),
    ))}
    (link,) = link_health_report([("x", "y")], health, {}, 1.0, reach)["links"]
    assert link["reach"] == [
        {"node_id": "x", "iface": "enp1s0f0np0", "target": "10.100.152.1", "ok": True, "ms": 0.1, "error": None},
        {"node_id": "x", "iface": "enP2p1s0f0np0", "target": "10.100.153.1", "ok": False, "ms": None,
         "error": "no answer in 1.5 s"},
    ]
    assert [(f["severity"], f["iface"]) for f in link["findings"]] == [("warning", "enP2p1s0f0np0")]
    # Not probed is an empty list, never a missing key.
    assert link_health_report([("x", "y")], health, {}, 1.0)["links"][0]["reach"] == []


def test_agent_probe_route_needs_the_token_and_refuses_public_targets(monkeypatch):
    monkeypatch.setattr(nichealth, "local_rails", lambda: [rail(ipv4=("10.100.152.2/24",))])
    dialled = []

    def fake_probe(targets, local):
        dialled.extend(targets)
        return [nichealth.ProbeResult(t.iface, t.address, True, True, 0.2, None) for t in targets]

    monkeypatch.setattr(nichealth, "probe_rails", fake_probe)
    http = TestClient(create_agent_app(NodeAgent(profile(), token="tok")))
    good = {"targets": [{"iface": "enp1s0f0np0", "address": "10.100.152.1", "port": 47101}]}
    assert http.post("/agent/links/probe", json=good).status_code == 403
    assert http.post("/agent/links/probe", json=good, headers={"X-Derate-Token": "no"}).status_code == 403
    public = {"targets": [{"iface": "enp1s0f0np0", "address": "1.1.1.1", "port": 47101}]}
    res = http.post("/agent/links/probe", json=public, headers={"X-Derate-Token": "tok"})
    assert res.status_code == 400 and "not a private" in res.json()["detail"]
    assert dialled == []
    res = http.post("/agent/links/probe", json=good, headers={"X-Derate-Token": "tok"})
    assert res.status_code == 200
    assert res.json() == {"results": [{"iface": "enp1s0f0np0", "target": "10.100.152.1", "ok": True,
                                       "reached": True, "ms": 0.2, "error": None}]}


def test_gateway_probes_in_process_when_the_coordinator_is_an_end(monkeypatch):
    a, b = _clean_pair()
    calls = []

    def fake_probe(targets, local):
        calls.append([(t.iface, t.address) for t in targets])
        return [nichealth.ProbeResult(t.iface, t.address, False, False, None, "no answer in 1.5 s")
                for t in targets]

    monkeypatch.setattr(nichealth, "probe_rails", fake_probe)
    client, seen = _gateway(
        monkeypatch,
        {"spark-01": {"interfaces": [r.to_json() for r in a]}, "spark-02": {"interfaces": [r.to_json() for r in b]}},
        coordinator="spark-02",
    )
    with client:
        body = client.get("/api/links/health", params={"a": "spark-01", "b": "spark-02"}).json()
    # The coordinator end dials, from its own rails toward spark-01's.
    assert calls == [[("enP2p1s0f0np0", "10.100.153.2"), ("enp1s0f0np0", "10.100.152.2")]]
    assert not any(path == "/agent/links/probe" for _, path, _ in seen)
    warnings = [f for f in body["links"][0]["findings"] if f["severity"] == "warning"]
    assert {(f["node_id"], f["iface"]) for f in warnings} == {
        ("spark-02", "enp1s0f0np0"), ("spark-02", "enP2p1s0f0np0"),
    }
    assert all("carried nothing to spark-01" in f["sentence"] for f in warnings)


def test_gateway_reports_an_agent_without_the_probe_route_as_info(monkeypatch):
    a, b = _clean_pair()
    client, _ = _gateway(
        monkeypatch,
        {"spark-01": {"interfaces": [r.to_json() for r in a]}, "spark-02": {"interfaces": [r.to_json() for r in b]}},
        probes={"spark-01": 404},
    )
    with client:
        body = client.get("/api/links/health", params={"a": "spark-01", "b": "spark-02"}).json()
    (f,) = body["links"][0]["findings"]
    assert f["severity"] == "info" and f["node_id"] == "spark-01"
    assert "no rail probe route" in f["sentence"] and "Upgrade that node" in f["sentence"]
    assert body["links"][0]["reach"] == []
