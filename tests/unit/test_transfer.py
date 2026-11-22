"""Copying a model cache folder to another node (control_plane/transfer).

Real loopback sockets and a real agent app: the receiver is the one the node
agent runs, the sender is the one the coordinator runs, and the only fakes are
the fabric addresses (loopback stands in for a RoCE rail) and the registry.
"""

from __future__ import annotations

import filecmp
import hashlib
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from control_plane.registry.agent import NodeAgent, create_agent_app
from control_plane.transfer import protocol, receiver as receiver_mod
from control_plane.transfer import sender as sender_mod
from control_plane.transfer.fabric import (
    LinkAddress,
    LinkPath,
    candidate_paths,
    describe,
    fastest_tier,
    link_addresses,
)
from control_plane.transfer.jobs import CancelRefused, ReplicateRefused, Replicator
from control_plane.transfer.protocol import MAGIC, PARTIAL_SUFFIX, Manifest, ManifestError
from control_plane.transfer.receiver import TransferRefused, start_transfer_receiver
from control_plane.transfer.sender import TransferCancelled, TransferError, send_folder
from tests.unit.test_agent_tokens import profile
from tests.unit.test_links import FakeRunner

LOOPBACK = (LinkAddress("lo", "127.0.0.1", 8, speed_mbps=200_000, rdma=True),)
FOLDER = "models--org--m"


def _git_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


@pytest.fixture()
def src_cache(tmp_path):
    root = tmp_path / "src"
    repo = root / FOLDER
    weights = os.urandom(3 << 20)
    config = json.dumps({"model_type": "x"}).encode()
    big = hashlib.sha256(weights).hexdigest()
    small = _git_sha1(config)
    (repo / "blobs").mkdir(parents=True)
    (repo / "blobs" / big).write_bytes(weights)
    (repo / "blobs" / small).write_bytes(config)
    snap = repo / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "model.safetensors").symlink_to(f"../../blobs/{big}")
    (snap / "config.json").symlink_to(f"../../blobs/{small}")
    (repo / "refs").mkdir()
    (repo / "refs" / "main").write_text("abc")
    (repo / ".no_exist" / "abc").mkdir(parents=True)
    (repo / ".no_exist" / "abc" / "tokenizer.json").write_bytes(b"")
    (repo / "trees").mkdir()
    (repo / "trees" / "abc.json").write_text("[]")
    os.utime(repo / "blobs" / big, (1_700_000_000, 1_700_000_000))
    return repo


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(protocol, "CHUNK_BYTES", 1 << 20)
    dst = tmp_path / "dst"
    dst.mkdir()
    rx = start_transfer_receiver(
        host="127.0.0.1", port=0, cache_dir=lambda: dst, addresses=lambda: LOOPBACK
    )
    agent = NodeAgent(profile(), token="tok")
    agent.transfer_receiver = rx
    http = TestClient(create_agent_app(agent))
    yield dst, rx, http
    rx.close()


def _send(src, http, **kw):
    kw.setdefault("cancel", threading.Event())
    kw.setdefault("on_progress", lambda done, total: None)
    return send_folder(
        src, agent_url="http://testserver", token="tok", http=http,
        local_addresses=lambda: LOOPBACK, streams=3, **kw,
    )


def _partials(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.name.endswith(PARTIAL_SUFFIX)]


def _tree(root: Path) -> dict[str, tuple]:
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        st = os.lstat(p)
        if p.is_symlink():
            out[rel] = ("link", os.readlink(p), st.st_mtime)
        elif p.is_file():
            out[rel] = ("file", st.st_size, st.st_mode & 0o777, st.st_mtime)
    return out


def test_a_folder_arrives_identical_links_and_mtimes_included(src_cache, rig):
    dst, _, http = rig
    progress = []
    result = _send(src_cache, http, on_progress=lambda d, t: progress.append((d, t)))
    assert result.path == "fabric"
    assert result.routes == ["127.0.0.1->127.0.0.1"]
    assert result.link == "RoCE, 200 Gb/s"
    got = dst / FOLDER
    assert _tree(got) == _tree(src_cache)
    for rel, entry in _tree(src_cache).items():
        if entry[0] == "file":
            assert filecmp.cmp(src_cache / rel, got / rel, shallow=False), rel
    assert result.verified == 2  # both blobs hash to their names
    # Every regular file: the two blobs, refs/main ("abc"), the empty marker
    # and trees/abc.json ("[]").
    assert progress[-1][0] == progress[-1][1] == (3 << 20) + len(json.dumps({"model_type": "x"})) + 3 + 2
    assert _partials(dst) == []


