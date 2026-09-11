"""Time a real NCCL collective on this fabric, and sweep what might move it.

The planner's most decision-sensitive input is the cost of ONE cross-node
collective. Nothing in this project has ever measured one. `ib_write_bw` can
only offer `ib_write_lat` -- a one-sided 2-byte RDMA write, a different
operation -- so `links/measure.py` now reports `latency_us=None` from that rung
and `UNMEASURED_COLLECTIVE_LATENCY_US` (40 us, this hardware class's published
nccl-tests figure with GPUDirect RDMA off) is charged and labelled instead.

This is the tool that replaces the charge with a measurement.

    python3 -m tests.nccl_sweep --peer 192.168.0.172          # the real thing
    python3 -m tests.nccl_sweep --peer HOST --sizes 8,5760,65536
    python3 -m tests.nccl_sweep --peer HOST --sweep            # tuning knobs
    python3 -m tests.nccl_sweep --local                        # plumbing only

**Why it runs inside the serving image.** The abort this fabric shows --
`local access violation work queue error` on a two-rank all_reduce over IB --
was reproduced against the HOST's torch, which bundles NCCL 2.28.9. The
serving image loads **2.31.2** (read off libnccl.so.2 itself --
`torch.cuda.nccl.version()` reports 2.29.7 there, which is only what torch
was compiled against). That is a different NCCL, and whether the
failure reproduces there is unknown and cheap to find out. It is also the
build that actually serves, so it is the only one whose number means anything
for a plan: measuring a different NCCL than vLLM will run is the same class of
error as measuring a different operation.

**The two sizes that matter.** 8 bytes is the latency floor -- what
`latency_us` should be. The decode all-reduce is `hidden_size * 2 * batch`,
which is 5,760 B for gpt-oss-120b and 8,192 B for DeepSeek-V4-Flash at batch 1;
that is the size the TP/PP decision actually turns on. Everything above ~1 MB
is the prefill and weight-loading regime and is bandwidth-bound, which is the
regime derate is NOT short of.

**What the sweep is for, and what it must not become.** `deploy/flags.py`
ships `NCCL_TUNABLES` -- every variable this image's `libnccl.so.2` honours,
read off its own string table -- and `DERATE_NCCL_ENV` renders chosen ones into
a launch's `env:` block. It ships NO defaults, deliberately. NCCL already
selects a protocol by message size, so forcing `NCCL_PROTO=LL` would help the
few-KB decode collective and hurt the multi-MB prefill one; which way that
trade lands on this fabric is exactly what this measures. Put a value in
`flags.py` only with a row from here behind it.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field

DEFAULT_IMAGE = os.environ.get(
    "DERATE_VLLM_IMAGE", "ghcr.io/pizzaman213/derate/vllm-audio:latest"
)

#: 8 B is the latency floor. 5,760 and 8,192 are real decode all-reduce sizes
#: (gpt-oss-120b and DeepSeek-V4-Flash at batch 1). 64 KiB is roughly batch 8.
#: 4 MiB is the prefill regime, included so a tuning change that helps the
#: small sizes and wrecks the large ones cannot hide.
DEFAULT_SIZES = (8, 5760, 8192, 65536, 4 << 20)

#: One variable at a time against the default, because a sweep of combinations
#: on a fabric this slow to set up buys less than it costs. Values are the ones
#: NCCL documents for these knobs; the point of the run is to find out which,
#: not to assume.
SWEEP = (
    # First, because it is the only knob that shifts NCCL's own crossover
    # points rather than pinning a choice -- it is the cost model's fixed
    # per-network-operation term in MICROSECONDS, so raising it tells the
    # model what this fabric actually costs and lets it re-derive the
    # algorithm and protocol per size itself. That size-awareness is exactly
    # what `NCCL_ALGO`/`NCCL_PROTO` throw away: their grammar has no size
    # axis at all, and a size suffix parses clean while being silently
    # discarded. Values bracket the measured 13.36 us collective floor.
    ("NCCL_NET_OVERHEAD", ("1", "5", "13", "25", "50")),
    ("NCCL_PROTO", ("LL", "LL128", "Simple")),
    ("NCCL_ALGO", ("Ring", "Tree")),
    ("NCCL_MIN_NCHANNELS", ("1", "2", "4")),
    ("NCCL_MAX_NCHANNELS", ("1", "2", "4")),
    ("NCCL_IB_QPS_PER_CONNECTION", ("1", "2", "4")),
)

WARMUP = 20
ITERS = 200


# --------------------------------------------------------------------------
# the program that runs inside the container, on each rank
# --------------------------------------------------------------------------

WORKER = r'''
import json, os, sys, time
import torch, torch.distributed as dist

sizes = [int(x) for x in sys.argv[1].split(",")]
warmup, iters = int(sys.argv[2]), int(sys.argv[3])

# set_device BEFORE init_process_group, which is the order torch documents.
# The other way round hangs: the communicator is built before the rank has a
# current device, and NCCL waits on a bootstrap that never completes. Two
# two-node runs were lost to this and both looked exactly like a fabric fault.
torch.cuda.set_device(0)
dist.init_process_group(backend="nccl")
rank = dist.get_rank()

def _loaded_nccl():
    # torch.cuda.nccl.version() is what torch was COMPILED against and differs
    # from what is loaded (2.29.7 vs 2.31.2 in this image). ncclGetVersion on
    # the live handle is the only one that describes the code that will run.
    try:
        import ctypes
        lib = ctypes.CDLL("libnccl.so.2")
        v = ctypes.c_int()
        lib.ncclGetVersion(ctypes.byref(v))
        n = v.value
        # NCCL packs major*10000 + minor*100 + patch above 2.9, else *1000.
        major = n // 10000 if n >= 20000 else n // 1000
        rest = n - major * (10000 if n >= 20000 else 1000)
        return "%d.%d.%d" % (major, rest // 100, rest % 100)
    except Exception:
        return None

out = {"rank": rank, "world": dist.get_world_size(), "rows": [],
       "nccl": _loaded_nccl(),
       "nccl_torch_built_against": ".".join(map(str, torch.cuda.nccl.version()))}
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
    # busbw for a ring all-reduce: 2*S*(n-1)/n bytes cross the wire per rank,
    # which is the same figure comm.py::tensor_bytes_per_step charges.
    n = dist.get_world_size()
    moved = 2.0 * nbytes * (n - 1) / n
    out["rows"].append({"bytes": nbytes, "us": per * 1e6,
                        "busbw_gbps": (moved / per) / 1e9})
if rank == 0:
    print("__derate_nccl_result " + json.dumps(out))
dist.destroy_process_group()
'''


@dataclass
class Run:
    label: str
    env: dict = field(default_factory=dict)
    rows: list = field(default_factory=list)
    error: str | None = None
    nccl: str | None = None


def _docker(image: str, env: dict, args: list[str], *, host_net: bool) -> list[str]:
    # `--device /dev/infiniband` is what a REAL derate launch gets: sparkrun
    # runs rootless for derate and its rootless branch maps exactly this
    # (verified on a live deployment -- `sparkrun_<id>_solo` reads
    # `Devices=[{/dev/infiniband /dev/infiniband rwm}] User=1000:1000`).
    #
    # Without it NCCL reports `NET/IB : No device found` and drops to its TCP
    # transport over the management LAN with no warning. Measured here: a
    # 5,760-byte all-reduce costs 243 us that way and 18.68 us over RoCE, and
    # 4 MiB manages 0.12 GB/s against 4.87. A harness missing it measures a
    # fabric the engine will never use -- the same class of error as timing a
    # different operation, or a different NCCL build.
    cmd = ["docker", "run", "--rm", "--gpus", "all", "--ipc=host",
           "--device", "/dev/infiniband", "--cap-add", "IPC_LOCK"]
    if host_net:
        cmd += ["--network", "host"]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    cmd += ["--entrypoint", "python3", image, "-c", WORKER] + args
    return cmd


def _parse(out: str) -> dict | None:
    for line in out.splitlines():
        if line.startswith("__derate_nccl_result "):
            return json.loads(line.split(" ", 1)[1])
    return None


def measure(
    *, image: str, sizes, peer: str | None, iface: str | None,
    extra_env: dict, label: str, timeout: float, ssh: str,
) -> Run:
    """One two-rank all_reduce sweep over *sizes*. Rank 0 here, rank 1 on peer.

    `--local` runs both ranks on this box, which measures nothing about the
    fabric and is only there to prove the harness works before anyone spends a
    node on it.
    """
    run = Run(label=label, env=dict(extra_env))
    # Rank 0 is here, so the rendezvous address must be an address the PEER can
    # reach. 127.0.0.1 makes rank 1 dial its own loopback and both ranks wait
    # for each other until the timeout -- a hang that reads exactly like a
    # fabric fault and is not one. Refused rather than defaulted, because
    # guessing the right interface on a two-NIC box is how you measure the
    # wrong one.
    master = os.environ.get("DERATE_NCCL_MASTER") or ("127.0.0.1" if not peer else "")
    if peer and not master:
        raise SystemExit(
            "set DERATE_NCCL_MASTER to an address this host answers on that "
            "%s can reach (e.g. its LAN address); 127.0.0.1 would hang both "
            "ranks waiting for a rendezvous that never happens" % peer
        )
    base = {
        "MASTER_ADDR": master,
        "MASTER_PORT": os.environ.get("DERATE_NCCL_PORT", "29555"),
        "WORLD_SIZE": "2",
        **extra_env,
    }
    if iface:
        base["NCCL_SOCKET_IFNAME"] = iface
    if not peer:
        # Both ranks on one GB10. NCCL refuses that by default -- "Multiple
        # Ranks are using the same GPU/Partition" -- which is correct for a
        # real run and merely in the way for a plumbing check.
        base.setdefault("NCCL_MULTI_RANK_GPU_ENABLE", "1")
    args = [",".join(str(s) for s in sizes), str(WARMUP), str(ITERS)]

    procs = []
    try:
        local_env = {**base, "RANK": "0"}
        procs.append(
            subprocess.Popen(
                _docker(image, local_env, args, host_net=True),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
        )
        if peer:
            remote_env = {**base, "RANK": "1"}
            remote = " ".join(
                shlex.quote(x) for x in _docker(image, remote_env, args, host_net=True)
            )
            procs.append(
                subprocess.Popen(
                    ["ssh", "-o", "BatchMode=yes", *shlex.split(ssh), peer, remote],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                )
            )
        else:
            local2 = {**base, "RANK": "1"}
            procs.append(
                subprocess.Popen(
                    _docker(image, local2, args, host_net=True),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                )
            )

        outs = []
        deadline = time.time() + timeout
        for p in procs:
            try:
                outs.append(p.communicate(timeout=max(1.0, deadline - time.time()))[0])
            except subprocess.TimeoutExpired:
                p.kill()
                outs.append(p.communicate()[0] or "")
                run.error = "timed out after %.0fs" % timeout
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()

    blob = None
    for o in outs:
        blob = blob or _parse(o or "")
    if blob is None:
        joined = "\n".join(o or "" for o in outs)
        # The failure this whole exercise is shadowed by. Name it rather than
        # reporting "no result", so a fabric fault is never read as a harness
        # fault.
        for marker in (
            "local access violation work queue error",
            "unhandled system error",
            "unhandled cuda error",
            "NCCL WARN",
        ):
            if marker in joined:
                run.error = run.error or marker
                break
        run.error = run.error or "no result line; see --verbose"
        run.raw = joined  # type: ignore[attr-defined]
        return run
    run.rows = blob["rows"]
    run.nccl = blob.get("nccl")
    return run


def _print(run: Run) -> None:
    if run.error:
        print(f"  {run.label:34s} FAILED: {run.error}")
        return
    cells = " ".join(
        f"{r['bytes']:>8}B {r['us']:8.1f}us" for r in run.rows[:3]
    )
    print(f"  {run.label:34s} {cells}")


def benefit_report(baseline: Run, best_small: Run | None, decode_bytes: int = 5760) -> None:
    """What a measured collective is worth, through derate's OWN comm model.

    Two different benefits, reported apart because they are not the same kind
    of thing and conflating them would overstate both:

    **Correcting the charge.** `UNMEASURED_COLLECTIVE_LATENCY_US` is what the
    planner bills an exchange at when nothing has measured one. It does not
    make the hardware faster -- it changes which plan derate PICKS, and by how
    much it thinks TP costs. A charge that is too high refuses tensor parallel
    that would have worked.

    **Tuning.** A knob that lowers the measured time is a real speedup, and it
    is worth exactly the exchange count times the saving.

    Both are run through `comm.estimated_step_seconds`, so the tok/s figures
    are the planner's own arithmetic rather than a second model that could
    disagree with it.
    """
    if not baseline.rows:
        return
    try:
        from control_plane.contracts import UNMEASURED_COLLECTIVE_LATENCY_US
        from control_plane.planner import comm
        from tests.fixtures import MODEL_SHAPES, SPARK_01
    except Exception:
        return

    floor = baseline.rows[0]["us"]
    charged = float(UNMEASURED_COLLECTIVE_LATENCY_US)
    print("\n" + "=" * 70)
    print("WHAT THIS IS WORTH")
    print("=" * 70)
    print(f"\n1. The charge was wrong. derate bills {charged:.0f} us an exchange;")
    print(f"   this fabric costs {floor:.2f} us. Ratio {charged/floor:.2f}x.")
    print("   That does not speed anything up -- it changes what the planner picks.\n")

    shapes = [(n, MODEL_SHAPES[n]) for n in ("deepseek-v3", "llama-3.3-70b") if n in MODEL_SHAPES]
    for name, shape in shapes:
        ex = comm.tensor_exchanges_per_step(shape, 2)
        print(f"   {name} ({ex} exchanges/token):")
        for n in (2, 4, 8):
            cpt = comm.compute_seconds_per_step(shape, SPARK_01, n) * 1e3
            was, now = cpt + ex * charged * 1e-3, cpt + ex * floor * 1e-3
            print(f"      tp={n}: planner thought {1000/was:6.1f} tok/s, "
                  f"truth is {1000/now:6.1f} ({now/was - 1:+.1%} step)")
        print()

    if best_small is None or not best_small.rows or best_small is baseline:
        print("2. Tuning: no knob beat the default at the decode size.")
        print("   That is a result, not a gap -- NCCL's own selection is already")
        print("   right here, and a value shipped anyway would be noise.")
        return

    gain = baseline.rows[0]["us"] - best_small.rows[0]["us"]
    if gain <= 0:
        print("2. Tuning: nothing beat the default.")
        return
    print(f"2. Tuning: {best_small.label} saves {gain:.2f} us an exchange "
          f"({gain/baseline.rows[0]['us']:.1%}).")
    for name, shape in shapes:
        ex = comm.tensor_exchanges_per_step(shape, 2)
        for n in (2, 4, 8):
            cpt = comm.compute_seconds_per_step(shape, SPARK_01, n) * 1e3
            before = cpt + ex * baseline.rows[0]["us"] * 1e-3
            after = cpt + ex * best_small.rows[0]["us"] * 1e-3
            if n == 2:
                print(f"   {name}:")
            print(f"      tp={n}: {1000/before:6.1f} -> {1000/after:6.1f} tok/s "
                  f"({1000/after/(1000/before) - 1:+.1%})")


def _looks_like_address(peer: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(peer)
    except ValueError:
        return False
    return True


def _node_id_for(peer: str) -> str:
    """The node id a plan will name for this peer, from its address.

    The comment at the record-writing site has always said node ids and not
    addresses, and the line under it read ``a.dst_node or a.peer`` -- so a
    sweep launched the documented way (``--peer 192.168.0.172``) wrote every
    record under the address. Measured 2026-09-11: 80 rows, 20 environments,
    zero errors, and ``measurements.matching_nccl`` keys on sorted node ids,
    so ``manager._nccl_env_for`` could never read one of them. Among them
    ``NCCL_IB_QPS_PER_CONNECTION=1`` at 12.22 us against 15.40 default -- 21
    percent off the small-message floor, measured and unreachable.

    Reverse DNS, because this tool is standalone by design: it has no
    coordinator to ask and runs on a box that may not be one. On this estate
    192.168.0.172 resolves to ``spark-26af.localdomain``, whose first label is
    the node id exactly.

    A peer that is already a name is returned unchanged, and a lookup that
    fails returns the string it was given WITH A WARNING rather than guessing
    -- a record under the wrong name reads as "never measured", which is the
    failure this exists to stop, and inventing a name would make it worse.
    ``--dst-node`` overrides all of it.
    """
    import socket

    if not _looks_like_address(peer):
        return peer
    try:
        host, _aliases, _addrs = socket.gethostbyaddr(peer)
    except OSError:
        print(
            f"  WARNING: {peer} does not reverse-resolve, so records will be "
            f"stored under the address and no plan will ever match them. "
            f"Pass --dst-node <node id>."
        )
        return peer
    return host.split(".")[0]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--peer", help="host running rank 1; omit with --local")
    ap.add_argument("--local", action="store_true",
                    help="both ranks here: proves the harness, measures no fabric")
    ap.add_argument("--image", default=DEFAULT_IMAGE)
    ap.add_argument("--sizes", default=",".join(str(s) for s in DEFAULT_SIZES))
    ap.add_argument("--iface", help="NCCL_SOCKET_IFNAME for bootstrap")
    ap.add_argument("--sweep", action="store_true",
                    help="also try each NCCL_TUNABLES knob, one at a time")
    ap.add_argument("--ssh", default="", help="extra ssh options")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--json", help="write every row here")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--no-record", action="store_true",
                    help="measure but write no NcclRecord")
    ap.add_argument("--src-node", help="node id for this host (default: hostname)")
    ap.add_argument("--dst-node", help="node id for --peer (default: the peer string)")
    a = ap.parse_args(argv)

    if not a.peer and not a.local:
        ap.error("--peer HOST, or --local to check the harness only")
    sizes = [int(x) for x in a.sizes.split(",")]
    stamp = time.time()

    print(f"image {a.image}")
    print(f"ranks: this host + {a.peer or 'this host again (--local)'}")
    print(f"sizes: {sizes}\n")

    runs = [measure(image=a.image, sizes=sizes, peer=a.peer, iface=a.iface,
                    extra_env={}, label="defaults", timeout=a.timeout, ssh=a.ssh)]
    _print(runs[0])
    if runs[0].error:
        print("\nThe baseline did not complete, so no tuning row would mean")
        print("anything. If this is the IB abort, the fabric is the finding --")
        if runs[0].nccl:
            print("It ran NCCL %s inside the image, which is not the 2.28.9 the"
                  % runs[0].nccl)
            print("host's torch reproduced the abort with.")
        else:
            print("No rank reported a version, so this says nothing yet about")
            print("whether the image's NCCL behaves like the host's 2.28.9.")
        if a.verbose:
            print(getattr(runs[0], "raw", ""))
        return 1

    print(f"\nNCCL {runs[0].nccl} (loaded) completed. Full baseline:")
    for r in runs[0].rows:
        print(f"    {r['bytes']:>9} B  {r['us']:9.2f} us  {r['busbw_gbps']:7.2f} GB/s busbw")
    if not a.peer:
        # Both ranks contended for one GB10 and serialised on it. Printing
        # this as a collective latency would be inventing the very number the
        # rest of this project refuses to invent -- and it would look like a
        # measurement, which is worse than no number at all.
        print("\n  --local: BOTH RANKS SHARED ONE GPU, so these are contention")
        print("  times and say nothing about the fabric. The harness works;")
        print("  that is all this run establishes. Use --peer for a number.")
    else:
        floor = runs[0].rows[0]["us"]
        print(f"\n  -> the 8-byte time is the real `latency_us`: {floor:.2f} us")
        print("     against UNMEASURED_COLLECTIVE_LATENCY_US = 40 us charged today")
        print(f"     ({'confirms the charge' if abs(floor - 40) < 12 else 'the charge is wrong by %.0fx' % (max(floor, 40) / max(min(floor, 40), 1e-9))})")

    if a.sweep and not a.peer:
        print("\n--sweep needs --peer: tuning a fabric that is not in the")
        print("loop would rank noise.")
    elif a.sweep:
        print("\nOne knob at a time, against the default:")
        from control_plane.deploy.flags import NCCL_TUNABLES

        for name, values in SWEEP:
            if name not in NCCL_TUNABLES:
                print(f"  {name}: not honoured by this image, skipped")
                continue
            for value in values:
                run = measure(image=a.image, sizes=sizes, peer=a.peer,
                              iface=a.iface, extra_env={name: value},
                              label=f"{name}={value}", timeout=a.timeout, ssh=a.ssh)
                runs.append(run)
                _print(run)

        ok = [r for r in runs if not r.error and r.rows]
        if len(ok) > 1:
            small = min(ok, key=lambda r: r.rows[0]["us"])
            large = max(ok, key=lambda r: r.rows[-1]["busbw_gbps"])
            print(f"\n  best at {ok[0].rows[0]['bytes']} B: {small.label} "
                  f"({small.rows[0]['us']:.2f} us vs {runs[0].rows[0]['us']:.2f})")
            print(f"  best at {ok[0].rows[-1]['bytes']} B: {large.label} "
                  f"({large.rows[-1]['busbw_gbps']:.2f} GB/s vs "
                  f"{runs[0].rows[-1]['busbw_gbps']:.2f})")
            if small.label != large.label:
                print("\n  THEY DISAGREE, which is the answer to the question this")
                print("  tool was built for: a single NCCL_PROTO/NCHANNELS setting")
                print("  cannot be right for both the KB decode collective and the")
                print("  MB prefill one. Do not ship one value for both.")

    # Durable, per pair, keyed so a different pair or image MISSES rather than
    # approximating -- `control_plane/measurements.py` already holds the
    # speculative and decode records on exactly these terms.
    if a.peer and not a.no_record:
        import socket
        from control_plane import measurements as M

        # Node IDS, not addresses. The record is looked up by a plan, and a
        # plan speaks node ids -- storing `192.168.0.172` makes a record the
        # planner can never match, which is a miss that looks like "never
        # measured" and is really "measured under the wrong name".
        src = a.src_node or socket.gethostname()
        dst_node = a.dst_node or a.peer
        kept = 0
        for run in runs:
            for row in run.rows:
                rec = M.NcclRecord(
                    src=src, dst=dst_node,
                    nccl_version=run.nccl or "unknown", image=a.image,
                    size_band=M.band(row["bytes"]),
                    microseconds=row["us"], busbw_gbps=row["busbw_gbps"],
                    env=dict(run.env), measured_at=stamp,
                )
                if M.save_nccl(rec):
                    kept += 1
            if run.error and not run.rows:
                # A failure is a fact about the fabric too: "never tried" and
                # "tried and it would not complete" are different answers and
                # the absence of a record cannot tell them apart.
                rec = M.NcclRecord(
                    src=src, dst=dst_node,
                    nccl_version=run.nccl or "unknown", image=a.image,
                    size_band=0, microseconds=0.0, busbw_gbps=0.0,
                    env=dict(run.env), measured_at=stamp, error=run.error,
                )
                if M.save_nccl(rec):
                    kept += 1
        print(f"\nstored {kept} record(s) under {M.nccl_records_dir()}")

    if a.sweep and a.peer:
        ok = [r for r in runs if not r.error and r.rows]
        best_small = min(ok, key=lambda r: r.rows[0]["us"]) if ok else None
        benefit_report(runs[0], best_small)

    if a.json:
        with open(a.json, "w") as fh:
            json.dump([{"label": r.label, "env": r.env, "nccl": r.nccl,
                        "error": r.error, "rows": r.rows} for r in runs], fh, indent=2)
        print(f"\nwrote {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
