"""Extra cache folders (DERATE_EXTRA_CACHE_DIRS): a copy that does not fit in the
hub lands in a folder beneath the HuggingFace root and is linked back with a
relative symlink, which the scan, the delete and the receiver all recognise as
derate's own -- and no other symlink."""

from __future__ import annotations

import os
from collections import namedtuple
from pathlib import Path

import pytest

from control_plane.registry import modelcache
from control_plane.transfer import receiver as receiver_mod
from control_plane.transfer.protocol import build_manifest
from control_plane.transfer.receiver import TransferRefused
from tests.unit.test_transfer import FOLDER, _partials, _send, _tree, src_cache  # noqa: F401

Usage = namedtuple("Usage", "total used free")
GiB = 1 << 30


@pytest.fixture()
def hf(tmp_path, monkeypatch):
    """`<root>/hub` is the cache, `<root>/extra` the configured extra folder."""
    root = tmp_path / "hf"
    hub = root / "hub"
    extra = root / "extra"
    hub.mkdir(parents=True)
    extra.mkdir()
    monkeypatch.setenv(modelcache.EXTRA_DIRS_ENV, str(extra))
    return root, hub, extra


def _room(monkeypatch, *, hub_free: int, extra_free: int, extra: Path) -> None:
    """The hub's filesystem has `hub_free`, the extra folder's `extra_free`."""
    real = extra.resolve()

    def usage(path):
        p = Path(path).resolve()
        free = extra_free if p == real or p.is_relative_to(real) else hub_free
        return Usage(1000 * GiB, 0, free)

    monkeypatch.setattr(receiver_mod.shutil, "disk_usage", usage)


@pytest.fixture()
def xrig(hf, tmp_path, monkeypatch):
    """The shared copy rig with its receiver pointed at `hf`'s hub."""
    _, hub, _ = hf
    from tests.unit.test_agent_tokens import profile
    from fastapi.testclient import TestClient
    from control_plane.registry.agent import NodeAgent, create_agent_app
    from control_plane.transfer import protocol
    from control_plane.transfer.receiver import start_transfer_receiver
    from tests.unit.test_transfer import LOOPBACK

    monkeypatch.setattr(protocol, "CHUNK_BYTES", 1 << 20)
    rx = start_transfer_receiver(host="127.0.0.1", port=0, cache_dir=lambda: hub,
                                 addresses=lambda: LOOPBACK)
    agent = NodeAgent(profile(), token="tok")
    agent.transfer_receiver = rx
    yield rx, TestClient(create_agent_app(agent))
    rx.close()


# -- which folders are usable --------------------------------------------------


def test_only_a_directory_beneath_the_hf_root_and_outside_the_hub_is_used(tmp_path):
    root = tmp_path / "hf"
    hub = root / "hub"
    good = root / "ssd"
    outside = tmp_path / "mnt"
    inside_hub = hub / "more"
    sneaky = root / "sneaky"  # beneath the root by name, outside it once resolved
    for d in (good, outside, inside_hub):
        d.mkdir(parents=True)
    sneaky.symlink_to(outside)
    raw = ":".join(map(str, [good, outside, inside_hub, sneaky, root, "relative/x", root / "absent", good]))

    dirs, refused = modelcache.extra_cache_dirs(hub, raw)

    assert dirs == (good.resolve(),)  # and listed once though given twice
    joined = "\n".join(refused)
    assert f"{outside} in DERATE_EXTRA_CACHE_DIRS is not beneath {root}" in joined
    assert f"{sneaky} in DERATE_EXTRA_CACHE_DIRS is not beneath {root}" in joined
    assert f"{root} in DERATE_EXTRA_CACHE_DIRS is not beneath" in joined
    assert "is the hub cache or inside it" in joined
    assert "relative/x in DERATE_EXTRA_CACHE_DIRS is not an absolute path" in joined
    assert "does not exist on this node" in joined
    assert len(refused) == 6


def test_unset_means_no_extra_folders(tmp_path, monkeypatch):
    monkeypatch.delenv(modelcache.EXTRA_DIRS_ENV, raising=False)
    assert modelcache.extra_cache_dirs(tmp_path / "hub") == ((), ())


# -- where a copy lands ----------------------------------------------------------


def test_a_copy_lands_in_the_hub_whenever_the_hub_has_room(src_cache, hf, xrig, monkeypatch):
    _, hub, extra = hf
    _room(monkeypatch, hub_free=500 * GiB, extra_free=900 * GiB, extra=extra)
    _send(src_cache, xrig[1])
    assert (hub / FOLDER).is_dir() and not (hub / FOLDER).is_symlink()
    assert not (extra / FOLDER).exists()