def test_a_second_copy_sends_only_what_is_not_named_by_its_hash(src_cache, rig):
    _, _, http = rig
    _send(src_cache, http)
    again = _send(src_cache, http)
    # Both blobs are skipped on size; refs/main, trees/abc.json and the empty
    # .no_exist marker are metadata and always travel.
    assert again.skipped == 2 and again.files == 5
    assert again.bytes_sent == len("abc") + len("[]")


def test_a_revision_refresh_moves_refs_main_even_at_the_same_length(src_cache, rig):
    dst, _, http = rig
    old, new = "a" * 40, "b" * 40
    (src_cache / "refs" / "main").write_text(new)
    (dst / FOLDER / "refs").mkdir(parents=True)
    (dst / FOLDER / "refs" / "main").write_text(old)
    _send(src_cache, http)
    assert (dst / FOLDER / "refs" / "main").read_text() == new


def test_a_platform_without_dir_fd_operations_does_not_start_a_receiver(tmp_path, monkeypatch):
    monkeypatch.setattr(receiver_mod, "_AT", False)
    with pytest.raises(OSError) as caught:
        start_transfer_receiver(host="127.0.0.1", port=0, cache_dir=lambda: tmp_path,
                                addresses=lambda: LOOPBACK)
    assert "no-follow" in str(caught.value)


def test_a_blob_that_does_not_hash_to_its_name_is_refused_and_removed(src_cache, rig):
    dst, _, http = rig
    bad = "0" * 64
    (src_cache / "blobs" / bad).write_bytes(b"not what the name says")
    with pytest.raises(TransferError, match="arrived corrupted"):
        _send(src_cache, http)
    assert _partials(dst) == []
    assert not (dst / FOLDER / "blobs" / bad).exists()


def test_a_stream_with_the_wrong_mac_is_closed_and_writes_nothing(src_cache, rig):
    dst, rx, http = rig
    opened = http.post(
        "/agent/transfers", json=protocol.build_manifest(src_cache).to_json(),
        headers={"X-Derate-Token": "tok"},
    ).json()
    with socket.create_connection(("127.0.0.1", rx.port), timeout=5) as sock:
        sock.sendall(MAGIC + opened["session_id"].encode())
        assert len(sock.recv(32)) == 32
        sock.sendall(b"\x00" * 32)
        assert sock.recv(1) == b""  # closed, no ack
    assert _partials(dst) == []
    assert not (dst / FOLDER).exists()


def test_a_blob_verified_before_a_copy_is_abandoned_is_kept_and_not_sent_again(src_cache, rig):
    """Background copies are cancelled by design (a launch pauses them), so a
    blob that already hashed to its name stays; the next attempt skips it."""
    import time

    dst, rx, http = rig
    manifest = protocol.build_manifest(src_cache)
    opened = http.post(
        "/agent/transfers", json=manifest.to_json(), headers={"X-Derate-Token": "tok"},
    ).json()
    big = next(i for i, f in enumerate(manifest.files) if f.size == 3 << 20)
    data = (src_cache / manifest.files[big].path).read_bytes()
    with socket.create_connection(("127.0.0.1", rx.port), timeout=5) as sock:
        sock.sendall(MAGIC + opened["session_id"].encode())
        nonce = sock.recv(32)
        sock.sendall(protocol.handshake_mac(bytes.fromhex(opened["secret"]), nonce))
        assert sock.recv(1) == protocol.ACK_OK
        sock.sendall(protocol.CHUNK_HEADER.pack(big, 0, len(data)) + data)
        assert sock.recv(1) == protocol.ACK_OK
    session = rx._sessions[opened["session_id"]]
    deadline = time.monotonic() + 10
    while big not in session.placed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert http.delete(f"/agent/transfers/{opened['session_id']}",
                       headers={"X-Derate-Token": "tok"}).status_code == 204
    kept = dst / FOLDER / manifest.files[big].path
    assert kept.read_bytes() == data and _partials(dst) == []
    again = _send(src_cache, http)
    assert again.bytes_sent == sum(f.size for f in manifest.files) - len(data)
    assert _tree(dst / FOLDER) == _tree(src_cache)


# -- the receiver runs as root and the cache folder belongs to the user -------

TOK = {"X-Derate-Token": "tok"}


def _open(http, src):
    manifest = protocol.build_manifest(src)
    opened = http.post("/agent/transfers", json=manifest.to_json(), headers=TOK)
    assert opened.status_code == 200, opened.text
    big = next(i for i, f in enumerate(manifest.files) if f.size == 3 << 20)
    return manifest, opened.json(), big


