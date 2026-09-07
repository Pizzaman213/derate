"""The measurement ladder.

Three rungs, tried in order, each labelled honestly for what it is:

1. **nccl-tests** -- `all_reduce_perf` and `sendrecv_perf` across the pair. This
   is the real answer, because it is the same code path an inference runtime
   takes. Both collectives are run: all-reduce governs whether tensor parallel
   is viable, sendrecv governs pipeline stage handoff and KV transfer, and the
   two differ enough that deriving one from the other would be guessing.
2. **ib_write_bw** -- raw RDMA, scaled by `IB_TO_NCCL_RATIO` and flagged as an
   estimate. Reporting raw RDMA as if it were NCCL bandwidth is precisely the
   error that makes the ecosystem's defaults wrong; the scaling and the flag
   are what stop us from repeating it.
3. **tcp** -- wire throughput on the data-plane interface. Coarse, flagged, and
   still better than nothing.

If all three fail we return None. A missing measurement is a state the planner
handles; a fabricated one produces a plan that silently underperforms.
"""

from __future__ import annotations

import logging
import os
import shlex
import time
from dataclasses import dataclass, replace
from typing import Protocol

from .gdr import detect_gdr
from .probe_server import DEFAULT_PROBE_PORT, tcp_rtt_us, tcp_throughput_gbps
from .parsers import parse_ib_lat, parse_ib_write_bw, parse_iperf3, parse_nccl_perf
from .qsfp import PortStatus, inspect_ports
from .record import IB_TO_NCCL_RATIO, AnnotatedLink, LinkAnnotation
from .runner import BackgroundCommand, CommandRunner, SubprocessRunner

log = logging.getLogger(__name__)

NCCL_TEST_DIRS = (
    "/opt/nccl-tests/build",
    "/usr/local/nccl-tests/build",
    "/usr/lib/nccl-tests",
    os.path.expanduser("~/nccl-tests/build"),
)

IB_SERVER_PORT = 18515
IPERF_PORT = 5201


@dataclass(frozen=True)
class Endpoint:
    """A node as the probes need to reach it."""

    node_id: str
    host: str
    """Where to dial. The data-plane address when we know one, management otherwise."""

    is_local: bool = False


class Measurer(Protocol):
    method: str

    def available(self) -> bool: ...
    def measure(self, a: Endpoint, b: Endpoint) -> AnnotatedLink | None: ...


