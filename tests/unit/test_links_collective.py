"""control_plane/links/collective.py: the pieces that run without a GPU.

The nccl-torch rung's real work happens in two containers on two machines;
everything here is the parsing, classification and argv that decide whether
that work is read correctly and launched with the flags it cannot do without.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from control_plane.links import collective
from control_plane.links.collective import (
    _MARKER,
    _SIGNATURES,
    _classify,
    _parse,
    docker_argv,
    local_interface_for,
    run_collective,
)


def test_parse_finds_the_marker_line_among_noise():
    text = "\n".join([
        "NCCL INFO NET/IB : Using [0]rocep1s0f0",
        "some warning __derate_collective not at the start",
        _MARKER + '{"rows": [{"bytes": 8}], "nccl": "2.31.2"}',
        "trailing noise",
    ])
    assert _parse(text) == {"rows": [{"bytes": 8}], "nccl": "2.31.2"}


def test_parse_returns_none_on_malformed_json_after_the_marker():
    assert _parse("noise\n" + _MARKER + "{not json") is None


def test_parse_returns_none_without_a_marker():
    assert _parse("") is None
    assert _parse(None) is None
    assert _parse("rank 0 exited\n") is None


@pytest.mark.parametrize("needle,sentence", _SIGNATURES)
def test_classify_names_each_known_signature(needle, sentence):
    assert _classify(f"line before\n... {needle} ...\nline after") == sentence


def test_classify_falls_back_for_unknown_output():
    assert _classify("Segmentation fault") == "the collective did not complete"


def test_docker_argv_carries_the_flags_the_fabric_needs():
    argv = docker_argv("img:tag", {"RANK": "0", "MASTER_ADDR": "10.100.152.2"}, ["8,1024", "5", "20"])

    def adjacent(a, b):
        return any(argv[i] == a and argv[i + 1] == b for i in range(len(argv) - 1))

    # Without the device NCCL drops to TCP on the management LAN, silently.
    assert adjacent("--device", "/dev/infiniband")
    assert adjacent("--network", "host")
    assert adjacent("--cap-add", "IPC_LOCK")
    assert adjacent("-e", "RANK=0")
    assert adjacent("-e", "MASTER_ADDR=10.100.152.2")
    # The image comes after every docker option, the worker args last.
    assert argv.index("img:tag") > argv.index("--device")
    assert argv[-3:] == ["8,1024", "5", "20"]


IP_ADDR = """1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
2: enP7s7    inet 192.168.0.172/24 brd 192.168.0.255 scope global enP7s7\\       valid_lft forever
3: enp1s0f0np0    inet 10.100.152.2/24 brd 10.100.152.255 scope global enp1s0f0np0\\       valid_lft forever
"""


def _fake_run(stdout):
    def run(argv, **kwargs):
        assert argv == ["ip", "-o", "-4", "addr", "show"]
        return SimpleNamespace(stdout=stdout, returncode=0)

    return run


def test_local_interface_for_finds_the_iface_carrying_the_address(monkeypatch):
    monkeypatch.setattr(collective.subprocess, "run", _fake_run(IP_ADDR))
    assert local_interface_for("10.100.152.2") == "enp1s0f0np0"
    assert local_interface_for("192.168.0.172") == "enP7s7"


def test_local_interface_for_is_none_for_an_absent_address(monkeypatch):
    monkeypatch.setattr(collective.subprocess, "run", _fake_run(IP_ADDR))
    assert local_interface_for("10.100.152.1") is None
    # A prefix of a real address is not that address.
    assert local_interface_for("10.100.152") is None


def test_local_interface_for_is_none_when_ip_is_missing(monkeypatch):
    def boom(*a, **k):
        raise OSError("ip: not found")

    monkeypatch.setattr(collective.subprocess, "run", boom)
    assert local_interface_for("10.100.152.2") is None


@pytest.mark.parametrize("master", ["127.0.0.1", "127.0.1.1", ""])
def test_run_collective_refuses_loopback_without_starting_anything(monkeypatch, master):
    def forbidden(*a, **k):
        raise AssertionError("run_collective started a process for a loopback master")

    monkeypatch.setattr(collective.subprocess, "Popen", forbidden)
    monkeypatch.setattr(collective.subprocess, "run", forbidden)
    result = run_collective(
        image="img", sizes=[8], master_addr=master, peer_host="10.100.152.1",
        env={"NCCL_MAX_NCHANNELS": "2"},
    )
    assert result.error == "master_addr must be an address the peer can reach, not loopback"
    assert not result.ok
    assert result.env == {"NCCL_MAX_NCHANNELS": "2"}


def test_run_collective_reports_a_missing_docker_as_an_error(monkeypatch):
    def no_docker(*a, **k):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(collective.subprocess, "Popen", no_docker)
    result = run_collective(image="img", sizes=[8], master_addr="10.100.152.2", peer_host="10.100.152.1")
    assert result.error and "docker" in result.error
    assert result.rows == []


def test_run_collective_classifies_output_without_a_result(monkeypatch):
    class Proc:
        def __init__(self, argv, **kwargs):
            self.argv = argv

        def communicate(self, timeout=None):
            return ("NCCL WARN NET/IB : No device found.\n", None)

        def poll(self):
            return 0

        def kill(self):
            pass

    started = []
    monkeypatch.setattr(collective.subprocess, "Popen", lambda argv, **k: started.append(argv) or Proc(argv))
    result = run_collective(
        image="img", sizes=[8], master_addr="10.100.152.2", peer_host="10.100.152.1", iface="enp1s0f0np0",
    )
    assert result.error == "the container cannot see /dev/infiniband"
    assert started[0][:2] == ["docker", "run"]
    assert started[1][0] == "ssh" and "10.100.152.1" in started[1]
    assert "NCCL_SOCKET_IFNAME=enp1s0f0np0" in started[0]
