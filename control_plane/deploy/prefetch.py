"""Weight prefetch: download a model without launching it, pausably.

sparkrun downloads on the coordinator host, into the coordinator's hub cache,
and then distributes to peers -- and it always calls ``snapshot_download``,
which skips complete blobs. So a prefetch runs HERE, into that same directory,
and the next launch's download is a no-op for whatever already arrived.

Each job is one ``prefetch_worker`` process in its own session, so pause is a
signal to its process group and resume is a second spawn: the hub library
resumes ``.incomplete`` blobs. Progress is read off the cache folder's
``blobs/`` (the measure ``registry/modelcache.py`` already uses), never from the
worker, so the figure is what is on disk.

Process-local and not persisted, like provider pulls: a record that survived a
restart would describe a process that no longer exists.
"""

from __future__ import annotations

import logging
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from control_plane.deploy.prefetch_worker import ERROR_PREFIX
from control_plane.deploy.sparkrun import sparkrun_env
from control_plane.registry import modelcache

log = logging.getLogger(__name__)

#: How long a finished job stays listed -- the same linger as provider pulls.
_DONE_LINGER_S = 20.0
#: Grace between SIGTERM and SIGKILL on pause.
_TERM_GRACE_S = 5.0
WORKER_MODULE = "control_plane.deploy.prefetch_worker"


def launch_cache_dir() -> Path:
    """Where a launch downloads: the hub library's own resolution in this
    process's environment, which is the environment sparkrun inherits."""
    from huggingface_hub import constants

    return Path(constants.HF_HUB_CACHE)