def test_a_copy_the_hub_cannot_hold_lands_in_the_extra_folder_behind_a_relative_link(
    src_cache, hf, xrig, monkeypatch
):
    root, hub, extra = hf
    _room(monkeypatch, hub_free=5 * GiB, extra_free=900 * GiB, extra=extra)
    rx, http = xrig

    _send(src_cache, http)

    link = hub / FOLDER
    assert link.is_symlink()
    assert os.readlink(link) == f"../extra/{FOLDER}"  # relative: same on host and in the container
    assert link.resolve().is_relative_to(root.resolve())
    assert (extra / FOLDER).is_dir() and not (extra / FOLDER).is_symlink()
    assert _tree(extra / FOLDER) == _tree(src_cache)
    assert _partials(root) == []
    # The capacity route plans against the roomiest folder, and lists both.
    cap = rx.capacity()
    assert cap["free"] == 900 * GiB
    assert {d["kind"]: d["free"] for d in cap["dirs"]} == {"default": 5 * GiB, "extra": 900 * GiB}
    assert cap["refused"] == []
    # A second copy fills the linked folder where it is: only the metadata,
    # which is never skipped on size, is sent again.
    again = _send(src_cache, http)
    assert again.bytes_sent == len("abc") + len("[]") and link.is_symlink()


def test_capacity_for_a_folder_names_the_filesystem_it_would_land_on(hf, xrig, monkeypatch):
    _, hub, extra = hf
    _room(monkeypatch, hub_free=50 * GiB, extra_free=900 * GiB, extra=extra)
    rx, http = xrig

    def cap(**params):
        r = http.get("/agent/transfers/capacity", params=params, headers={"X-Derate-Token": "tok"})
        assert r.status_code == 200, r.text
        return r.json()

    # A new folder: the hub while it has room for the copy, else the extra folder.
    small = cap(folder=FOLDER, bytes=GiB)
    assert (small["landing"], small["free"]) == (str(hub), 50 * GiB)
    big = cap(folder=FOLDER, bytes=100 * GiB)
    assert (big["landing"], big["free"]) == (str(extra.resolve()), 900 * GiB)
    assert big["dirs"] == small["dirs"] and len(big["dirs"]) == 2
    # Without a folder the answer is unchanged: the roomiest folder, no `landing`.
    plain = cap()
    assert plain["free"] == 900 * GiB and "landing" not in plain
    # A folder already in the hub is filled there, however large the copy.
    (hub / "models--org--inhub").mkdir()
    assert cap(folder="models--org--inhub", bytes=100 * GiB)["landing"] == str(hub)
    # ...and one already linked into the extra folder stays there, however small.
    (extra / FOLDER).mkdir()
    (hub / FOLDER).symlink_to(f"../extra/{FOLDER}")
    linked = cap(folder=FOLDER, bytes=0)
    assert (linked["landing"], linked["free"]) == (str(extra.resolve()), 900 * GiB)


def test_capacity_and_open_pick_the_same_extra_folder_holding_a_partial_copy(
    src_cache, hf, xrig, monkeypatch
):
    """An interrupted copy left the big blob in the extra folder. Only the
    remainder fits anywhere, so both answers must credit what is already there
    and name the extra folder -- capacity used to charge the whole copy, find
    room nowhere and name the hub while `open` landed in the extra folder."""
    _, hub, extra = hf
    rx, _ = xrig
    manifest = build_manifest(src_cache)
    total = sum(f.size for f in manifest.files)
    big = next(f for f in manifest.files if f.size == 3 << 20)
    (extra / FOLDER / "blobs").mkdir(parents=True)
    (extra / FOLDER / big.path).write_bytes((src_cache / big.path).read_bytes())
    room = receiver_mod.FREE_SPACE_RESERVE + total - (1 << 20)  # the remainder fits, the whole does not
    _room(monkeypatch, hub_free=room, extra_free=room, extra=extra)

    cap = rx.capacity(FOLDER, total)
    assert cap["landing"] == str(extra.resolve())
    opened = rx.open(manifest.to_json())
    assert rx._sessions[opened["session_id"]].dest.parent == extra.resolve()
    assert rx.abort(opened["session_id"])


