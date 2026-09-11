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


class TorchNcclMeasurer:
    """A real NCCL collective, timed inside the serving image.

    The rung `NcclMeasurer` above has always wanted to be and never could:
    its `available()` needs `mpirun`, `all_reduce_perf` and `sendrecv_perf`,
    none of which is installed anywhere in this estate, so every link on every
    cluster derate has run on fell through to `ib_write_bw` -- raw RDMA scaled
    by a constant, with a latency for a different operation.

    This one needs none of that. It runs a two-rank `torch.distributed`
    all_reduce in the SAME container image the engine runs, so it exercises
    the same NCCL build, the same transport and the same rails vLLM's own
    collectives will. That is strictly better evidence than nccl-tests would
    be: nccl-tests would measure a different NCCL than the one that serves.

    It answers both numbers the planner needs and neither of which existed:

    * the **8-byte time** is a real `latency_us` -- a collective, not the
      one-sided 2-byte RDMA write `ib_write_lat` reports;
    * **busbw at the largest size** is a real `all_reduce_gbps`, which retires
      `IB_TO_NCCL_RATIO` for any link this rung covers.

    Measured on this estate 2026-09-11: 13.36 us and 18.5 GB/s, against the
    ib_write_bw rung's 5.728 GB/s and the 40 us the planner was charging.

    Kept BELOW `NcclMeasurer` in the ladder so an operator who does install
    nccl-tests still gets the reference implementation, and above
    `IbWriteBwMeasurer` because an estimate should never win over a
    measurement.
    """

    method = "nccl-torch"

    #: 8 B is the latency floor. The largest is the bandwidth figure. The two
    #: in between are the sizes derate's own collectives actually are, kept so
    #: the notes can show the curve rather than two endpoints.
    SIZES = (8, 8192, 1 << 20, 64 << 20)

    def __init__(self, runner: CommandRunner | None = None, clock=None, *, image: str | None = None) -> None:
        self._runner = runner or SubprocessRunner()
        self._clock = clock or time.time
        self._image = image

    def available(self) -> bool:
        """Whether the serving image is here to run the collective in.

        Deliberately cheap: the expensive question -- does the fabric actually
        complete a collective -- is what `measure` answers, and answering it
        twice would double the cost of every ladder descent.
        """
        if self._runner.which("docker") is None:
            return False
        try:
            proc = self._runner.run(
                ["docker", "image", "inspect", self._resolved_image()], timeout=30.0
            )
            return proc.ok
        except Exception:
            return False

    def _resolved_image(self) -> str:
        if self._image:
            return self._image
        from control_plane.deploy.flags import runtime_spec

        return os.environ.get("DERATE_VLLM_IMAGE") or runtime_spec("vllm").default_image

    def measure(self, a: Endpoint, b: Endpoint) -> AnnotatedLink | None:
        from control_plane.links import collective

        local, peer = (a, b) if a.is_local else (b, a)
        if not local.is_local:
            # Neither end is this host, so this coordinator cannot be rank 0
            # and has nothing honest to say about the pair.
            return None

        started = time.monotonic()
        result = collective.run_collective(
            image=self._resolved_image(),
            sizes=list(self.SIZES),
            master_addr=local.host,
            peer_host=peer.host,
            iface=collective.local_interface_for(local.host),
        )
        if not result.ok:
            log.info("nccl-torch measure %s/%s: %s", a.node_id, b.node_id, result.error)
            return None

        floor = result.rows[0]["us"]
        top = max(result.rows, key=lambda r: r["bytes"])
        ports = _ports_for(self._runner, a, b)
        evidence = detect_gdr(None, self._runner)

        notes = [
            f"two-rank all_reduce in {self._resolved_image()}, NCCL "
            f"{result.nccl or 'unknown'} -- the same build the engine runs, so "
            f"this is the transport vLLM's own collectives will use",
            "latency_us is the 8-byte COLLECTIVE time, not an ib_write_lat: a "
            "one-sided 2-byte RDMA write is a different operation and was the "
            "number this ladder used to report",
            " · ".join(
                f"{r['bytes']}B {r['us']:.2f}us" for r in result.rows
            ),
        ]
        if not evidence.enabled:
            notes.append(
                "GPUDirect RDMA is off, so every byte stages through host "
                "memory -- on GB10 that is not a misconfiguration, it is what "
                "unified memory means"
            )
        notes.extend(_port_notes(ports))

        return AnnotatedLink(
            src=a.node_id,
            dst=b.node_id,
            all_reduce_gbps=round(top["busbw_gbps"], 3),
            # The same collective is the only thing measured, so reporting a
            # different sendrecv figure would be inventing one. `ib_write_bw`
            # says the same about itself.
            sendrecv_gbps=round(top["busbw_gbps"], 3),
            latency_us=round(floor, 2),
            gpudirect_rdma=evidence.enabled,
            measured_at=self._clock(),
            method=self.method,
            annotation=LinkAnnotation(
                estimated=False,
                active_ports=ports.active if ports.known else None,
                total_ports=ports.total if ports.known else None,
                ports_inspected_on=ports.inspected_on if ports.known else None,
                gdr_detected_by=f"{evidence.source}: {evidence.detail}",
                duration_s=round(time.monotonic() - started, 2),
                notes=tuple(notes),
            ),
        )


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
        # `ib_write_lat` is still run, and its figure is still reported -- as a
        # NOTE, never as `latency_us`. It measures a one-sided 2-byte RDMA
        # write; the planner multiplies `latency_us` by the exchange count and
        # calls the product the cost of that many two-rank NCCL all-reduces,
        # which additionally carry a kernel launch, a reduction and a
        # synchronisation. Reporting the write as the collective made the most
        # decision-sensitive input in the planner ~28x optimistic (1.44 us
        # recorded against this project's own nccl-tests fixtures at 40.0),
        # and every plan built on it was wrong in the direction that favours
        # tensor parallel.
        #
        # Absence rather than a substitute: this rung genuinely cannot measure
        # a collective, and `UNMEASURED_COLLECTIVE_LATENCY_US` is what the
        # planner charges instead, labelled. A missing ib_write_lat therefore
        # no longer fails the whole rung -- the bandwidth estimate is still
        # worth having.
        write_latency, latency_note = self._latency(a, b, dev_args)

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
            "no collective latency was measured: ib_write_lat times a one-sided "
            "2-byte RDMA write, not a two-rank all-reduce, so it is reported "
            "here as evidence and not as latency_us"
            + (f" (ib_write_lat: {write_latency:.2f} us)" if write_latency is not None else ""),
            "ib_write_bw cannot tell all-reduce from sendrecv, so both figures are the "
            "same estimate; re-measure with nccl-tests before trusting the difference",
        ]
        if latency_note:
            notes.append(latency_note)
        notes.extend(_single_rail_note(ports))
        notes.extend(_port_notes(ports))

        return AnnotatedLink(
            src=a.node_id,
            dst=b.node_id,
            all_reduce_gbps=round(scaled, 3),
            sendrecv_gbps=round(scaled, 3),
            latency_us=None,
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
            # Below nccl-tests so a box that has it keeps the reference, above
            # ib_write_bw because an estimate must never beat a measurement.
            TorchNcclMeasurer(runner, clock),
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


def _active_devices(ports: PortStatus) -> list[str]:
    """Every HCA with an active port, in order, deduplicated."""
    out: list[str] = []
    for port in ports.ports:
        if port.active and ":" in port.name:
            name = port.name.split(":", 1)[0]
            if name not in out:
                out.append(name)
    return out


def _first_active_device(ports: PortStatus) -> str | None:
    """The HCA name perftest wants, e.g. `mlx5_0`, from an active port."""
    devices = _active_devices(ports)
    return devices[0] if devices else None


def _single_rail_note(ports: PortStatus) -> list[str]:
    """Said when this rung drove one rail of several, which it always does.

    `ib_write_bw` takes ONE `-d`, so a box with two active HCAs gets a figure
    for one of them and the estimate is roughly half the fabric. Patching
    around that by running perftest twice and adding the numbers would be
    inventing an aggregate; the honest fix is the `nccl-torch` rung above,
    which uses NCCL and binds every rail itself. This note is what stops the
    fallback's number being read as the whole link.
    """
    devices = _active_devices(ports)
    if len(devices) <= 1:
        return []
    return [
        "measured on %s only: ib_write_bw drives one HCA per process, and this "
        "host has %d active (%s). The figure is one rail's, not the fabric's -- "
        "the nccl-torch rung measures every rail because NCCL binds them itself"
        % (devices[0], len(devices), ", ".join(devices))
    ]


def _local_node(a: Endpoint, b: Endpoint) -> str | None:
    if a.is_local:
        return a.node_id
    if b.is_local:
        return b.node_id
    return None