def _authed(rx, opened) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", rx.port), timeout=5)
    sock.sendall(MAGIC + opened["session_id"].encode())
    nonce = receiver_mod._recv_exactly(sock, 32)
    sock.sendall(protocol.handshake_mac(bytes.fromhex(opened["secret"]), nonce))
    assert sock.recv(1) == protocol.ACK_OK
    return sock


def test_a_link_planted_where_a_folder_belongs_refuses_the_copy(src_cache, rig, tmp_path):
    dst, rx, http = rig
    outside = tmp_path / "outside"
    outside.mkdir()
    (dst / FOLDER).mkdir()
    (dst / FOLDER / "blobs").symlink_to(outside)
    with pytest.raises(TransferError, match=f"{FOLDER}/blobs is a link or not a folder on this node"):
        _send(src_cache, http)
    assert list(outside.iterdir()) == []
    assert rx._sessions == {}


def test_a_link_planted_after_the_copy_opened_is_refused_at_its_first_chunk(src_cache, rig, tmp_path):
    dst, rx, http = rig
    manifest, opened, big = _open(http, src_cache)
    outside = tmp_path / "outside"
    outside.mkdir()
    (dst / FOLDER).mkdir()
    (dst / FOLDER / "blobs").symlink_to(outside)
    with _authed(rx, opened) as sock:
        sock.sendall(protocol.CHUNK_HEADER.pack(big, 0, manifest.files[big].size))
        assert sock.recv(1) == protocol.ACK_REFUSED
    assert list(outside.iterdir()) == []


def test_a_link_named_like_a_blob_is_not_the_blob_and_its_target_is_untouched(src_cache, rig, tmp_path):
    dst, _, http = rig
    manifest = protocol.build_manifest(src_cache)
    path = next(f.path for f in manifest.files if f.size == 3 << 20)
    decoy = tmp_path / "decoy"
    decoy.write_bytes(b"x" * (3 << 20))  # the right size, so a followed stat calls it present
    (dst / FOLDER / "blobs").mkdir(parents=True)
    (dst / FOLDER / path).symlink_to(decoy)
    result = _send(src_cache, http)
    assert result.skipped == 0
    got = dst / FOLDER / path
    assert not got.is_symlink() and got.read_bytes() == (src_cache / path).read_bytes()
    assert decoy.read_bytes() == b"x" * (3 << 20)


def test_a_link_planted_under_a_partials_name_is_replaced_not_written_through(src_cache, rig, tmp_path):
    dst, _, http = rig
    manifest = protocol.build_manifest(src_cache)
    path = next(f.path for f in manifest.files if f.size == 3 << 20)
    decoy = tmp_path / "decoy"
    decoy.write_bytes(b"untouched")
    (dst / FOLDER / "blobs").mkdir(parents=True)
    (dst / FOLDER / (path + PARTIAL_SUFFIX)).symlink_to(decoy)
    _send(src_cache, http)
    assert decoy.read_bytes() == b"untouched"
    assert _tree(dst / FOLDER) == _tree(src_cache)


def test_a_stream_that_says_nothing_is_dropped_at_the_short_handshake_timeout(rig, monkeypatch):
    _, rx, _ = rig
    monkeypatch.setattr(receiver_mod, "_AUTH_TIMEOUT_S", 0.5)
    with socket.create_connection(("127.0.0.1", rx.port), timeout=10) as sock:
        start = time.monotonic()
        assert sock.recv(1) == b""
        assert time.monotonic() - start < 3  # not the 120 s an authenticated stream gets


def test_the_handshake_timeout_is_one_deadline_not_one_per_byte(rig, monkeypatch):
    """A byte every 0.2 s never trips a 0.5 s per-read timeout; the deadline
    closes the stream before the 24-byte hello is complete, so no answer comes."""
    _, rx, _ = rig
    monkeypatch.setattr(receiver_mod, "_AUTH_TIMEOUT_S", 0.5)
    with socket.create_connection(("127.0.0.1", rx.port), timeout=10) as sock:
        try:
            for b in MAGIC + b"0" * 16:
                sock.sendall(bytes([b]))
                time.sleep(0.2)
            answer = sock.recv(1)
        except OSError:
            answer = b""
    assert answer == b""  # never ACK_REFUSED, which a completed hello would get


