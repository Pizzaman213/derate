"""The coordinator's copies in flight: one job per (folder, node).

The same shape as `deploy/prefetch.py::Prefetcher` -- a job dict whose
underscore keys are private, a short linger for finished jobs, and a rate over
the last few samples -- but each job is a thread rather than a subprocess:
`sendfile` spends its time in the kernel, so there is no interpreter to free.
Copies only go one way, out of the coordinator's own cache.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from control_plane.registry import modelcache

from .protocol import DEFAULT_STREAMS, FOLDER_RE, MAX_STREAMS
from .sender import TransferCancelled, send_folder

log = logging.getLogger(__name__)

#: How long a finished job stays listed -- the same linger as prefetch.
_DONE_LINGER_S = 20.0
_SAMPLE_EVERY_S = 0.5


class ReplicateRefused(Exception):
    def __init__(self, status: int, code: str, sentence: str) -> None:
        super().__init__(sentence)
        self.status = status
        self.code = code
        self.sentence = sentence


class CancelRefused(Exception):
    """The copy is committing on the node and finishes whatever is asked."""

    def __init__(self, sentence: str) -> None:
        super().__init__(sentence)
        self.sentence = sentence


def _public(job: dict) -> dict:
    return {k: (list(v) if isinstance(v, list) else v) for k, v in job.items() if not k.startswith("_")}


class Replicator:
    def __init__(
        self,
        *,
        registry: Any,
        local_node_id: str,
        cache_dir: Callable[[], Path | None] = modelcache.cache_dir,
        send: Callable[..., Any] = send_folder,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._registry = registry
        self.local_node_id = local_node_id
        self._cache_dir = cache_dir
        self._send = send
        self._clock = clock
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}

    def start(self, folder: str, node_id: str, *, streams: int = DEFAULT_STREAMS) -> dict:
        if not isinstance(folder, str) or not FOLDER_RE.match(folder):
            raise ReplicateRefused(
                400, "invalid_request", "folder must be a model cache folder, models--owner--name."
            )
        if not isinstance(streams, int) or isinstance(streams, bool) or not 1 <= streams <= MAX_STREAMS:
            raise ReplicateRefused(
                400, "invalid_request", f"streams must be an integer from 1 to {MAX_STREAMS}."
            )
        if node_id == self.local_node_id:
            raise ReplicateRefused(
                400, "invalid_request", "The coordinator already has this cache; pick another node."
            )
        if self._registry.get_node(node_id) is None:
            raise ReplicateRefused(404, "unknown_node", f"No node '{node_id}'.")
        cache = self._cache_dir()
        if cache is None or not (Path(cache) / folder).is_dir():
            raise ReplicateRefused(
                404, "model_not_cached", f"{folder} is not in the coordinator's cache at {cache}."
            )
        agent_url = self._registry.agent_url(node_id)
        if agent_url is None:
            raise ReplicateRefused(
                404, "node_agent_unreachable", f"No agent address is known for {node_id}."
            )
        token = self._registry.agent_token(node_id)
        if token is None:
            raise ReplicateRefused(
                503, "cluster_token_unavailable", f"No agent credential is held for {node_id}."
            )
        with self._lock:
            for job in self._jobs.values():
                if job["folder"] != folder or job["node_id"] != node_id:
                    continue
                if job["status"] in ("running", "verifying"):
                    return _public(job)
                if job["status"] == "cancelling":
                    raise ReplicateRefused(
                        409,
                        "transfer_cancelling",
                        f"A cancelled copy of {folder} to {node_id} is still stopping; "
                        "try again in a moment.",
                    )
            pinned = modelcache.revision(Path(cache) / folder)
            transfer_id = "tr-" + secrets.token_hex(6)
            cancel = threading.Event()
            job = {
                "transfer_id": transfer_id,
                "folder": folder,
                "repo_id": modelcache.repo_from_folder(folder),
                "node_id": node_id,
                "streams": streams,
                # What this copy is pinned to, read when it starts: the commit
                # in `refs/main` and the snapshot ids beside it. A node whose
                # copy names another commit holds an older revision.
                "revision": pinned["commit"],
                "snapshots": pinned["snapshots"],
                "path": None,
                "routes": [],
                # What a person reads: "RoCE, 2 × 200 Gb/s", "enP7s7, 10 Gb/s".
                "link": None,
                "total": None,
                "completed": 0,
                "status": "running",
                "error": None,
                "started_at": self._clock(),
                "finished_at": None,
                "rate_bps": None,
                "eta_s": None,
                "_cancel": cancel,
                "_samples": deque(maxlen=5),
                "_last_sample": 0.0,
                "_thread": None,
            }
            thread = threading.Thread(
                target=self._run,
                args=(job, Path(cache) / folder, agent_url, token),
                name=f"derate-transfer-{transfer_id}",
                daemon=True,
            )
            job["_thread"] = thread
            self._jobs[transfer_id] = job
            public = _public(job)
        thread.start()
        return public

    def get(self, transfer_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(transfer_id)
            return _public(job) if job else None

    def wait(
        self, transfer_id: str, on_update: Callable[[dict], None], poll_s: float = 1.0
    ) -> dict:
        while True:
            job = self.get(transfer_id)
            if job is None:
                return {"status": "cancelled"}
            on_update(job)
            if job["status"] in ("done", "error"):
                return job
            time.sleep(poll_s)

    def cancel(self, transfer_id: str, *, join_s: float = 15.0) -> bool:
        """Stop a running copy and wait for its session to be discarded.

        Raises `CancelRefused` while verifying: the node is already committing
        and the sender is inside that request, so a cancel could only hide a
        copy that still lands. The job stays registered as `cancelling` until
        its worker exits -- the worker's exit removes it -- so a replacement
        cannot start beside a sender that is still writing to the node.
        """
        with self._lock:
            job = self._jobs.get(transfer_id)
            if job is None:
                return False
            if job["status"] == "cancelling":
                return True
            if job["status"] == "verifying":
                raise CancelRefused(
                    f"the copy is being verified on {job['node_id']}; it finishes on its own "
                    "and cannot be stopped now"
                )
            job["_cancel"].set()
            thread = job["_thread"]
            if thread is None or not thread.is_alive():
                del self._jobs[transfer_id]
                return True
            job["status"] = "cancelling"
            job["rate_bps"] = job["eta_s"] = None
        if thread is not threading.current_thread():
            thread.join(timeout=join_s)
        return True

    def list(self) -> list[dict]:
        now = self._clock()
        with self._lock:
            for tid, job in list(self._jobs.items()):
                if (
                    job["status"] == "done"
                    and job["finished_at"] is not None
                    and now - job["finished_at"] > _DONE_LINGER_S
                ):
                    del self._jobs[tid]
            return [_public(j) for j in sorted(self._jobs.values(), key=lambda j: j["started_at"])]

    def shutdown(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
            self._jobs.clear()
        for job in jobs:
            job["_cancel"].set()
        for job in jobs:
            thread = job["_thread"]
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=5.0)

    # -- the job thread ------------------------------------------------------

    def _run(self, job: dict, source: Path, agent_url: str, token: str) -> None:
        try:
            self._run_send(job, source, agent_url, token)
        finally:
            with self._lock:
                if job["status"] == "cancelling" and self._jobs.get(job["transfer_id"]) is job:
                    del self._jobs[job["transfer_id"]]

    def _run_send(self, job: dict, source: Path, agent_url: str, token: str) -> None:
        def on_progress(completed: int, total: int) -> None:
            with self._lock:
                job["completed"] = completed
                job["total"] = total
                self._sample(job, force=completed >= total)

        def on_phase(phase: str) -> None:
            with self._lock:
                if phase == "verifying" and job["status"] in ("running", "cancelling"):
                    if job["_cancel"].is_set():
                        raise TransferCancelled("cancelled")
                    job["status"] = "verifying"
                    job["rate_bps"] = job["eta_s"] = None

        def on_route(path: str, routes: list[str], link: str) -> None:
            with self._lock:
                job["path"] = path
                job["routes"] = list(routes)
                job["link"] = link

        try:
            result = self._send(
                source,
                agent_url=agent_url,
                token=token,
                streams=job["streams"],
                cancel=job["_cancel"],
                on_progress=on_progress,
                on_phase=on_phase,
                on_route=on_route,
            )
        except TransferCancelled:
            return
        except Exception as exc:  # noqa: BLE001 - a job's failure is its status
            log.warning("copy of %s to %s failed: %s", job["folder"], job["node_id"], exc)
            with self._lock:
                if job["status"] == "cancelling":
                    return
                job["status"] = "error"
                job["error"] = f"{job['node_id']}: {exc}"
                job["finished_at"] = self._clock()
            return
        with self._lock:
            if job["status"] == "cancelling":
                return
            job["path"] = getattr(result, "path", job["path"])
            job["routes"] = list(getattr(result, "routes", job["routes"]))
            job["link"] = getattr(result, "link", job["link"])
            if job["total"] is None:
                job["total"] = job["completed"]
            job["status"] = "done"
            job["finished_at"] = self._clock()
            job["eta_s"] = 0.0

    def _sample(self, job: dict, *, force: bool = False) -> None:
        """Rate over the last few samples, ETA from it. Null until two samples. Lock held."""
        now = self._clock()
        if not force and now - job["_last_sample"] < _SAMPLE_EVERY_S:
            return
        job["_last_sample"] = now
        samples = job["_samples"]
        samples.append((now, job["completed"]))
        if len(samples) < 2:
            return
        (t0, b0), (t1, b1) = samples[0], samples[-1]
        if t1 <= t0 or b1 < b0:
            return
        rate = (b1 - b0) / (t1 - t0)
        job["rate_bps"] = rate
        total = job["total"]
        if total is not None and rate > 0:
            job["eta_s"] = max(0.0, (total - job["completed"]) / rate)