def test_with_no_room_anywhere_the_refusal_names_every_folder(src_cache, hf, xrig, monkeypatch):
    _, hub, extra = hf
    _room(monkeypatch, hub_free=5 * GiB, extra_free=6 * GiB, extra=extra)
    with pytest.raises(TransferRefused) as caught:
        xrig[0].open(build_manifest(src_cache).to_json())
    assert caught.value.status == 507
    assert f"{hub} has 5.0 GiB free" in caught.value.sentence
    assert f"no extra cache folder has room either ({extra.resolve()}: 6.0 GiB free)" in caught.value.sentence
    assert not (hub / FOLDER).exists()


def test_a_link_into_an_unconfigured_folder_is_still_refused(src_cache, hf, xrig):
    root, hub, _ = hf
    (root / "elsewhere" / FOLDER).mkdir(parents=True)
    (hub / FOLDER).symlink_to(f"../elsewhere/{FOLDER}")
    with pytest.raises(TransferRefused) as caught:
        xrig[0].open(build_manifest(src_cache).to_json())
    assert caught.value.status == 400
    assert caught.value.sentence == f"{FOLDER} is a symlink here"


def test_an_absolute_link_into_the_extra_folder_is_not_ours(src_cache, hf, xrig):
    _, hub, extra = hf
    (extra / FOLDER).mkdir()
    (hub / FOLDER).symlink_to((extra / FOLDER).resolve())
    with pytest.raises(TransferRefused) as caught:
        xrig[0].open(build_manifest(src_cache).to_json())
    assert caught.value.sentence == f"{FOLDER} is a symlink here"


def test_our_link_to_an_unmounted_disk_is_refused_rather_than_refilled(src_cache, hf, xrig):
    _, hub, _ = hf
    (hub / FOLDER).symlink_to(f"../extra/{FOLDER}")  # ours, but the folder is gone
    with pytest.raises(TransferRefused) as caught:
        xrig[0].open(build_manifest(src_cache).to_json())
    assert caught.value.status == 409
    assert caught.value.sentence.endswith("which is not there; is the disk it lives on mounted?")


# -- the scan and the delete -------------------------------------------------------


def _blob(repo: Path, name: str, size: int) -> None:
    (repo / "blobs").mkdir(parents=True)
    (repo / "blobs" / name).write_bytes(b"x" * size)
    (repo / "snapshots" / "abc").mkdir(parents=True)


def test_the_scan_measures_through_our_link_and_skips_any_other(hf, tmp_path):
    root, hub, extra = hf
    _blob(extra / "models--org--linked", "a", 4096)
    (hub / "models--org--linked").symlink_to("../extra/models--org--linked")
    _blob(root / "elsewhere" / "models--org--foreign", "b", 8192)
    (hub / "models--org--foreign").symlink_to("../elsewhere/models--org--foreign")

    repos = {r["folder"]: r["bytes"] for r in modelcache.scan(hub)["repos"]}

    assert repos == {"models--org--linked": 4096}


def test_delete_removes_our_folder_and_its_link(hf):
    _, hub, extra = hf
    _blob(extra / "models--org--linked", "a", 4096)
    (hub / "models--org--linked").symlink_to("../extra/models--org--linked")

    out = modelcache.delete("models--org--linked", hub)

    assert out == {"deleted": True, "folder": "models--org--linked", "repo_id": "org/linked",
                   "bytes_freed": 4096}
    assert not os.path.lexists(hub / "models--org--linked")
    assert not (extra / "models--org--linked").exists()
    assert extra.is_dir()  # the extra folder itself is never the target


def test_delete_through_a_link_is_bounded_to_the_configured_extra_folders(hf, monkeypatch):
    root, hub, extra = hf
    _blob(extra / "models--org--linked", "a", 4096)
    (hub / "models--org--linked").symlink_to("../extra/models--org--linked")
    _blob(root / "elsewhere" / "models--org--foreign", "b", 10)
    (hub / "models--org--foreign").symlink_to("../elsewhere/models--org--foreign")

    with pytest.raises(modelcache.DeleteRefused, match="not directly inside the model cache"):
        modelcache.delete("models--org--foreign", hub)
    assert (root / "elsewhere" / "models--org--foreign" / "blobs" / "b").exists()

    # Once the folder is no longer configured, its link is no longer ours either.
    monkeypatch.delenv(modelcache.EXTRA_DIRS_ENV)
    with pytest.raises(modelcache.DeleteRefused):
        modelcache.delete("models--org--linked", hub)
    assert (extra / "models--org--linked" / "blobs" / "a").exists()