def test_a_resent_chunk_of_a_finished_file_is_acked_and_reopens_nothing(src_cache, rig):
    dst, rx, http = rig
    manifest, opened, big = _open(http, src_cache)
    data = (src_cache / manifest.files[big].path).read_bytes()
    session = rx._sessions[opened["session_id"]]
    chunk = protocol.CHUNK_HEADER.pack(big, 0, len(data)) + data
    with _authed(rx, opened) as sock:
        sock.sendall(chunk)
        assert sock.recv(1) == protocol.ACK_OK
        deadline = time.monotonic() + 10
        while big not in session.placed and time.monotonic() < deadline:
            time.sleep(0.01)
        assert big in session.placed
        assert big not in session.fds  # closed once checked, not held until commit
        sock.sendall(chunk)  # the sender lost the first ack and resends
        assert sock.recv(1) == protocol.ACK_OK
    assert big not in session.fds
    assert _partials(dst) == []
    assert session.received == len(data)
    assert (dst / FOLDER / manifest.files[big].path).read_bytes() == data
    assert http.delete(f"/agent/transfers/{opened['session_id']}", headers=TOK).status_code == 204


def test_a_resent_chunk_after_a_failed_hash_is_refused_not_acked(src_cache, rig):
    _, rx, http = rig
    manifest, opened, big = _open(http, src_cache)
    size = manifest.files[big].size
    session = rx._sessions[opened["session_id"]]
    chunk = protocol.CHUNK_HEADER.pack(big, 0, size) + b"\x00" * size  # not what the name says
    with _authed(rx, opened) as sock:
        sock.sendall(chunk)
        assert sock.recv(1) == protocol.ACK_OK
        assert _wait_until(lambda: big in session.checks and session.checks[big].done())
        assert session.checks[big].result() is False
        sock.sendall(chunk)
        assert sock.recv(1) == protocol.ACK_REFUSED
    assert http.delete(f"/agent/transfers/{opened['session_id']}", headers=TOK).status_code == 204


def test_files_waiting_for_a_check_hold_no_descriptor(tmp_path, rig, monkeypatch):
    """Checks blocked behind a slow hash must not pin one fd per finished file."""
    _, rx, http = rig
    repo = tmp_path / "many" / FOLDER
    (repo / "blobs").mkdir(parents=True)
    for k in range(40):
        data = b"blob %d" % k
        (repo / "blobs" / hashlib.sha256(data).hexdigest()).write_bytes(data)
    manifest = protocol.build_manifest(repo)
    gate = threading.Event()
    real = receiver_mod._digest_fd

    def slow(fd, kind, size):
        gate.wait(10)
        return real(fd, kind, size)

    monkeypatch.setattr(receiver_mod, "_digest_fd", slow)
    opened = http.post("/agent/transfers", json=manifest.to_json(), headers=TOK).json()
    session = rx._sessions[opened["session_id"]]
    try:
        with _authed(rx, opened) as sock:
            for i, f in enumerate(manifest.files):
                sock.sendall(protocol.CHUNK_HEADER.pack(i, 0, f.size) + (repo / f.path).read_bytes())
                assert sock.recv(1) == protocol.ACK_OK
            # Every file is in; the checks are blocked in the hash.
            assert len(session.checks) == 40
            assert session.fds == {}
    finally:
        gate.set()
    assert http.post(f"/agent/transfers/{opened['session_id']}/commit", headers=TOK).status_code == 200



def test_capacity_refuses_a_folder_that_is_not_a_cache_folder(rig):
    _, _, http = rig
    r = http.get("/agent/transfers/capacity", params={"folder": "models--../x"}, headers=TOK)
    assert r.status_code == 400
    assert r.json()["detail"] == "folder must be a model cache folder, models--owner--name"
    plain = http.get("/agent/transfers/capacity", headers=TOK).json()
    assert "landing" not in plain



@pytest.mark.parametrize(
    "body, sentence",
    [
        ({"folder": FOLDER, "files": [{"path": "blobs/../../x", "size": 1}]}, "not a path inside"),
        (
            {"folder": FOLDER, "links": [{"path": "snapshots/a/x", "target": "../../../etc/passwd"}]},
            "points outside the folder",
        ),
        ({"folder": "models--../x"}, "models--owner--name"),
    ],
)
def test_a_manifest_that_escapes_the_folder_is_refused(rig, body, sentence):
    with pytest.raises(ManifestError, match=sentence):
        Manifest.from_json(body)
    _, rx, _ = rig
    with pytest.raises(TransferRefused) as caught:
        rx.open(body)
    assert caught.value.status == 400


def test_a_node_without_room_refuses_with_both_figures(src_cache, rig, monkeypatch):
    _, rx, _ = rig

    class Usage:
        free = 5 << 30
        total = 100 << 30

    monkeypatch.setattr(receiver_mod.shutil, "disk_usage", lambda path: Usage)
    with pytest.raises(TransferRefused) as caught:
        rx.open(protocol.build_manifest(src_cache).to_json())
    assert caught.value.status == 507
    assert "GiB free" in caught.value.sentence


