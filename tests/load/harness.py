"""Process orchestration and the ladder.

Three roles, on separate cores, because a load test whose driver and subject
share a core measures the wrong thing:

    core 0        this harness
    core 1        the gateway under test -- one process, one loop, as shipped
    cores 2-5     four fake runtimes
    cores 6-13    up to eight driver processes

Every rung runs twice: once straight at the fake runtimes and once through the
gateway. Driver cost is present in both, so the difference is the gateway's.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import httpx

from . import report
from .report import merge

class HarnessError(RuntimeError):
    """The harness could not establish or confirm its own preconditions."""


HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

GATEWAY_CORE = "1"
BACKEND_CORES = ["2", "3", "4", "5"]
DRIVER_CORES = ["6", "7", "8", "9", "10", "11", "12", "13"]

GATEWAY_PORT = 8099
BACKEND_PORT_BASE = 9101

# One driver process saturates well before the gateway does, so rungs above
# this rate are split across more of them.
RATE_PER_DRIVER = 2000.0


def _taskset(cores: str) -> list[str]:
    exe = shutil.which("taskset")
    return [exe, "-c", cores] if exe else []


# Where subprocess logs go. NEVER a pipe: a pipe nobody drains fills its 64 KB
# buffer and the next write() blocks forever -- inside the gateway's event
# loop, which then answers nothing at 0% CPU while still being alive. That is
# the harness wedging its own subject and then reporting it as a gateway
# failure, and it is exactly the class of lie this harness exists to catch.
LOG_DIR = os.environ.get("DERATE_LOAD_LOGS") or tempfile.mkdtemp(prefix="derate-load-logs-")


class Proc:
    def __init__(self, name: str, argv: list[str], cores: str) -> None:
        self.name = name
        self.argv = _taskset(cores) + argv
        self.popen: subprocess.Popen | None = None
        self.log_path = os.path.join(LOG_DIR, f"{name}.log")
        self._log = None

    def start(self) -> None:
        env = dict(os.environ, PYTHONPATH=REPO)
        self._log = open(self.log_path, "ab", buffering=0)
        self.popen = subprocess.Popen(
            self.argv,
            cwd=REPO,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=self._log,
        )

    def log_tail(self, limit: int = 4000) -> str:
        try:
            with open(self.log_path, "rb") as handle:
                return handle.read()[-limit:].decode("utf-8", "replace")
        except Exception:
            return ""

    def stop(self) -> None:
        if self.popen and self.popen.poll() is None:
            self.popen.send_signal(signal.SIGINT)
            try:
                self.popen.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.popen.kill()
                self.popen.wait(timeout=5)
        self.popen = None
        if self._log is not None:
            self._log.close()
            self._log = None

    @property
    def alive(self) -> bool:
        return bool(self.popen and self.popen.poll() is None)


class Cluster:
    """Fake runtimes plus the gateway, started and torn down together."""

    def __init__(
        self,
        *,
        backends: int = 4,
        tokens: int = 64,
        ttft: float = 0.0,
        itl: float = 0.0,
        settings: dict | None = None,
        max_seqs: int = 4096,
    ) -> None:
        self.n = backends
        self.tokens = tokens
        self.ttft = ttft
        self.itl = itl
        self.settings = settings or {}
        self.max_seqs = max_seqs
        self.backend_procs: list[Proc] = []
        self.gateway_proc: Proc | None = None
        self.client = httpx.Client(timeout=30.0)
        # Control-plane calls get their own short timeout so a saturated
        # component fails fast and visibly instead of hanging the harness.
        self._control = httpx.Client(timeout=8.0)

    @property
    def backend_ports(self) -> list[int]:
        return [BACKEND_PORT_BASE + i for i in range(self.n)]

    @property
    def backend_urls(self) -> list[str]:
        return [f"http://127.0.0.1:{p}/v1" for p in self.backend_ports]

    @property
    def gateway_url(self) -> str:
        return f"http://127.0.0.1:{GATEWAY_PORT}"

    def start(self) -> None:
        for i, port in enumerate(self.backend_ports):
            proc = Proc(
                f"backend-{i}",
                [
                    sys.executable, "-m", "tests.load.backend",
                    "--port", str(port),
                    "--tokens", str(self.tokens),
                    "--ttft", str(self.ttft),
                    "--itl", str(self.itl),
                ],
                BACKEND_CORES[i % len(BACKEND_CORES)],
            )
            proc.start()
            self.backend_procs.append(proc)

        self.gateway_proc = Proc(
            "gateway",
            [
                sys.executable, "-m", "tests.load.gateway_app",
                "--port", str(GATEWAY_PORT),
                "--backends", ",".join(self.backend_urls),
                "--max-seqs", str(self.max_seqs),
                "--settings", json.dumps(self.settings),
            ],
            GATEWAY_CORE,
        )
        self.gateway_proc.start()

        self._await(
            [f"http://127.0.0.1:{p}/health" for p in self.backend_ports]
            + [f"{self.gateway_url}/healthz"]
        )

    def _await(self, urls: list[str], timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        pending = list(urls)
        while pending and time.time() < deadline:
            still = []
            for url in pending:
                try:
                    if self.client.get(url).status_code == 200:
                        continue
                except Exception:
                    pass
                still.append(url)
            pending = still
            if pending:
                time.sleep(0.1)
        if pending:
            for proc in [*self.backend_procs, self.gateway_proc]:
                if proc is None:
                    continue
                err = proc.log_tail()
                if err.strip():
                    print(f"--- {proc.name} log ---\n{err}", file=sys.stderr)
            raise RuntimeError(f"did not come up: {pending}")

    def stop(self) -> None:
        if self.gateway_proc:
            self.gateway_proc.stop()
        for proc in self.backend_procs:
            proc.stop()
        self.backend_procs.clear()
        self.client.close()
        self._control.close()

    # -- runtime control ---------------------------------------------------

    def configure_backends(self, spec: dict, *, attempts: int = 5) -> None:
        """Set every fake runtime's knobs, and prove it took.

        This used to swallow every exception, which meant a probe could run its
        whole body against the previous probe's backends and report a verdict
        about conditions that were never established. A harness that cannot
        confirm its own preconditions cannot be trusted about anything else.
        """
        for port in self.backend_ports:
            url = f"http://127.0.0.1:{port}/control"
            last = None
            for attempt in range(attempts):
                try:
                    # Short, because a backend that cannot answer a control
                    # call promptly is itself the thing worth reporting.
                    response = self._control.post(url, json=spec)
                    response.raise_for_status()
                    echoed = response.json()
                    mismatch = {
                        key: (value, echoed.get(key))
                        for key, value in spec.items()
                        if key in echoed and echoed[key] != value
                    }
                    if mismatch:
                        last = f"backend did not take the setting: {mismatch}"
                    else:
                        break
                except Exception as exc:
                    last = f"{type(exc).__name__}: {exc}"
                time.sleep(0.5 * (attempt + 1))
            else:
                raise HarnessError(f"could not configure backend on {port}: {last}")

    def serves_ok(self, timeout: float = 20.0) -> bool:
        """One real request end to end. The only honest readiness check."""
        try:
            response = self._control.post(
                f"{self.gateway_url}/v1/chat/completions",
                json=_payload(stream=False),
                timeout=timeout,
            )
            return response.status_code == 200
        except Exception:
            return False

    def backend_stats(self) -> list[dict]:
        out = []
        for port in self.backend_ports:
            try:
                out.append(self.client.get(f"http://127.0.0.1:{port}/stats").json())
            except Exception:
                out.append({})
        return out

    def probe(self, reset: bool = False, timeout: float | None = None) -> dict:
        try:
            return self.client.get(
                f"{self.gateway_url}/loadprobe",
                params={"reset": int(reset)},
                timeout=timeout if timeout is not None else 30.0,
            ).json()
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    def pin_policy(self, policy: str = "least_outstanding") -> None:
        """Fix the routing policy so a rung is not silently comparing two.

        The auto policy can flip to WEIGHTED_CAPACITY once enough requests have
        completed for measured strength to diverge between replicas.
        """
        try:
            self.client.put(
                f"{self.gateway_url}/api/routing/loadtest", json={"policy": policy}
            )
        except Exception:
            pass


# --- running one rung -----------------------------------------------------


def _payload(stream: bool, prompt_chars: int = 64, max_tokens: int | None = None) -> dict:
    body = {
        "model": "loadtest",
        "messages": [{"role": "user", "content": "x" * prompt_chars}],
        "stream": stream,
    }
    if max_tokens:
        body["max_tokens"] = max_tokens
    return body


def run_drivers(jobs: list[dict]) -> list[dict]:
    """Fire K driver processes together and collect their results."""
    procs, paths = [], []
    with tempfile.TemporaryDirectory(prefix="derate-load-") as tmp:
        for i, job in enumerate(jobs):
            path = os.path.join(tmp, f"result-{i}.json")
            job = dict(job, out=path)
            spec = os.path.join(tmp, f"job-{i}.json")
            with open(spec, "w") as handle:
                json.dump(job, handle)
            proc = Proc(
                f"driver-{i}",
                [sys.executable, "-m", "tests.load.driver", spec],
                DRIVER_CORES[i % len(DRIVER_CORES)],
            )
            proc.start()
            procs.append(proc)
            paths.append(path)

        results = []
        for proc, path in zip(procs, paths):
            try:
                proc.popen.wait(timeout=600)
            except subprocess.TimeoutExpired:
                proc.popen.kill()
            if os.path.exists(path):
                with open(path) as handle:
                    results.append(json.load(handle))
            else:
                print(
                    f"  driver {proc.name} produced nothing: "
                    f"{proc.log_tail(2000).strip()[:400]}",
                    file=sys.stderr,
                )
        return results


def driver_count(rate: float) -> int:
    if rate <= 0:
        return 1
    return max(1, min(len(DRIVER_CORES), int(rate / RATE_PER_DRIVER) + 1))


# 28,231 ephemeral ports exist on this box and every driver connects to the
# same host:port, so the whole fleet shares that space. Capping per driver
# keeps the total under it even when the gateway slows and concurrency climbs.
MAX_CONNS_PER_DRIVER = 2048


def rps_jobs(url: str, rate: float, duration: float, k: int, **extra) -> list[dict]:
    conns = min(MAX_CONNS_PER_DRIVER, max(64, int(rate / k) + 64))
    return [
        {
            "mode": "rps",
            "url": url,
            "body": _payload(stream=False),
            "rate": rate / k,
            "duration_s": duration,
            "max_conns": conns,
            # Past the connection cap a request is recorded as never sent
            # rather than queued, because a queue here would be the harness
            # hiding the very backlog it is looking for.
            "inflight_cap": conns,
            "timeout_s": 30.0,
            **extra,
        }
        for _ in range(k)
    ]


def stream_jobs(url: str, concurrency: int, duration: float, k: int, **extra) -> list[dict]:
    per = max(1, concurrency // k)
    return [
        {
            "mode": "stream",
            "url": url,
            "body": _payload(stream=True),
            "concurrency": per,
            "duration_s": duration,
            "max_conns": per + 16,
            "timeout_s": 60.0,
            **extra,
        }
        for _ in range(k)
    ]


def spread(jobs: list[dict], urls: list[str]) -> list[dict]:
    """Point each driver at a different fake runtime for the direct A/B, so the
    baseline is not bottlenecked on one backend process."""
    out = []
    for i, job in enumerate(jobs):
        out.append(dict(job, url=urls[i % len(urls)] + "/chat/completions"))
    return out
