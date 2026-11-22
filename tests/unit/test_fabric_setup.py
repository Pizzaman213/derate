"""control_plane/links/fabric_setup.py: the host-side rail planner, against the
two Sparks' real 2026-11-21 state, with fake readers -- nothing touches a NIC."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from control_plane.links import fabric_setup as fs
from control_plane.links.fabric_setup import (
    Action,
    Addr,
    Config,
    HostState,
    Iface,
    Management,
    Refused,
)


def rail(name, rdma, pci, mtu, ipv4, ring=1024, cqe=False):
    return Iface(name, (rdma,), True, pci, mtu, ring, ring, 8192, 8192, cqe, tuple(ipv4))


def dark(name, rdma, pci):
    return Iface(name, (rdma,), False, pci)


def spark_26af() -> HostState:
    return HostState(
        (
            rail("enP2p1s0f0np0", "roceP2p1s0f0", "0002:01:00.0", 1500,
                 [Addr("169.254.212.153", 16, "link", True)]),
            dark("enP2p1s0f1np1", "roceP2p1s0f1", "0002:01:00.1"),
            rail("enp1s0f0np0", "rocep1s0f0", "0000:01:00.0", 9000,
                 [Addr("10.100.0.2", 30, "global", True)]),
            dark("enp1s0f1np1", "rocep1s0f1", "0000:01:00.1"),
        ),
        Management("enP7s7", "192.168.0.172", 24),
    )


def spark_4d38() -> HostState:
    return HostState(
        (
            rail("enP2p1s0f0np0", "roceP2p1s0f0", "0002:01:00.0", 1500,
                 [Addr("10.100.153.1", 24, "global", True)]),
            rail("enp1s0f0np0", "rocep1s0f0", "0000:01:00.0", 1500,
                 [Addr("10.100.152.1", 24, "global", True)]),
            dark("enp1s0f1np1", "rocep1s0f1", "0000:01:00.1"),
        ),
        Management("enP7s7", "192.168.0.71", 24),
    )


def simulate(state: HostState, actions) -> HostState:
    """What the machine looks like after `actions` ran successfully."""
    by = {i.name: i for i in state.interfaces}
    for a in actions:
        i = by[a.iface]
        if a.kind == "mtu":
            i = dataclasses.replace(i, mtu=a.value)
        elif a.kind == "ring":
            i = dataclasses.replace(i, ring_rx=a.value[0], ring_tx=a.value[1])
        elif a.kind == "cqe":
            i = dataclasses.replace(i, cqe_compress=a.value)
        elif a.kind == "addr_add":
            kept = tuple(x for x in i.ipv4 if not x.same(a.value))
            i = dataclasses.replace(i, ipv4=kept + (a.value,))
        elif a.kind == "addr_del":
            i = dataclasses.replace(i, ipv4=tuple(x for x in i.ipv4 if not x.same(a.value)))
        by[a.iface] = i
    return dataclasses.replace(state, interfaces=tuple(by[i.name] for i in state.interfaces))


def norm(state: HostState):
    return {i.name: dataclasses.replace(i, ipv4=tuple(sorted(i.ipv4, key=lambda a: a.cidr)))
            for i in state.interfaces}


def test_spark_26af_gets_both_fabric_addresses_and_loses_the_stale_ones():
    actions = fs.plan(spark_26af(), Config())

    assert [a.command() for a in actions] == [
        "ethtool -G enp1s0f0np0 rx 8192 tx 8192",
        "ethtool --set-priv-flags enp1s0f0np0 rx_cqe_compress on",
        "ip addr replace 10.100.152.172/24 brd + dev enp1s0f0np0",
        "ip link set dev enP2p1s0f0np0 mtu 9000",
        "ethtool -G enP2p1s0f0np0 rx 8192 tx 8192",
        "ethtool --set-priv-flags enP2p1s0f0np0 rx_cqe_compress on",
        "ip addr replace 10.100.153.172/24 brd + dev enP2p1s0f0np0",
        "ip addr del 10.100.0.2/30 dev enp1s0f0np0",
        "ip addr del 169.254.212.153/16 dev enP2p1s0f0np0",
    ]
    # The rail already at 9000 is not touched, and nothing runs on a dark port.
    assert not any(a.kind == "mtu" and a.iface == "enp1s0f0np0" for a in actions)
    assert {a.iface for a in actions} == {"enp1s0f0np0", "enP2p1s0f0np0"}


def test_rails_are_numbered_by_pci_address_not_by_a_byte_sort_of_the_names():
    # "enP2..." < "enp1..." as bytes; the PCI order puts enp1s0f0np0 first,
    # which is how spark-4d38's rails were numbered by hand.
    assert [r.name for r in fs.rails(spark_26af())] == ["enp1s0f0np0", "enP2p1s0f0np0"]
    want = fs.targets(spark_4d38(), Config())
    assert want["enp1s0f0np0"].cidr == "10.100.152.71/24"
    assert want["enP2p1s0f0np0"].cidr == "10.100.153.71/24"


def test_spark_4d38_keeps_its_hand_made_addresses_and_gains_dot_71():
    actions = fs.plan(spark_4d38(), Config())

    assert not [a for a in actions if a.kind == "addr_del"]
    adds = [(a.iface, a.value.cidr) for a in actions if a.kind == "addr_add"]
    assert adds == [("enp1s0f0np0", "10.100.152.71/24"),
                    ("enP2p1s0f0np0", "10.100.153.71/24")]
    after = simulate(spark_4d38(), actions)
    by = {i.name: {a.cidr for a in i.ipv4} for i in after.interfaces}
    assert by["enp1s0f0np0"] == {"10.100.152.1/24", "10.100.152.71/24"}
    assert by["enP2p1s0f0np0"] == {"10.100.153.1/24", "10.100.153.71/24"}


def test_a_converged_machine_gets_no_actions():
    for state in (spark_26af(), spark_4d38()):
        after = simulate(state, fs.plan(state, Config()))
        assert fs.plan(after, Config()) == []


def test_exclusive_false_keeps_every_other_address():
    actions = fs.plan(spark_26af(), Config(exclusive=False))
    assert not [a for a in actions if a.kind == "addr_del"]


def test_a_management_network_that_is_not_a_slash_24_is_refused_with_a_sentence():
    state = dataclasses.replace(spark_26af(), management=Management("enP7s7", "172.16.4.9", 16))
    with pytest.raises(Refused) as e:
        fs.plan(state, Config())
    assert "172.16.0.0/16" in e.value.sentence
    assert "not a /24" in e.value.sentence
    assert '"addresses"' in e.value.sentence


@pytest.mark.parametrize("mgmt", [None, Management("enP7s7", "192.168.0.255", 24),
                                  Management("enP7s7", "192.168.0.0", 24)])
def test_no_usable_management_octet_is_refused(mgmt):
    with pytest.raises(Refused):
        fs.plan(dataclasses.replace(spark_26af(), management=mgmt), Config())


def test_named_addresses_need_no_management_octet():
    state = dataclasses.replace(spark_26af(), management=Management("enP7s7", "172.16.4.9", 16))
    cfg = Config(addresses={"enp1s0f0np0": "10.100.152.9/24", "enP2p1s0f0np0": "10.100.153.9/24"})
    adds = [a.value.cidr for a in fs.plan(state, cfg) if a.kind == "addr_add"]
    assert adds == ["10.100.152.9/24", "10.100.153.9/24"]


def test_the_management_address_is_never_removed_even_on_a_rail():
    state = spark_4d38()
    mgmt_rail = dataclasses.replace(state.interfaces[1],
                                    ipv4=state.interfaces[1].ipv4 + (Addr("192.168.0.71", 24),))
    state = dataclasses.replace(state, interfaces=(state.interfaces[0], mgmt_rail, state.interfaces[2]))
    assert not [a for a in fs.plan(state, Config()) if a.kind == "addr_del"]


def test_snapshot_then_restore_undoes_exactly_the_apply():
    before = spark_26af()
    actions = fs.plan(before, Config())
    snap = fs.update_snapshot(None, before, actions)
    assert set(snap["added"]["enp1s0f0np0"]) == {"10.100.152.172/24"}
    applied = simulate(before, actions)

    # A second run sees the state the first made and must not overwrite the originals.
    assert fs.update_snapshot(snap, applied, fs.plan(applied, Config())) == snap

    restore = fs.plan_restore(applied, json.loads(json.dumps(snap)))
    kinds = sorted((a.kind, a.iface) for a in restore)
    assert kinds == sorted((
        ("ring", "enp1s0f0np0"), ("cqe", "enp1s0f0np0"), ("addr_add", "enp1s0f0np0"),
        ("addr_del", "enp1s0f0np0"),
        ("mtu", "enP2p1s0f0np0"), ("ring", "enP2p1s0f0np0"), ("cqe", "enP2p1s0f0np0"),
        ("addr_add", "enP2p1s0f0np0"), ("addr_del", "enP2p1s0f0np0"),
    ))
    # Flags travel: the link-local address comes back link-scoped, noprefixroute.
    back = next(a for a in restore if a.kind == "addr_add" and a.iface == "enP2p1s0f0np0")
    assert back.command() == ("ip addr replace 169.254.212.153/16 brd + dev enP2p1s0f0np0 "
                              "scope link noprefixroute")
    assert norm(simulate(applied, restore)) == norm(before)
    assert fs.plan_restore(before, snap) == []


def test_restore_never_deletes_an_address_derate_did_not_add():
    before = spark_4d38()
    actions = fs.plan(before, Config())
    snap = fs.update_snapshot(None, before, actions)
    applied = simulate(before, actions)
    # Something else (DHCP, an operator) adds an address afterwards.
    extra = Action("addr_add", "enp1s0f0np0", Addr("10.100.152.200", 24), "")
    later = simulate(applied, [extra])
    dels = [a.value.cidr for a in fs.plan_restore(later, snap) if a.kind == "addr_del"]
    assert sorted(dels) == ["10.100.152.71/24", "10.100.153.71/24"]


def test_config_refuses_an_unknown_key_rather_than_ignoring_it():
    with pytest.raises(Refused) as e:
        Config.from_json({"mtus": 9000})
    assert "mtus" in e.value.sentence
    with pytest.raises(Refused):
        Config.from_json({"subnet_base": "10.100.152.7"})
    assert Config.from_json(Config().to_json()) == Config()


# ---------------------------------------------------------------------------
# read_state and main, with a fake sysfs and a fake command runner
# ---------------------------------------------------------------------------

IP_ADDR_26AF_P1 = json.dumps([{"ifname": "enp1s0f0np0", "mtu": 9000, "addr_info": [
    {"family": "inet", "local": "10.100.0.2", "prefixlen": 30, "scope": "global",
     "noprefixroute": True},
    {"family": "inet6", "local": "fe80::4ebb:47ff:fe80:26b0", "prefixlen": 64, "scope": "link"}]}])
IP_ADDR_26AF_P2 = json.dumps([{"ifname": "enP2p1s0f0np0", "addr_info": [
    {"family": "inet", "local": "169.254.212.153", "prefixlen": 16, "scope": "link",
     "noprefixroute": True}]}])
IP_ADDR_MGMT = json.dumps([{"ifname": "enP7s7", "addr_info": [
    {"family": "inet", "local": "192.168.0.172", "prefixlen": 24, "scope": "global"}]}])
ROUTE = json.dumps([{"dst": "default", "gateway": "192.168.0.1", "dev": "enP7s7",
                     "prefsrc": "192.168.0.172", "metric": 102}])
RINGS = """Ring parameters for enp1s0f0np0:
Pre-set maximums:
RX:\t\t\t8192
RX Mini:\t\tn/a
TX:\t\t\t8192
Current hardware settings:
RX:\t\t\t1024
RX Mini:\t\tn/a
TX:\t\t\t1024
RX Buf Len:\t\tn/a
"""
PRIV = """Private flags for enp1s0f0np0:
rx_cqe_moder       : off
rx_cqe_compress    : off
rx_striding_rq     : on
"""


def fake_sysfs(root: Path) -> Path:
    net = root / "net"
    for name, pci, rdma, carrier, mtu in [
        ("enp1s0f0np0", "0000:01:00.0", "rocep1s0f0", "1", "9000"),
        ("enP2p1s0f0np0", "0002:01:00.0", "roceP2p1s0f0", "1", "1500"),
        ("enp1s0f1np1", "0000:01:00.1", "rocep1s0f1", "0", "1500"),
        ("enP2p1s0f1np1", "0002:01:00.1", "roceP2p1s0f1", "0", "1500"),
        ("enP7s7", "0007:01:00.0", None, "1", "1500"),
    ]:
        dev = root / "devices" / pci
        (dev / "infiniband" / (rdma or "")).mkdir(parents=True, exist_ok=True) if rdma \
            else dev.mkdir(parents=True)
        (net / name).mkdir(parents=True)
        (net / name / "device").symlink_to(dev)
        (net / name / "carrier").write_text(carrier + "\n")
        (net / name / "mtu").write_text(mtu + "\n")
    return net


class FakeRun:
    def __init__(self, ethtool=True):
        self.calls: list[tuple[str, ...]] = []
        self.ethtool = ethtool

    def __call__(self, argv):
        argv = tuple(argv)
        self.calls.append(argv)
        if argv[0] == "ethtool" and not self.ethtool:
            return 127, "", "ethtool: not installed"
        table = {
            ("ip", "-j", "addr", "show", "dev", "enp1s0f0np0"): IP_ADDR_26AF_P1,
            ("ip", "-j", "addr", "show", "dev", "enP2p1s0f0np0"): IP_ADDR_26AF_P2,
            ("ip", "-j", "addr", "show", "dev", "enP7s7"): IP_ADDR_MGMT,
            ("ip", "-j", "route", "show", "default"): ROUTE,
        }
        if argv in table:
            return 0, table[argv], ""
        if argv[:2] == ("ethtool", "-g"):
            return 0, RINGS, ""
        if argv[:2] == ("ethtool", "--show-priv-flags"):
            return 0, PRIV, ""
        return 0, "", ""  # a change command

    def changes(self):
        return [c for c in self.calls if c[:2] in (("ip", "link"), ("ip", "addr"))
                or c[:2] in (("ethtool", "-G"), ("ethtool", "--set-priv-flags"))]


def test_read_state_reads_the_real_tool_output_into_the_26af_state(tmp_path):
    state = fs.read_state(FakeRun(), fake_sysfs(tmp_path))
    assert norm(state) == norm(spark_26af())
    assert state.management == Management("enP7s7", "192.168.0.172", 24)
    assert state.unread == ()


def test_without_ethtool_rings_and_cqe_are_left_alone_and_said_so(tmp_path):
    state = fs.read_state(FakeRun(ethtool=False), fake_sysfs(tmp_path))
    assert "ethtool is not installed" in state.unread[0]
    kinds = {a.kind for a in fs.plan(state, Config())}
    assert "ring" not in kinds and "cqe" not in kinds and "addr_add" in kinds


def test_dry_run_prints_the_plan_and_changes_nothing(tmp_path, capsys):
    run = FakeRun()
    rc = fs.main(["--dry-run", "--config", str(tmp_path / "absent.json")], run=run,
                 sys_net=fake_sysfs(tmp_path), euid=lambda: 1000)
    out = capsys.readouterr().out
    assert rc == 0
    assert "dry run" in out and "exclusive: true" in out
    assert "ip addr replace 10.100.152.172/24 brd + dev enp1s0f0np0" in out
    assert run.changes() == []


def test_apply_without_root_runs_nothing(tmp_path):
    run = FakeRun()
    assert fs.main(["--apply"], run=run, sys_net=fake_sysfs(tmp_path), euid=lambda: 1000) == 2
    assert run.calls == []


def test_apply_snapshots_first_and_restore_reverses_it(tmp_path):
    net = fake_sysfs(tmp_path)
    snap = tmp_path / "var" / "fabric-snapshot.json"
    cfg = tmp_path / "fabric.json"
    cfg.write_text(json.dumps(Config().to_json()))
    run = FakeRun()
    assert fs.main(["--apply", "--config", str(cfg), "--snapshot", str(snap)],
                   run=run, sys_net=net, euid=lambda: 0) == 0
    assert len(run.changes()) == 9
    recorded = json.loads(snap.read_text())
    assert recorded["interfaces"]["enP2p1s0f0np0"]["mtu"] == 1500

    # The fake machine never changed, so it already matches the record: restore
    # has nothing to run, and a real (non-dry) restore then drops the record.
    restore_run = FakeRun()
    assert fs.main(["--restore", str(snap), "--dry-run"], run=restore_run, sys_net=net,
                   euid=lambda: 1000) == 0
    assert restore_run.changes() == []
    assert snap.exists()
    assert fs.main(["--restore", str(snap)], run=restore_run, sys_net=net, euid=lambda: 0) == 0
    assert not snap.exists()


def test_a_failed_action_does_not_stop_the_rest_and_fails_the_run(tmp_path):
    class Failing(FakeRun):
        def __call__(self, argv):
            if tuple(argv)[:2] == ("ethtool", "-G"):
                self.calls.append(tuple(argv))
                return 1, "", "netlink error: Invalid argument"
            return super().__call__(argv)

    run = Failing()
    rc = fs.main(["--apply", "--config", str(tmp_path / "none.json")], run=run,
                 sys_net=fake_sysfs(tmp_path), euid=lambda: 0)
    assert rc == 1
    assert len(run.changes()) == 9


# --- execute keeps the old address when the new one failed ---------------------


def test_a_failed_replace_skips_that_rails_removals_only():
    a, b = "enp1s0f0np0", "enP2p1s0f0np0"
    actions = [
        Action("addr_add", a, Addr("10.100.152.2", 24), "add"),
        Action("addr_add", b, Addr("10.100.153.2", 24), "add"),
        Action("addr_del", a, Addr("10.100.0.2", 30), "del old"),
        Action("addr_del", b, Addr("169.254.212.153", 16, "link"), "del old"),
    ]
    ran = []

    def run(argv):
        ran.append(argv)
        if argv[2] == "replace" and argv[-1] == a:
            return 2, "", "RTNETLINK answers: File exists"
        return 0, "", ""

    failures = fs.execute(actions, run, out=lambda s: None)
    assert ["ip", "addr", "del", "10.100.0.2/30", "dev", a] not in ran
    assert ["ip", "addr", "del", "169.254.212.153/16", "dev", b] in ran
    assert len(failures) == 2 and any("skipped" in f and a in f for f in failures)


# --- strict values before any root argv -------------------------------------------


@pytest.mark.parametrize("iface", ["eth0;reboot", "a b", "x" * 16, "", "-"[:0]])
def test_bad_interface_names_are_refused(iface):
    with pytest.raises(Refused):
        Action("mtu", iface, 9000, "mtu")
    with pytest.raises(Refused):
        Config.from_json({"addresses": {iface: "10.100.152.7/24"}})


@pytest.mark.parametrize("kind,value", [
    ("mtu", 100), ("mtu", "9000"), ("mtu", True), ("ring", (0, 8)), ("ring", ("1", 2)),
    ("cqe", "on"), ("addr_add", Addr("10.0.0.1; x", 24)), ("addr_del", Addr("10.0.0.1", 33)),
    ("addr_add", Addr("10.0.0.1", 24, "global; x")), ("bogus", 1),
])
def test_bad_action_values_are_refused(kind, value):
    with pytest.raises(Refused):
        Action(kind, "enp1s0f0np0", value, "x")


def test_a_malformed_snapshot_is_refused_not_run():
    snap = {"interfaces": {"enp1s0f0np0": {"mtu": "9000; reboot", "ipv4": []}}}
    with pytest.raises(Refused):
        fs.plan_restore(spark_26af(), snap)
    snap = {"interfaces": {"enp1s0f0np0": {"ipv4": [{"address": "1.2.3.4 x", "prefix": 8}]}}}
    with pytest.raises(Refused):
        fs.plan_restore(spark_26af(), snap)


# --- root writes ------------------------------------------------------------------


def test_write_json_is_atomic_and_0644(tmp_path):
    target = tmp_path / "snap.json"
    fs._write_json(str(target), {"a": 1})
    assert json.loads(target.read_text()) == {"a": 1}
    assert target.stat().st_mode & 0o777 == 0o644
    assert [p.name for p in tmp_path.iterdir()] == ["snap.json"]


def test_write_json_refuses_a_symlinked_target(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (tmp_path / "snap.json").symlink_to(victim)
    with pytest.raises(Refused):
        fs._write_json(str(tmp_path / "snap.json"), {"a": 1})
    assert victim.read_text() == "keep"


def test_write_json_refuses_a_symlinked_or_foreign_directory(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(Refused):
        fs._write_json(str(tmp_path / "link" / "snap.json"), {"a": 1})
    with pytest.raises(Refused):
        fs._write_json(str(real / "snap.json"), {"a": 1}, euid=lambda: 12345)
    assert list(real.iterdir()) == []


def test_write_json_refuses_a_symlinked_ancestor(tmp_path):
    real = tmp_path / "real"
    (real / "sub").mkdir(parents=True)
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(fs.Refused, match="symlink"):
        fs._write_json(str(tmp_path / "link" / "sub" / "x.json"), {"a": 1})
    assert not (real / "sub" / "x.json").exists()


def test_write_json_creates_missing_dirs_and_leaves_no_temp(tmp_path):
    import os
    target = tmp_path / "a" / "b" / "x.json"
    fs._write_json(str(target), {"a": 1})
    assert json.loads(target.read_text()) == {"a": 1}
    assert target.stat().st_mode & 0o777 == 0o644
    assert os.listdir(target.parent) == ["x.json"]