def test_a_cancelled_copy_leaves_no_session_and_no_partials(src_cache, rig):
    dst, rx, http = rig
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(TransferCancelled):
        _send(src_cache, http, cancel=cancel)
    assert rx._sessions == {}
    assert _partials(dst) == []


def test_link_addresses_reads_every_lit_physical_port_with_its_speed_and_rdma():
    files = {
        "/sys/class/net/enp1s0f0np0/device/uevent": "DRIVER=mlx5_core\n",
        "/sys/class/net/enp1s0f0np0/device/infiniband/rocep1s0f0/ports": "",
        "/sys/class/net/enp1s0f0np0/carrier": "1\n",
        "/sys/class/net/enp1s0f0np0/speed": "200000\n",
        "/sys/class/net/enP2p1s0f1np1/device/uevent": "DRIVER=mlx5_core\n",
        "/sys/class/net/enP2p1s0f1np1/carrier": "0\n",
        "/sys/class/net/ens1f0/device/uevent": "DRIVER=ice\n",  # another vendor
        "/sys/class/net/ens1f0/carrier": "1\n",
        "/sys/class/net/ens1f0/speed": "100000\n",
        "/sys/class/net/docker0/carrier": "1\n",  # virtual: no device
    }
    runner = FakeRunner(files=files, outputs={
        "enp1s0f0np0": "3: enp1s0f0np0    inet 10.100.0.2/30 brd 10.100.0.3 scope global\n"
                       "3: enp1s0f0np0    inet6 fe80::4ebb:47ff:fe80:26b0/64 scope link\n",
        "ens1f0": "4: ens1f0    inet 10.9.0.2/24 brd 10.9.0.255 scope global\n",
        "docker0": "5: docker0    inet 172.17.0.1/16 scope global\n",
    })
    assert link_addresses(runner) == (
        LinkAddress("enp1s0f0np0", "10.100.0.2", 30, 200_000, True),
        LinkAddress("enp1s0f0np0", "fe80::4ebb:47ff:fe80:26b0", 64, 200_000, True),
        LinkAddress("ens1f0", "10.9.0.2", 24, 100_000, False),
    )


def _sparks():
    """The two Sparks as they are today: no shared IPv4 subnet on the rails
    (a stale /30 here), link-local on everything, a 10 GbE LAN."""
    here = (
        LinkAddress("enP2p1s0f0np0", "fe80::26b4", 64, 200_000, True),
        LinkAddress("enP7s7", "192.168.0.172", 24, 10_000, False),
        LinkAddress("enP7s7", "fe80::305e", 64, 10_000, False),
        LinkAddress("enp1s0f0np0", "10.100.0.2", 30, 200_000, True),
        LinkAddress("enp1s0f0np0", "fe80::26b0", 64, 200_000, True),
    )
    there = (
        LinkAddress("enP2p1s0f0np0", "fe80::4d3d", 64, 200_000, True),
        LinkAddress("enP7s7", "192.168.0.71", 24, 10_000, False),
        LinkAddress("enP7s7", "fe80::71", 64, 10_000, False),
        LinkAddress("enp1s0f0np0", "10.100.152.1", 24, 200_000, True),
        LinkAddress("enp1s0f0np0", "fe80::4d39", 64, 200_000, True),
    )
    return here, there


def test_with_no_netplan_the_rails_are_found_by_link_local_and_paired_rail_with_rail():
    here, there = _sparks()
    chosen = fastest_tier(candidate_paths(here, there))
    assert [(p.local_iface, p.remote_iface, p.dial) for p in chosen] == [
        ("enP2p1s0f0np0", "enP2p1s0f0np0", "fe80::4d3d%enP2p1s0f0np0"),
        ("enp1s0f0np0", "enp1s0f0np0", "fe80::4d39%enp1s0f0np0"),
    ]
    assert describe(chosen) == "RoCE, 2 × 200 Gb/s"


def test_the_lan_carries_the_copy_only_when_the_rails_do_not_answer():
    here, there = _sparks()
    reachable = [p for p in candidate_paths(here, there) if p.local_iface == "enP7s7"]
    chosen = fastest_tier(reachable)
    assert [p.dial for p in chosen] == ["192.168.0.71"]  # subnet before link-local
    assert describe(chosen) == "enP7s7, 10 Gb/s"


def test_a_fast_card_from_another_vendor_beats_the_lan():
    here = (LinkAddress("ens1f0", "10.9.0.2", 24, 100_000, False),
            LinkAddress("eno1", "192.168.0.10", 24, 2_500, False))
    there = (LinkAddress("ens1f0", "10.9.0.3", 24, 100_000, False),
             LinkAddress("eno1", "192.168.0.11", 24, 2_500, False))
    chosen = fastest_tier(candidate_paths(here, there))
    assert [(p.source, p.dial) for p in chosen] == [("10.9.0.2", "10.9.0.3")]
    assert describe(chosen) == "ens1f0, 100 Gb/s"


