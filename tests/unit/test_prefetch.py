"""Hub weight prefetch: a real subprocess per job, paused by signal."""

from __future__ import annotations

import os
import textwrap
import time
from pathlib import Path

import pytest

from control_plane.deploy.prefetch import Prefetcher
from control_plane.registry import modelcache

REPO = "acme/tiny"

FAKE_WORKER = textwrap.dedent('''
    import argparse, os, sys, time
    p = argparse.ArgumentParser()
    p.add_argument("--repo"); p.add_argument("--revision"); p.add_argument("--cache-dir")
    p.add_argument("--pattern", action="append", default=[])
    a = p.parse_args()
    if os.environ.get("FAKE_FAIL"):
        print("derate-prefetch-error: repo is gated", file=sys.stderr); sys.exit(1)
    blobs = os.path.join(a.cache_dir, "models--" + a.repo.replace("/", "--"), "blobs")
    os.makedirs(blobs, exist_ok=True)
    chunks = int(os.environ.get("FAKE_CHUNKS", "4"))
    for i in range(chunks):
        path = os.path.join(blobs, "b%d" % i)
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(b"x" * 1000)
            time.sleep(float(os.environ.get("FAKE_SLEEP", "0.3")))
    sys.exit(0)
''')


@pytest.fixture
def worker_dir(tmp_path: Path) -> Path:
    d = tmp_path / "mods"
    d.mkdir()
    (d / "fake_prefetch_worker.py").write_text(FAKE_WORKER)
    return d


def _make(tmp_path: Path, worker_dir: Path, **env) -> Prefetcher:
    cache = tmp_path / "hub"
    cache.mkdir(exist_ok=True)
    return Prefetcher(
        cache_dir=cache, measured_dir=cache, worker_module="fake_prefetch_worker",
        poll_s=0.05, env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(worker_dir), **env},
    )


