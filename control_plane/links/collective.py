"""Run a real NCCL collective between two nodes, and time it.

This is the measurement the ladder in `measure.py` has always wanted and never
had. `NcclMeasurer` is first in that ladder and its docstring calls itself "the
measurement that counts", but `available()` needs `mpirun`, `all_reduce_perf`
and `sendrecv_perf`, none of which are installed anywhere in this estate -- so
every link falls to the `ib_write_bw` rung, whose figure is raw RDMA scaled by
a constant, and whose latency is a one-sided 2-byte write timed by
`ib_write_lat`.

What runs here instead is a two-rank `torch.distributed.all_reduce` **inside
the serving image**, which needs no MPI and no nccl-tests. It is strictly
better evidence than either: it exercises the same NCCL build, the same
transport and the same rails that vLLM's own collectives will, so the number
describes the thing the planner is planning.

**The container has to match a real launch or the number is fiction.**
`--device /dev/infiniband` is not a tuning choice: without it NCCL reports
`NET/IB : No device found` and silently drops to its TCP transport over the
management LAN. Measured on this estate, that is 243 us for an 8 KB all-reduce
against 17 us over RoCE, and 0.12 GB/s against 18 at 4 MiB. sparkrun maps the
devices for a real derate launch (its rootless branch does; a live deployment
reads `Devices=[{/dev/infiniband /dev/infiniband rwm}] User=1000:1000`), so
anything measuring this fabric has to as well.

**And the bootstrap interface has to be named.** NCCL picks its out-of-band
address itself, and on this estate it picked `10.100.0.2` -- a stray /30 on the
peer's RoCE NIC that the coordinator has no route to -- and hung until the
timeout, which reads exactly like a fabric fault and is not one.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: Rendezvous port for the two ranks. Not a served port and not the agent's;
#: high and fixed so a firewall rule can name it.
DEFAULT_PORT = int(os.environ.get("DERATE_NCCL_PORT", "29555"))

#: How long one configuration gets before it is called a failure. A completing
#: run is seconds; this is the budget for a fabric that will not converge.
DEFAULT_TIMEOUT_S = 150.0

WARMUP, ITERS = 10, 50

#: The program each rank runs. `set_device` BEFORE `init_process_group`, which
#: is the order torch documents -- the other way round builds the communicator
#: before the rank has a current device and hangs on a bootstrap that never
#: completes.
_WORKER = r'''
import json, os, sys, time
import torch, torch.distributed as dist

sizes = [int(x) for x in sys.argv[1].split(",")]
warmup, iters = int(sys.argv[2]), int(sys.argv[3])

torch.cuda.set_device(0)
dist.init_process_group(backend="nccl")
rank = dist.get_rank()
n = dist.get_world_size()

def loaded_nccl():
    # torch.cuda.nccl.version() reports what torch was COMPILED against and
    # differs from what loads (2.29.7 vs 2.31.2 in this image). Only the live
    # handle describes the code that will actually run.
    try:
        import ctypes
        lib = ctypes.CDLL("libnccl.so.2")
        v = ctypes.c_int(); lib.ncclGetVersion(ctypes.byref(v)); m = v.value
        major = m // 10000 if m >= 20000 else m // 1000
        rest = m - major * (10000 if m >= 20000 else 1000)
        return "%d.%d.%d" % (major, rest // 100, rest % 100)
    except Exception:
        return None

out = {"rank": rank, "world": n, "nccl": loaded_nccl(), "rows": []}
for nbytes in sizes:
    t = torch.ones(max(1, nbytes // 2), dtype=torch.float16, device="cuda")
    for _ in range(warmup):
        dist.all_reduce(t)
    torch.cuda.synchronize()
    start = time.perf_counter()
    for _ in range(iters):
        dist.all_reduce(t)
    torch.cuda.synchronize()
    per = (time.perf_counter() - start) / iters
    # A ring all-reduce moves 2*S*(n-1)/n bytes per rank, which is the same
    # figure comm.py::tensor_bytes_per_step charges.
    moved = 2.0 * nbytes * (n - 1) / n
    out["rows"].append({"bytes": nbytes, "us": per * 1e6,
                        "busbw_gbps": (moved / per) / 1e9})
if rank == 0:
    print("__derate_collective " + json.dumps(out))
dist.destroy_process_group()
'''

_MARKER = "__derate_collective "

#: Failures worth naming rather than reporting as "no result". Each of these
#: has cost this project real time being mistaken for something else.
_SIGNATURES = (
    ("local access violation work queue error", "IB queue-pair fault"),
    ("No device found", "the container cannot see /dev/infiniband"),
    ("Network is unreachable", "NCCL chose an unroutable bootstrap address"),
    ("unhandled system error", "NCCL system error"),
    ("Multiple Ranks are using the same GPU", "both ranks landed on one GPU"),
)


@dataclass
class CollectiveResult:
    """One configuration, timed at several sizes, or the reason it did not run."""

    rows: list = field(default_factory=list)
    nccl: str | None = None
    error: str | None = None
    env: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.rows)

    def at(self, nbytes: int) -> dict | None:
        return next((r for r in self.rows if r["bytes"] == nbytes), None)


def docker_argv(image: str, env: dict, args: list[str]) -> list[str]:
    """The `docker run` for one rank. See the module docstring for the two
    flags that are not optional."""
    cmd = [
        "docker", "run", "--rm", "--gpus", "all", "--ipc=host", "--network", "host",
        "--device", "/dev/infiniband", "--cap-add", "IPC_LOCK",
    ]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    return cmd + ["--entrypoint", "python3", image, "-c", _WORKER] + args


def _parse(text: str) -> dict | None:
    for line in (text or "").splitlines():
        if line.startswith(_MARKER):
            try:
                return json.loads(line[len(_MARKER):])
            except ValueError:
                return None
    return None


def _classify(output: str) -> str:
    for needle, said in _SIGNATURES:
        if needle in output:
            return said
    return "the collective did not complete"


def run_collective(
    *,
    image: str,
    sizes: list,
    master_addr: str,
    peer_host: str,
    iface: str | None = None,
    env: dict | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    port: int = DEFAULT_PORT,
    ssh_opts: tuple = ("-o", "BatchMode=yes"),
) -> CollectiveResult:
    """Time an all_reduce between this host (rank 0) and *peer_host* (rank 1).

    *master_addr* must be an address the PEER can reach back on -- not
    `127.0.0.1`, which makes rank 1 dial its own loopback and both ranks wait
    out the timeout. There is no default for that reason.

    Never raises. A fabric that will not converge is a result, and it comes
    back as `error` with the signature named where one is recognised.
    """
    if not master_addr or master_addr.startswith("127."):
        return CollectiveResult(
            error="master_addr must be an address the peer can reach, not loopback",
            env=dict(env or {}),
        )

    base = {
        "MASTER_ADDR": master_addr,
        "MASTER_PORT": str(port),
        "WORLD_SIZE": "2",
        **(env or {}),
    }
    if iface:
        base["NCCL_SOCKET_IFNAME"] = iface
    args = [",".join(str(s) for s in sizes), str(WARMUP), str(ITERS)]

    procs, outs = [], []
    try:
        procs.append(subprocess.Popen(
            docker_argv(image, {**base, "RANK": "0"}, args),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True,
        ))
        remote = " ".join(
            shlex.quote(x) for x in docker_argv(image, {**base, "RANK": "1"}, args)
        )
        procs.append(subprocess.Popen(
            ["ssh", *ssh_opts, peer_host, remote],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True,
        ))

        deadline = time.monotonic() + timeout_s
        for p in procs:
            try:
                outs.append(p.communicate(timeout=max(1.0, deadline - time.monotonic()))[0])
            except subprocess.TimeoutExpired:
                p.kill()
                outs.append(p.communicate()[0] or "")
    except OSError as exc:
        return CollectiveResult(error=str(exc), env=dict(env or {}))
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()

    joined = "\n".join(o or "" for o in outs)
    blob = _parse(joined)
    if blob is None:
        return CollectiveResult(error=_classify(joined), env=dict(env or {}))
    return CollectiveResult(rows=blob["rows"], nccl=blob.get("nccl"), env=dict(env or {}))


def local_interface_for(address: str) -> str | None:
    """The interface carrying *address* on this host, or None.

    Read off the system rather than guessed, and it falls through rather than
    raising when the tool is absent -- the same rule the rest of the estate's
    host readers follow. None means "let NCCL choose", which is what happened
    before this existed and is not worse than it was.
    """
    try:
        out = subprocess.run(
            ["ip", "-o", "-4", "addr", "show"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[3].split("/")[0] == address:
            return parts[1]
    return None