def test_two_lan_ports_in_one_subnet_the_faster_one_carries_it():
    here = (LinkAddress("eno1", "192.168.0.10", 24, 2_500, False),
            LinkAddress("enp5s0", "192.168.0.20", 24, 10_000, False))
    there = (LinkAddress("enp5s0", "192.168.0.71", 24, 10_000, False),)
    chosen = fastest_tier(candidate_paths(here, there))
    assert [(p.local_iface, p.source) for p in chosen] == [("enp5s0", "192.168.0.20")]


def test_a_candidate_that_does_not_answer_is_not_a_path(monkeypatch):
    monkeypatch.setattr(sender_mod, "_PROBE_TIMEOUT_S", 0.3)
    with socket.create_server(("127.0.0.1", 0)) as listener:
        port = listener.getsockname()[1]
        live = LinkPath("lo", "lo", None, "127.0.0.1", 1000, False)
        dead = LinkPath("x", "y", None, "192.0.2.1", 200_000, True)  # TEST-NET: never answers
        assert sender_mod._reachable([dead, live], port) == [live]


# -- the coordinator's job registry -----------------------------------------


class _Registry:
    def __init__(self, *, url="http://peer:18081", token="t"):
        self.url, self.token = url, token

    def get_node(self, node_id):
        return object() if node_id in ("spark-26af", "spark-4d38") else None

    def agent_url(self, node_id):
        return self.url

    def agent_token(self, node_id):
        return self.token


def _replicator(tmp_path, registry=None, cache=True):
    gate = threading.Event()

    def send(source, **kw):
        # Like the real sender: a set cancel ends the stream promptly.
        while not (gate.is_set() or kw["cancel"].is_set()):
            gate.wait(0.01)
        raise TransferCancelled("cancelled")

    root = tmp_path / "hub"
    (root / FOLDER).mkdir(parents=True)
    rep = Replicator(
        registry=registry or _Registry(), local_node_id="spark-26af",
        cache_dir=(lambda: root) if cache else (lambda: None), send=send,
    )
    return rep, gate


@pytest.mark.parametrize(
    "args, registry, cache, status, code",
    [
        (("models--../x", "spark-4d38"), None, True, 400, "invalid_request"),
        ((FOLDER, "spark-4d38", 0), None, True, 400, "invalid_request"),
        ((FOLDER, "spark-4d38", 33), None, True, 400, "invalid_request"),
        ((FOLDER, "spark-26af"), None, True, 400, "invalid_request"),
        ((FOLDER, "spark-nope"), None, True, 404, "unknown_node"),
        ((FOLDER, "spark-4d38"), None, False, 404, "model_not_cached"),
        (("models--org--absent", "spark-4d38"), None, True, 404, "model_not_cached"),
        ((FOLDER, "spark-4d38"), _Registry(url=None), True, 404, "node_agent_unreachable"),
        ((FOLDER, "spark-4d38"), _Registry(token=None), True, 503, "cluster_token_unavailable"),
    ],
)
def test_replicator_refusals(tmp_path, args, registry, cache, status, code):
    rep, gate = _replicator(tmp_path, registry, cache)
    folder, node, *streams = args
    with pytest.raises(ReplicateRefused) as caught:
        rep.start(folder, node, **({"streams": streams[0]} if streams else {}))
    assert (caught.value.status, caught.value.code) == (status, code)
    gate.set()


def test_starting_the_same_copy_twice_returns_the_running_job(tmp_path):
    rep, gate = _replicator(tmp_path)
    first = rep.start(FOLDER, "spark-4d38")
    assert rep.start(FOLDER, "spark-4d38")["transfer_id"] == first["transfer_id"]
    assert first["repo_id"] == "org/m" and first["status"] == "running"
    assert rep.cancel(first["transfer_id"]) is True
    assert rep.get(first["transfer_id"]) is None
    gate.set()
    rep.shutdown()