class NcclMeasurer:
    """The measurement that counts."""

    method = "nccl-tests"

    def __init__(
        self,
        runner: CommandRunner | None = None,
        clock=None,
        *,
        min_bytes: str = "8",
        max_bytes: str = "512M",
        timeout_s: float = 300.0,
        extra_mpirun_args: tuple[str, ...] | None = None,
    ) -> None:
        self._runner = runner or SubprocessRunner()
        self._clock = clock or time.time
        self._min_bytes = min_bytes
        self._max_bytes = max_bytes
        self._timeout_s = timeout_s
        env_args = os.environ.get("DERATE_MPIRUN_ARGS", "")
        self._extra = tuple(extra_mpirun_args or ()) + tuple(shlex.split(env_args))

    def available(self) -> bool:
        return bool(self._launcher() and self._binary("all_reduce_perf") and self._binary("sendrecv_perf"))

    def measure(self, a: Endpoint, b: Endpoint) -> AnnotatedLink | None:
        launcher = self._launcher()
        all_reduce_bin = self._binary("all_reduce_perf")
        sendrecv_bin = self._binary("sendrecv_perf")
        if not (launcher and all_reduce_bin and sendrecv_bin):
            return None

        ar_run = self._run_test(launcher, all_reduce_bin, a, b)
        sr_run = self._run_test(launcher, sendrecv_bin, a, b)

        ar = parse_nccl_perf(ar_run.combined)
        sr = parse_nccl_perf(sr_run.combined)
        all_reduce = ar.bandwidth_gbps
        sendrecv = sr.bandwidth_gbps

        if all_reduce is None or sendrecv is None:
            # Both collectives are load-bearing and neither is derivable from
            # the other, so a half-run is not a measurement. Drop to the next
            # rung rather than shipping a number for one and a guess for the other.
            log.warning(
                "nccl-tests did not yield both figures for %s/%s (all_reduce=%s, sendrecv=%s); "
                "falling back",
                a.node_id,
                b.node_id,
                all_reduce,
                sendrecv,
            )
            return None

        combined = ar_run.combined + "\n" + sr_run.combined
        evidence = detect_gdr(combined, self._runner)
        ports = _ports_for(self._runner, a, b)

        notes: list[str] = []
        if ar.used_reported_average or sr.used_reported_average:
            notes.append(
                "sweep contained no bandwidth-bound message sizes; used the tool's "
                "own whole-sweep average, which understates a collective's throughput"
            )
        notes.extend(_port_notes(ports))
        if not evidence.enabled:
            notes.append(
                "GPUDirect RDMA is off, so GPU tensors transit system memory before "
                "reaching the NIC; this is why the figure sits well under the link's rate"
            )

        latency = ar.latency_us
        if latency is None:
            latency = sr.latency_us
        if latency is None:
            return None

        return AnnotatedLink(
            src=a.node_id,
            dst=b.node_id,
            all_reduce_gbps=round(all_reduce, 3),
            sendrecv_gbps=round(sendrecv, 3),
            latency_us=round(latency, 2),
            gpudirect_rdma=evidence.enabled,
            measured_at=self._clock(),
            method=self.method,
            annotation=LinkAnnotation(
                estimated=False,
                active_ports=ports.active if ports.known else None,
                total_ports=ports.total if ports.known else None,
                ports_inspected_on=ports.inspected_on if ports.known else None,
                gdr_detected_by=f"{evidence.source}: {evidence.detail}",
                duration_s=round(ar_run.duration_s + sr_run.duration_s, 2),
                notes=tuple(notes),
            ),
        )

    def _run_test(self, launcher: str, binary: str, a: Endpoint, b: Endpoint):
        argv = [
            launcher,
            "--allow-run-as-root",
            "-np",
            "2",
            "-H",
            f"{a.host}:1,{b.host}:1",
            # Observability only. We deliberately set nothing that would change
            # the transport NCCL picks -- NCCL_NET_GDR_LEVEL, NCCL_IB_DISABLE and
            # friends stay untouched, because the point is to measure what this
            # cluster actually does, not what it could be coaxed into doing.
            "-x",
            "NCCL_DEBUG=INFO",
            "-x",
            "NCCL_DEBUG_SUBSYS=INIT,NET",
            *self._extra,
            binary,
            "-b",
            self._min_bytes,
            "-e",
            self._max_bytes,
            "-f",
            "2",
            "-g",
            "1",
        ]
        return self._runner.run(argv, timeout=self._timeout_s)

    def _launcher(self) -> str | None:
        override = os.environ.get("DERATE_MPIRUN")
        if override:
            return override
        for name in ("mpirun", "srun"):
            found = self._runner.which(name)
            if found:
                return found
        return None

    def _binary(self, name: str) -> str | None:
        for env in ("DERATE_NCCL_TESTS_DIR", "NCCL_TESTS_DIR"):
            base = os.environ.get(env)
            if base:
                candidate = os.path.join(base, name)
                if self._runner.read_text(candidate) is not None or self._runner.glob(candidate):
                    return candidate
        found = self._runner.which(name)
        if found:
            return found
        for base in NCCL_TEST_DIRS:
            candidate = os.path.join(base, name)
            if self._runner.glob(candidate):
                return candidate
        return None


class _ClientSideMixin:
    """Runs a two-sided probe from the correct end of the link.

    `ib_write_bw` and `iperf3` are client/server: the client's numbers describe
    the path from wherever the client ran. Running it here would measure
    coordinator-to-b whenever the coordinator is not itself endpoint a, and
    file the answer under a-to-b. So the client runs on a -- locally if a is us,
    over ssh if it is not.
    """

    _runner: CommandRunner

    def _client(self, a: Endpoint, argv: list[str], timeout: float):
        if a.is_local:
            return self._runner.run(argv, timeout=timeout)
        return self._runner.run_on(a.host, argv, timeout=timeout)


