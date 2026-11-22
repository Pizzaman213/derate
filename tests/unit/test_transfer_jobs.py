"""Replicator cancel: a job whose worker outlives the join stays `cancelling`."""
from __future__ import annotations

import threading
import time

import pytest

from control_plane.transfer.jobs import Replicator, ReplicateRefused
from control_plane.transfer.sender import TransferCancelled
from tests.unit.test_transfer import FOLDER, _Registry


def _stubborn(tmp_path):
    release = threading.Event()
    started = threading.Event()

    def send(source, **kw):
        started.set()
        release.wait(10)  # ignores the cancel event until released
        raise TransferCancelled("cancelled")

    root = tmp_path / "hub"
    (root / FOLDER).mkdir(parents=True)
    rep = Replicator(registry=_Registry(), local_node_id="spark-26af",
                     cache_dir=lambda: root, send=send)
    return rep, release, started


def test_cancel_keeps_a_live_worker_registered_as_cancelling(tmp_path):
    rep, release, started = _stubborn(tmp_path)
    job = rep.start(FOLDER, "spark-4d38")
    assert started.wait(5)
    assert rep.cancel(job["transfer_id"], join_s=0.05) is True
    assert rep.get(job["transfer_id"])["status"] == "cancelling"
    assert [j["status"] for j in rep.list()] == ["cancelling"]
    with pytest.raises(ReplicateRefused) as e:
        rep.start(FOLDER, "spark-4d38")
    assert (e.value.status, e.value.code) == (409, "transfer_cancelling")
    assert "still stopping" in e.value.sentence
    t0 = time.monotonic()
    assert rep.cancel(job["transfer_id"], join_s=5.0) is True  # no second wait
    assert time.monotonic() - t0 < 1.0
    release.set()
    deadline = time.monotonic() + 5
    while rep.get(job["transfer_id"]) is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert rep.get(job["transfer_id"]) is None
    again = rep.start(FOLDER, "spark-4d38")
    assert again["transfer_id"] != job["transfer_id"]
    rep.shutdown()
