"""CLI.

    python -m tests.load calibrate    # the harness's own ceiling, first
    python -m tests.load rps          # the 1 -> 200,000 rps ladder
    python -m tests.load stream       # 1 -> 8192 concurrent streams
    python -m tests.load probes       # the six cascades
    python -m tests.load all
"""

from __future__ import annotations

import argparse
import json
import sys

from . import ladder, probes
from .harness import Cluster


def _rule(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.load")
    parser.add_argument(
        "command",
        choices=["calibrate", "rps", "stream", "probes", "all"],
        nargs="?",
        default="all",
    )
    parser.add_argument("--backends", type=int, default=4)
    parser.add_argument("--tokens", type=int, default=64)
    parser.add_argument("--ttft", type=float, default=0.01)
    parser.add_argument("--itl", type=float, default=0.02)
    parser.add_argument("--duration", type=float, default=15.0)
    parser.add_argument("--cooldown", type=float, default=5.0)
    parser.add_argument(
        "--max-rps", type=float, default=200_000.0, help="highest rung to attempt"
    )
    parser.add_argument(
        "--max-streams", type=int, default=8192, help="highest stream rung to attempt"
    )
    parser.add_argument("--no-ab", action="store_true", help="skip the direct A/B leg")
    parser.add_argument(
        "--settings", default="{}", help="JSON GatewaySettings overrides for the DUT"
    )
    parser.add_argument("--json", help="write the full result to this path")
    parser.add_argument(
        "--rungs", help="comma-separated rungs, overriding the built-in ladder"
    )
    parser.add_argument(
        "--policy",
        default="least_outstanding",
        help="pin the routing policy so rungs stay comparable",
    )
    args = parser.parse_args()

    cluster = Cluster(
        backends=args.backends,
        tokens=args.tokens,
        ttft=args.ttft,
        itl=args.itl,
        settings=json.loads(args.settings),
    )
    out: dict = {"config": vars(args)}
    exit_code = 0

    print(
        f"starting {args.backends} fake runtimes "
        f"({args.tokens} tokens, ttft {args.ttft * 1000:.0f} ms, "
        f"itl {args.itl * 1000:.0f} ms) and the gateway",
        flush=True,
    )
    cluster.start()
    cluster.pin_policy(args.policy)
    print(f"gateway at {cluster.gateway_url}", flush=True)

    try:
        if args.command in ("calibrate", "all"):
            _rule("calibrate: what this harness can generate and absorb")
            out["calibrate"] = ladder.calibrate(cluster)

        if args.command in ("rps", "all"):
            _rule("ladder: offered request rate, 1 -> 200,000")
            out["rps"] = ladder.run_rps(
                cluster,
                duration=args.duration,
                cooldown=args.cooldown,
                rungs=(
                    [float(r) for r in args.rungs.split(",")]
                    if args.rungs
                    else [r for r in ladder.RUNGS_RPS if r <= args.max_rps]
                ),
                ab=not args.no_ab,
            )
            print(f"derated request rate: {out['rps']['derated']} rps", flush=True)

        if args.command in ("stream", "all"):
            _rule("ladder: concurrent SSE streams, 1 -> 8192")
            out["stream"] = ladder.run_stream(
                cluster,
                duration=args.duration,
                cooldown=args.cooldown,
                rungs=(
                    [int(r) for r in args.rungs.split(",")]
                    if args.rungs
                    else [r for r in ladder.RUNGS_STREAM if r <= args.max_streams]
                ),
            )
            print(
                f"derated concurrent streams: {out['stream']['derated']}", flush=True
            )

        if args.command in ("probes", "all"):
            _rule("probes: the cascades")
            findings = []
            for probe in probes.ALL:
                print(f"\n-> {probe.__name__}", flush=True)
                try:
                    # settle() is part of the probe: if preconditions cannot be
                    # established the probe did not run, and saying so is the
                    # whole point.
                    probes.settle(cluster)
                    finding = probe(cluster)
                except Exception as exc:
                    finding = {
                        "probe": probe.__name__,
                        "broke": None,
                        "observed": f"did not run: {type(exc).__name__}: {exc}",
                        "detail": {},
                    }
                findings.append(finding)
                mark = {True: "BROKE", False: "held", None: "DID NOT RUN"}[
                    finding.get("broke")
                ]
                print(f"   [{mark}] {finding.get('observed')}", flush=True)
            out["probes"] = findings

            _rule("summary")
            broke = [f for f in findings if f.get("broke") is True]
            # A probe that did not run verified nothing. Counting it as a pass
            # is the quiet lie this whole harness exists to catch, and it has
            # no business living in the tool that looks for it.
            unrun = [f for f in findings if f.get("broke") is None]
            held = [f for f in findings if f.get("broke") is False]

            for finding in broke:
                print(f"  BROKE        {finding['probe']}: {finding['observed']}")
            for finding in unrun:
                print(f"  DID NOT RUN  {finding['probe']}: {finding['observed']}")
            print(
                f"\n  {len(held)} held, {len(broke)} broke, "
                f"{len(unrun)} never established their preconditions"
            )
            if broke or unrun:
                exit_code = 1
            out["verdict"] = {
                "held": [f["probe"] for f in held],
                "broke": [f["probe"] for f in broke],
                "did_not_run": [f["probe"] for f in unrun],
            }
    finally:
        cluster.stop()

    if args.json:
        with open(args.json, "w") as handle:
            json.dump(out, handle, indent=2, default=str)
        print(f"\nfull result written to {args.json}", flush=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