class IbWriteBwMeasurer(_ClientSideMixin):
    """Raw RDMA, scaled and flagged. Never reported as NCCL bandwidth."""

    method = "ib_write_bw"

    def __init__(self, runner: CommandRunner | None = None, clock=None, *, port: int = IB_SERVER_PORT) -> None:
        self._runner = runner or SubprocessRunner()
        self._clock = clock or time.time
        self._port = port

    def available(self) -> bool:
        return self._runner.which("ib_write_bw") is not None

    def measure(self, a: Endpoint, b: Endpoint) -> AnnotatedLink | None:
        ports = _ports_for(self._runner, a, b)
        device = _first_active_device(ports)
        dev_args = ["-d", device] if device else []
        common = ["-a", "--report_gbits", "-F", "-p", str(self._port), *dev_args]

        # The peer runs the listener; we drive it. If the peer has no perftest,
        # the client simply fails to connect and we fall through.
        with BackgroundCommand(self._runner, b.host, ["ib_write_bw", *common]):
            client = self._client(a, ["ib_write_bw", *common, b.host], timeout=180.0)

        raw_gbps, unit = parse_ib_write_bw(client.combined)
        if raw_gbps is None or raw_gbps <= 0:
            return None

        scaled = raw_gbps * IB_TO_NCCL_RATIO
        latency, latency_note = self._latency(a, b, dev_args)
        if latency is None:
            return None

        evidence = detect_gdr(None, self._runner)
        # The 0.42 scale always applies (raw RDMA is never NCCL bandwidth),
        # but the note must not claim a GDR-disabled path when the evidence
        # says GDR is active.
        if evidence.enabled:
            path_clause = (
                "to approximate NCCL all-reduce throughput. Raw RDMA "
                "is not NCCL bandwidth and is not reported as such"
            )
        else:
            path_clause = (
                "to approximate the NCCL path staged through system memory. "
                "Raw RDMA is not NCCL bandwidth and is not reported as such"
            )
        notes = [
            f"derived from ib_write_bw: {raw_gbps:.2f} GB/s raw RDMA scaled by "
            f"{IB_TO_NCCL_RATIO} " + path_clause,
            "ib_write_bw cannot tell all-reduce from sendrecv, so both figures are the "
            "same estimate; re-measure with nccl-tests before trusting the difference",
        ]
        if latency_note:
            notes.append(latency_note)
        notes.extend(_port_notes(ports))

        return AnnotatedLink(
            src=a.node_id,
            dst=b.node_id,
            all_reduce_gbps=round(scaled, 3),
            sendrecv_gbps=round(scaled, 3),
            latency_us=round(latency, 2),
            gpudirect_rdma=evidence.enabled,
            measured_at=self._clock(),
            method=self.method,
            annotation=LinkAnnotation(
                estimated=True,
                raw_gbps=round(raw_gbps, 3),
                scale_factor=IB_TO_NCCL_RATIO,
                active_ports=ports.active if ports.known else None,
                total_ports=ports.total if ports.known else None,
                ports_inspected_on=ports.inspected_on if ports.known else None,
                gdr_detected_by=f"{evidence.source}: {evidence.detail}",
                duration_s=round(client.duration_s, 2),
                notes=tuple(notes),
            ),
        )

    def _latency(self, a: Endpoint, b: Endpoint, dev_args: list[str]) -> tuple[float | None, str | None]:
        if self._runner.which("ib_write_lat"):
            common = ["-F", "-p", str(self._port + 1), *dev_args]
            with BackgroundCommand(self._runner, b.host, ["ib_write_lat", *common]):
                out = self._client(a, ["ib_write_lat", *common, b.host], timeout=60.0)
            value = parse_ib_lat(out.combined)
            if value is not None:
                return value, None
        if not a.is_local:
            # A round trip measured from here is the wrong round trip.
            return None, None
        rtt = tcp_rtt_us(b.host, DEFAULT_PROBE_PORT) or tcp_rtt_us(b.host, 22)
        if rtt is None:
            return None, None
        return rtt, (
            "latency is a TCP connect round trip, not a collective latency; "
            "ib_write_lat was unavailable"
        )