def test_a_job_is_pinned_to_the_revision_it_started_from(tmp_path):
    rep, gate = _replicator(tmp_path)
    repo = tmp_path / "hub" / FOLDER
    (repo / "refs").mkdir()
    (repo / "refs" / "main").write_text("c" * 40 + "\n")
    (repo / "snapshots" / ("c" * 40)).mkdir(parents=True)
    (repo / "snapshots" / ("b" * 40)).mkdir()
    job = rep.start(FOLDER, "spark-4d38")
    assert job["revision"] == "c" * 40
    assert job["snapshots"] == ["b" * 40, "c" * 40]
    rep.cancel(job["transfer_id"])
    gate.set()
    rep.shutdown()
    # A folder with no refs/main is pinned to nothing, never to a guess.
    rep2, gate2 = _replicator(tmp_path / "bare")
    assert rep2.start(FOLDER, "spark-4d38")["revision"] is None
    gate2.set()
    rep2.shutdown()


def test_a_failed_send_is_an_error_row_naming_the_node(tmp_path):
    root = tmp_path / "hub"
    (root / FOLDER).mkdir(parents=True)

    def send(source, **kw):
        kw["on_route"]("management", ["default->peer"], "the management network")
        raise TransferError("the node refused a chunk of blobs/x")

    rep = Replicator(registry=_Registry(), local_node_id="spark-26af", cache_dir=lambda: root, send=send)
    job = rep.start(FOLDER, "spark-4d38")
    final = rep.wait(job["transfer_id"], on_update=lambda j: None, poll_s=0.01)
    assert final["status"] == "error"
    assert final["error"] == "spark-4d38: the node refused a chunk of blobs/x"
    assert final["path"] == "management"


def _phase_replicator(tmp_path, *, verify: bool):
    """A send that streams until cancelled, or reaches verifying and waits."""
    root = tmp_path / "hub"
    (root / FOLDER).mkdir(parents=True)
    gate, discarded = threading.Event(), []

    def send(source, **kw):
        if verify:
            kw["on_phase"]("verifying")
            gate.wait(5)
            return None
        while not kw["cancel"].is_set():
            time.sleep(0.01)
        time.sleep(0.2)  # the DELETE of the node's session takes a moment
        discarded.append(True)
        raise TransferCancelled("cancelled")

    rep = Replicator(registry=_Registry(), local_node_id="spark-26af", cache_dir=lambda: root, send=send)
    return rep, gate, discarded


def test_a_copy_being_verified_cannot_be_cancelled_and_stays_listed(tmp_path):
    rep, gate, _ = _phase_replicator(tmp_path, verify=True)
    tid = rep.start(FOLDER, "spark-4d38")["transfer_id"]
    assert _wait_until(lambda: rep.get(tid)["status"] == "verifying")
    with pytest.raises(CancelRefused) as caught:
        rep.cancel(tid)
    assert caught.value.sentence == (
        "the copy is being verified on spark-4d38; it finishes on its own and cannot be stopped now"
    )
    assert [j["transfer_id"] for j in rep.list()] == [tid]
    gate.set()
    assert rep.wait(tid, on_update=lambda j: None, poll_s=0.01)["status"] == "done"


def test_cancelling_a_running_copy_waits_for_its_session_to_be_discarded(tmp_path):
    rep, _, discarded = _phase_replicator(tmp_path, verify=False)
    tid = rep.start(FOLDER, "spark-4d38")["transfer_id"]
    assert rep.cancel(tid) is True
    assert discarded == [True]  # the sender finished its DELETE before cancel returned
    assert rep.get(tid) is None and rep.list() == []


def test_a_cancel_racing_the_end_of_streaming_still_wins(tmp_path):
    """cancel lands after the streams finish but before verifying is claimed:
    on_phase refuses to start verifying, so the sender discards the session."""
    root = tmp_path / "hub"
    (root / FOLDER).mkdir(parents=True)
    raised = []

    def send(source, **kw):
        kw["cancel"].set()
        try:
            kw["on_phase"]("verifying")
        except TransferCancelled:
            raised.append(True)
            raise

    rep = Replicator(registry=_Registry(), local_node_id="spark-26af", cache_dir=lambda: root, send=send)
    tid = rep.start(FOLDER, "spark-4d38")["transfer_id"]
    assert _wait_until(lambda: raised)
    assert rep.get(tid)["status"] == "running"


def _wait_until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_a_stream_keeps_only_a_few_source_files_open(tmp_path, rig, monkeypatch):
    dst, rx, http = rig
    repo = tmp_path / "many" / FOLDER
    (repo / "blobs").mkdir(parents=True)
    for i in range(12):
        data = os.urandom(1000 + i)
        (repo / "blobs" / hashlib.sha256(data).hexdigest()).write_bytes(data)
    live: set[int] = set()
    peak = [0]
    real_open = open

    class _Tracked:
        def __init__(self, fh):
            self._fh = fh
            live.add(id(self))
            peak[0] = max(peak[0], len(live))

        def __getattr__(self, name):
            return getattr(self._fh, name)

        def close(self):
            live.discard(id(self))
            self._fh.close()

    monkeypatch.setattr(
        sender_mod, "open", lambda *a, **k: _Tracked(real_open(*a, **k)), raising=False
    )
    result = send_folder(
        repo, agent_url="http://testserver", token="tok", http=http,
        local_addresses=lambda: LOOPBACK, streams=1, cancel=threading.Event(),
        on_progress=lambda done, total: None,
    )
    assert result.files == 12
    assert peak[0] <= sender_mod._OPEN_PER_STREAM
    assert live == set()