def _wait(pred, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_rate_and_eta_come_from_successive_ticks_and_pause_clears_them(tmp_path, monkeypatch):
    from collections import deque

    import control_plane.deploy.prefetch as prefetch

    cache = tmp_path / "hub"
    cache.mkdir()
    p = Prefetcher(cache_dir=cache, measured_dir=cache)
    sizes = iter([0, 100])
    clock = iter([1000.0, 1001.0])
    monkeypatch.setattr(Prefetcher, "_measure", staticmethod(lambda c, r: next(sizes)))
    monkeypatch.setattr(prefetch.time, "time", lambda: next(clock))
    p._jobs["dl-x"] = {
        "download_id": "dl-x", "repo_id": REPO, "total": 1100, "completed": 0,
        "status": "running", "error": None, "finished_at": None,
        "rate_bps": None, "eta_s": None, "_cache": str(cache), "_samples": deque(maxlen=5),
    }

    class _Live:
        stderr = None

        def poll(self):
            return None

    p._procs["dl-x"] = _Live()
    p.tick()
    assert p._jobs["dl-x"]["rate_bps"] is None  # one sample is not a rate
    p.tick()
    assert p._jobs["dl-x"]["rate_bps"] == 100
    assert p._jobs["dl-x"]["eta_s"] == 10

    p._procs.pop("dl-x")
    job = p.pause("dl-x")
    assert job["rate_bps"] is None and job["eta_s"] is None
    assert not p._jobs["dl-x"]["_samples"]


def _job(p: Prefetcher, did: str) -> dict:
    return next(j for j in p.list() if j["download_id"] == did)


def test_start_progress_pause_resume_done(tmp_path, worker_dir):
    p = _make(tmp_path, worker_dir, FAKE_CHUNKS="8", FAKE_SLEEP="0.4")
    try:
        job = p.start(REPO, patterns=["*.safetensors"], total_bytes=8000)
        did = job["download_id"]
        assert did.startswith("dl-") and job["status"] == "running"
        assert _wait(lambda: _job(p, did)["completed"] >= 1000)

        proc = p._procs[did]
        paused = p.pause(did)
        assert paused["status"] == "paused"
        assert proc.poll() is not None, "pause must end the worker process"
        frozen = _job(p, did)["completed"]
        time.sleep(0.3)
        assert _job(p, did)["completed"] == frozen and _job(p, did)["status"] == "paused"

        # Starting the same repo again hands back the paused job.
        assert p.start(REPO, total_bytes=8000)["download_id"] == did

        assert p.resume(did)["status"] == "running"
        assert _wait(lambda: _job(p, did)["status"] == "done")
        done = _job(p, did)
        assert done["completed"] == 8000 and done["error"] is None
        assert done["finished_at"] is not None
    finally:
        p.shutdown()


def test_worker_error_line_becomes_the_error(tmp_path, worker_dir):
    p = _make(tmp_path, worker_dir, FAKE_FAIL="1")
    try:
        did = p.start(REPO, total_bytes=None)["download_id"]
        assert _wait(lambda: _job(p, did)["status"] == "error")
        assert _job(p, did)["error"] == "repo is gated"
    finally:
        p.shutdown()


def test_cancel_removes_the_record_and_keeps_blobs(tmp_path, worker_dir):
    p = _make(tmp_path, worker_dir, FAKE_CHUNKS="50", FAKE_SLEEP="0.2")
    try:
        did = p.start(REPO, total_bytes=50000)["download_id"]
        assert _wait(lambda: _job(p, did)["completed"] > 0)
        proc = p._procs[did]
        assert p.cancel(did) is True
        assert proc.poll() is not None
        assert all(j["download_id"] != did for j in p.list())
        blobs = tmp_path / "hub" / modelcache.folder_for(REPO) / "blobs"
        assert any(blobs.iterdir())
        assert p.cancel(did) is False
        assert p.pause(did) is None and p.resume(did) is None
    finally:
        p.shutdown()


def test_cache_dir_mismatch_refuses(tmp_path):
    p = Prefetcher(cache_dir=tmp_path / "a", measured_dir=tmp_path / "b")
    with pytest.raises(ValueError, match="set DERATE_HF_CACHE to match"):
        p.start(REPO, total_bytes=1)
    assert p.list() == []


# -- routes ------------------------------------------------------------------


class _FakeRegistry:
    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.started: list[tuple] = []

    def start(self, repo_id, *, revision=None, patterns=(), total_bytes):
        self.started.append((repo_id, tuple(patterns), total_bytes))
        job = {"download_id": "dl-abc", "repo_id": repo_id, "revision": revision,
               "patterns": list(patterns), "total": total_bytes, "completed": 10,
               "status": "running", "error": None, "started_at": 1.0, "finished_at": None}
        self.jobs[job["download_id"]] = job
        return dict(job)

    def _set(self, did, status):
        if did not in self.jobs:
            return None
        self.jobs[did]["status"] = status
        return dict(self.jobs[did])

    def pause(self, did):
        return self._set(did, "paused")

    def resume(self, did):
        return self._set(did, "running")

    def cancel(self, did):
        return self.jobs.pop(did, None) is not None

    def list(self):
        return [dict(j) for j in self.jobs.values()]

    def shutdown(self):
        pass


def test_download_routes_and_activity_rows(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane.deploy import prefetch
    from control_plane.gateway import create_app, internal_api
    from control_plane.resolver.hf import ModelInfo
    from tests.unit.test_gateway import GIB, _pull_setup

    fake = _FakeRegistry()
    monkeypatch.setattr(prefetch, "_DEFAULT", fake)
    internal_api._DOWNLOADS.clear()
    deps, _, _ = _pull_setup(size=0, free_bytes=GIB)

    class _Client:
        def model_info(self, model_id, revision="main"):
            return ModelInfo(model_id=model_id, sha="s", siblings=(), safetensors=None, gguf=None, tags=(), file_sizes={
                "model.safetensors": 1000, "config.json": 10, "q4.gguf": 500, "README.md": 7,
            })

    monkeypatch.setattr(deps.resolver, "client", _Client(), raising=False)
    with TestClient(create_app(deps)) as client:
        assert client.post("/api/downloads", json={"model_id": "not a repo"}).status_code == 400
        r = client.post("/api/downloads", json={"model_id": "acme/tiny"})
        assert r.status_code == 202, r.text
        assert fake.started[-1][2] == 1010
        r = client.post("/api/downloads", json={"model_id": "acme/tiny", "gguf_file": "q4.gguf"})
        assert fake.started[-1] == ("acme/tiny", ("q4.gguf",), 500)

        assert client.post("/api/downloads/dl-abc/pause").json()["status"] == "paused"
        rows = client.get("/api/activity").json()["downloads"]
        hub = [r for r in rows if r["kind"] == "hub"]
        assert hub and hub[0]["download_id"] == "dl-abc" and hub[0]["paused"] is True
        assert hub[0]["provider"] == "Hugging Face" and hub[0]["model"] == "acme/tiny"
        assert hub[0]["done"] is False
        assert client.post("/api/downloads/dl-abc/resume").json()["status"] == "running"
        assert client.delete("/api/downloads/dl-abc").status_code == 204
        missing = client.post("/api/downloads/dl-abc/pause")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "download_not_found"