class TcpMeasurer(_ClientSideMixin):
    """Wire throughput on the data-plane interface. The floor of the ladder."""

    method = "tcp"

    def __init__(
        self,
        runner: CommandRunner | None = None,
        clock=None,
        *,
        probe_port: int = DEFAULT_PROBE_PORT,
        duration_s: float = 5.0,
    ) -> None:
        self._runner = runner or SubprocessRunner()
        self._clock = clock or time.time
        self._probe_port = probe_port
        self._duration_s = duration_s

    def available(self) -> bool:
        return True  # the built-in sink needs nothing installed

    def measure(self, a: Endpoint, b: Endpoint) -> AnnotatedLink | None:
        gbps, source = self._throughput(a, b)
        if gbps is None or gbps <= 0:
            return None

        latency = self._latency(a, b)
        if latency is None:
            return None

        ports = _ports_for(self._runner, a, b)
        evidence = detect_gdr(None, self._runner)
        notes = [
            f"TCP wire throughput via {source}, not a collective measurement. NCCL on "
            "this link will be slower than this figure, not faster; treat it as an "
            "upper bound and re-measure with nccl-tests before planning on it",
            "latency is a TCP connect round trip, not a collective latency",
        ]
        notes.extend(_port_notes(ports))

        return AnnotatedLink(
            src=a.node_id,
            dst=b.node_id,
            all_reduce_gbps=round(gbps, 3),
            sendrecv_gbps=round(gbps, 3),
            latency_us=round(latency, 2),
            gpudirect_rdma=evidence.enabled,
            measured_at=self._clock(),
            method=self.method,
            annotation=LinkAnnotation(
                estimated=True,
                raw_gbps=round(gbps, 3),
                active_ports=ports.active if ports.known else None,
                total_ports=ports.total if ports.known else None,
                ports_inspected_on=ports.inspected_on if ports.known else None,
                gdr_detected_by=f"{evidence.source}: {evidence.detail}",
                notes=tuple(notes),
            ),
        )

    def _throughput(self, a: Endpoint, b: Endpoint) -> tuple[float | None, str]:
        # When the client runs here we can check for iperf3 first; when it runs
        # on a remote node we cannot, so we try and let the parse decide.
        if not a.is_local or self._runner.which("iperf3"):
            server = ["iperf3", "-s", "-1", "-p", str(IPERF_PORT)]
            with BackgroundCommand(self._runner, b.host, server):
                out = self._client(
                    a,
                    ["iperf3", "-c", b.host, "-p", str(IPERF_PORT), "-J", "-t", str(int(self._duration_s))],
                    timeout=self._duration_s + 30.0,
                )
            gbps = parse_iperf3(out.combined)
            if gbps:
                return gbps, "iperf3"
        if not a.is_local:
            # The built-in sink is driven from this process, so it can only
            # measure a link this node is actually on.
            return None, "unreachable"
        gbps = tcp_throughput_gbps(b.host, self._probe_port, duration_s=self._duration_s)
        return gbps, "the built-in probe sink"

    def _latency(self, a: Endpoint, b: Endpoint) -> float | None:
        if not a.is_local:
            return None
        return tcp_rtt_us(b.host, self._probe_port) or tcp_rtt_us(b.host, 22)


class LadderMeasurer:
    """Try each rung in order and take the first that produces a real number."""

    def __init__(self, measurers: list[Measurer]) -> None:
        self._measurers = measurers

    def measure(self, a: Endpoint, b: Endpoint) -> AnnotatedLink | None:
        skipped: list[str] = []
        for measurer in self._measurers:
            if not measurer.available():
                skipped.append(f"{measurer.method} (not installed)")
                continue
            try:
                result = measurer.measure(a, b)
            except Exception:
                log.exception("%s probe raised measuring %s/%s", measurer.method, a.node_id, b.node_id)
                skipped.append(f"{measurer.method} (probe failed)")
                continue
            if result is not None:
                if skipped:
                    return _with_note(result, "tried first, without success: " + ", ".join(skipped))
                return result
            skipped.append(f"{measurer.method} (no usable output)")

        log.warning(
            "no usable measurement for %s/%s; tried %s. The planner will run in "
            "conservative mode for this pair.",
            a.node_id,
            b.node_id,
            ", ".join(skipped) or "nothing",
        )
        return None


def default_measurer(runner: CommandRunner | None = None, clock=None) -> LadderMeasurer:
    runner = runner or SubprocessRunner()
    return LadderMeasurer(
        [
            NcclMeasurer(runner, clock),
            IbWriteBwMeasurer(runner, clock),
            TcpMeasurer(runner, clock),
        ]
    )


def _with_note(link: AnnotatedLink, note: str) -> AnnotatedLink:
    return replace(link, annotation=link.annotation.with_note(note))


def _ports_for(runner: CommandRunner, a: Endpoint, b: Endpoint) -> PortStatus:
    """Inspect fabric ports, but only when we are looking at our own.

    Port state is a local observation. If the coordinator is neither endpoint,
    its own cages say nothing about the link between two other machines, and
    attaching them to that record would be a plausible-looking lie.
    """
    local = _local_node(a, b)
    if local is None:
        return PortStatus(ports=(), inspected_on=None, source="not-observable")
    return inspect_ports(runner, local)


def _port_notes(ports: PortStatus) -> list[str]:
    if ports.source == "not-observable":
        return [
            "QSFP port state was not recorded: neither endpoint is this node, so the "
            "cages we can see are not the ones carrying this link"
        ]
    note = ports.note()
    return [note] if note else []


def _first_active_device(ports: PortStatus) -> str | None:
    """The HCA name perftest wants, e.g. `mlx5_0`, from an active port."""
    for port in ports.ports:
        if port.active and ":" in port.name:
            return port.name.split(":", 1)[0]
    return None


def _local_node(a: Endpoint, b: Endpoint) -> str | None:
    if a.is_local:
        return a.node_id
    if b.is_local:
        return b.node_id
    return None