# -- routes ------------------------------------------------------------------


@pytest.mark.raw_http
def test_agent_copy_routes_need_the_token_and_a_receiver():
    agent = NodeAgent(profile(), token="tok")
    http = TestClient(create_agent_app(agent))
    assert http.post("/agent/transfers", json={}).status_code == 403
    assert http.post("/agent/transfers/x/commit").status_code == 403
    assert http.delete("/agent/transfers/x").status_code == 403
    r = http.post("/agent/transfers", json={}, headers={"X-Derate-Token": "tok"})
    assert r.status_code == 503
    assert r.json()["detail"] == "this node is not accepting copies: port 47101 could not be opened"


class _FakeTransfers:
    def __init__(self):
        self.jobs: dict[str, dict] = {}

    def start(self, folder, node_id, *, streams):
        if not protocol.FOLDER_RE.match(folder or ""):
            raise ReplicateRefused(400, "invalid_request", "folder must be a model cache folder.")
        if node_id == "ghost":
            raise ReplicateRefused(404, "unknown_node", "No node 'ghost'.")
        job = {
            "transfer_id": "tr-1", "folder": folder, "repo_id": "org/m", "node_id": node_id,
            "streams": streams, "path": "fabric", "routes": [], "link": "RoCE, 2 × 200 Gb/s",
            "total": 10, "completed": 4,
            "status": "running", "error": None, "started_at": 1.0, "finished_at": None,
            "rate_bps": None, "eta_s": None,
        }
        self.jobs[job["transfer_id"]] = job
        return job

    def list(self):
        return list(self.jobs.values())

    def cancel(self, transfer_id):
        if transfer_id == "tr-verifying":
            raise CancelRefused("the copy is being verified on spark-4d38; it finishes on its own")
        return self.jobs.pop(transfer_id, None) is not None

    def shutdown(self):
        pass


def test_gateway_copy_routes_and_activity_rows():
    from control_plane.gateway import create_app
    from tests.unit.test_gateway import GIB, _pull_setup

    deps, _, _ = _pull_setup(size=0, free_bytes=GIB)
    deps.transfers = _FakeTransfers()
    with TestClient(create_app(deps)) as client:
        r = client.post("/api/transfers", json={"folder": FOLDER, "node_id": "spark-4d38"})
        assert r.status_code == 202, r.text
        assert r.json()["streams"] == protocol.DEFAULT_STREAMS
        rows = [d for d in client.get("/api/activity").json()["downloads"] if d["kind"] == "transfer"]
        assert rows and rows[0]["transfer_id"] == "tr-1"
        assert rows[0]["provider"] == "copy to spark-4d38 over RoCE, 2 × 200 Gb/s"
        assert client.get("/api/transfers").json()["transfers"][0]["transfer_id"] == "tr-1"
        assert client.post("/api/transfers", json={"folder": "x", "node_id": "n"}).status_code == 400
        assert client.post("/api/transfers", json={"folder": FOLDER}).status_code == 400
        ghost = client.post("/api/transfers", json={"folder": FOLDER, "node_id": "ghost"})
        assert ghost.status_code == 404 and ghost.json()["error"]["code"] == "unknown_node"
        assert client.delete("/api/transfers/tr-1").status_code == 204
        gone = client.delete("/api/transfers/tr-1")
        assert gone.status_code == 404 and gone.json()["error"]["code"] == "transfer_not_found"
        busy = client.delete("/api/transfers/tr-verifying")
        assert busy.status_code == 409 and busy.json()["error"]["code"] == "transfer_verifying"
        assert "finishes on its own" in busy.json()["error"]["message"]


def test_a_gateway_without_a_replicator_says_so():
    from control_plane.gateway import create_app
    from tests.unit.test_gateway import GIB, _pull_setup

    deps, _, _ = _pull_setup(size=0, free_bytes=GIB)
    with TestClient(create_app(deps)) as client:
        r = client.post("/api/transfers", json={"folder": FOLDER, "node_id": "spark-4d38"})
        assert r.status_code == 503 and r.json()["error"]["code"] == "transfers_unavailable"