class Prefetcher:
    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        measured_dir: Path | None | type(...) = ...,
        worker_module: str = WORKER_MODULE,
        poll_s: float = 1.0,
        env: dict[str, str] | None = None,
    ) -> None:
        self._cache_dir = cache_dir
        self._measured_dir = measured_dir
        self._worker_module = worker_module
        self._poll_s = poll_s
        self._env = env
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._stderr: dict[str, object] = {}
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    # -- paths ---------------------------------------------------------------

    def _dirs(self) -> tuple[Path, Path | None]:
        launch = self._cache_dir or launch_cache_dir()
        measured = modelcache.cache_dir() if self._measured_dir is ... else self._measured_dir
        return launch, measured

    def _resolve_cache(self) -> Path:
        launch, measured = self._dirs()
        if measured is not None and Path(measured).resolve() != Path(launch).resolve():
            raise ValueError(
                f"derate measures the cache at {measured} but launches download into "
                f"{launch}; set DERATE_HF_CACHE to match before prefetching."
            )
        return Path(launch)

    # -- public --------------------------------------------------------------

    def start(
        self,
        repo_id: str,
        *,
        revision: str | None = None,
        patterns: tuple[str, ...] | list[str] = (),
        total_bytes: int | None,
    ) -> dict:
        cache = self._resolve_cache()
        with self._lock:
            for job in self._jobs.values():
                if job["repo_id"] == repo_id and job["status"] in ("running", "paused"):
                    return dict(job)
            download_id = "dl-" + secrets.token_hex(6)
            job = {
                "download_id": download_id,
                "repo_id": repo_id,
                "revision": revision,
                "patterns": list(patterns),
                "total": total_bytes,
                "completed": self._measure(cache, repo_id),
                "status": "running",
                "error": None,
                "started_at": time.time(),
                "finished_at": None,
                "rate_bps": None,
                "eta_s": None,
                "_cache": str(cache),
                "_samples": deque(maxlen=5),
            }
            self._jobs[download_id] = job
            self._spawn(download_id)
            self._ensure_thread()
            return _public(job)

    def pause(self, download_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(download_id)
            if job is None:
                return None
            if job["status"] == "running":
                self._kill(download_id)
                job["status"] = "paused"
                _reset_rate(job)
            return _public(job)

    def resume(self, download_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(download_id)
            if job is None:
                return None
            if job["status"] in ("paused", "error"):
                job["status"] = "running"
                job["error"] = None
                job["finished_at"] = None
                _reset_rate(job)
                self._spawn(download_id)
                self._ensure_thread()
            return _public(job)

    def cancel(self, download_id: str) -> bool:
        """Stop and forget. Never deletes blobs: that is Delete from disk."""
        with self._lock:
            if download_id not in self._jobs:
                return False
            self._kill(download_id)
            del self._jobs[download_id]
            return True

    def get(self, download_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(download_id)
            return _public(job) if job else None

    def list(self) -> list[dict]:
        now = time.time()
        with self._lock:
            for did, job in list(self._jobs.items()):
                if job["finished_at"] is not None and job["status"] == "done" \
                        and now - job["finished_at"] > _DONE_LINGER_S:
                    del self._jobs[did]
            return [_public(j) for j in sorted(self._jobs.values(), key=lambda j: j["started_at"])]

    def shutdown(self) -> None:
        self._stopping.set()
        with self._lock:
            for did in list(self._procs):
                self._kill(did, grace=0.5)

    # -- internals (lock held) -----------------------------------------------

    def _spawn(self, download_id: str) -> None:
        job = self._jobs[download_id]
        argv = [sys.executable, "-m", self._worker_module, "--repo", job["repo_id"],
                "--cache-dir", job["_cache"]]
        if job["revision"]:
            argv += ["--revision", job["revision"]]
        for p in job["patterns"]:
            argv += ["--pattern", p]
        env = dict(self._env) if self._env is not None else sparkrun_env()
        # The worker imports control_plane, so it needs this checkout on its path.
        root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        self._procs[download_id] = subprocess.Popen(
            argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, start_new_session=True, text=True,
        )

    def _kill(self, download_id: str, grace: float = _TERM_GRACE_S) -> None:
        proc = self._procs.pop(download_id, None)
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        if proc.stderr:
            proc.stderr.close()

    @staticmethod
    def _measure(cache: Path, repo_id: str) -> int:
        return modelcache._blob_bytes(
            cache / modelcache.folder_for(repo_id), follow_symlinks=True
        )[0]

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="prefetch-poll", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stopping.wait(self._poll_s):
            if not self.tick():
                return

    def tick(self) -> bool:
        """One poll. True while anything is still running."""
        with self._lock:
            running = False
            for did, job in self._jobs.items():
                if job["status"] != "running":
                    continue
                job["completed"] = self._measure(Path(job["_cache"]), job["repo_id"])
                _sample_rate(job, time.time())
                proc = self._procs.get(did)
                code = proc.poll() if proc else 1
                if code is None:
                    running = True
                    continue
                err = proc.stderr.read() if proc and proc.stderr else ""
                if proc and proc.stderr:
                    proc.stderr.close()
                self._procs.pop(did, None)
                job["finished_at"] = time.time()
                if code == 0:
                    job["status"] = "done"
                else:
                    job["status"] = "error"
                    line = next((ln for ln in (err or "").splitlines()
                                 if ln.startswith(ERROR_PREFIX)), None)
                    job["error"] = (line[len(ERROR_PREFIX):] if line
                                    else f"the download process exited with status {code}")
            return running


def _reset_rate(job: dict) -> None:
    job["_samples"].clear()
    job["rate_bps"] = None
    job["eta_s"] = None


def _sample_rate(job: dict, now: float) -> None:
    """Rate over the last few polls; ETA from it. Null until two samples."""
    samples = job["_samples"]
    samples.append((now, job["completed"]))
    job["rate_bps"] = job["eta_s"] = None
    if len(samples) < 2:
        return
    (t0, b0), (t1, b1) = samples[0], samples[-1]
    if t1 <= t0 or b1 <= b0:
        return
    rate = (b1 - b0) / (t1 - t0)
    job["rate_bps"] = rate
    total = job["total"]
    if total and total > 0:
        job["eta_s"] = max(0.0, (total - job["completed"]) / rate)


def _public(job: dict) -> dict:
    return {k: (list(v) if isinstance(v, list) else v) for k, v in job.items() if not k.startswith("_")}


_DEFAULT: Prefetcher | None = None
_DEFAULT_LOCK = threading.Lock()


def default() -> Prefetcher:
    """The coordinator's one registry."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = Prefetcher()
        return _DEFAULT


def shutdown_default() -> None:
    if _DEFAULT is not None:
        _DEFAULT.shutdown()
