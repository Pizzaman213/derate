"""Tests for link measurement.

The number this component produces is the input the whole product turns on, so
these tests care less about coverage than about two specific failure modes: a
figure that is subtly wrong, and a figure that is honest about being a guess.
"""

from __future__ import annotations

import fnmatch
import json
import os
import threading
import time

import pytest

from control_plane.contracts.hardware import LinkMeasurement
from control_plane.links import (
    IB_TO_NCCL_RATIO,
    AnnotatedLink,
    Endpoint,
    LadderMeasurer,
    LinkService,
    LinkStore,
    NcclMeasurer,
    StubLinkService,
    detect_gdr,
    inspect_ports,
    pair_key,
)
from control_plane.links.measure import IbWriteBwMeasurer, TcpMeasurer
from control_plane.links.parsers import (
    parse_ib_lat,
    parse_ib_write_bw,
    parse_iperf3,
    parse_nccl_perf,
)
from control_plane.links.record import LinkAnnotation, annotate, from_json, to_json
from control_plane.links.runner import CommandResult
from tests.fixtures import LINK_SPARK_10G

T0 = 1_788_912_000.0  # a fixed clock, so nothing here depends on wall time
SEVEN_DAYS = 7 * 24 * 60 * 60


# --------------------------------------------------------------------------- doubles


class FakeRunner:
    """A CommandRunner that answers from a script instead of the machine."""

    def __init__(
        self,
        *,
        binaries: tuple[str, ...] = (),
        outputs: dict[str, str] | None = None,
        files: dict[str, str] | None = None,
        failures: tuple[str, ...] = (),
    ) -> None:
        self._binaries = set(binaries)
        self._outputs = outputs or {}
        self._files = files or {}
        self._failures = set(failures)
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv, timeout=60.0, env=None):
        self.calls.append(tuple(argv))
        return self._result(argv)

    def run_on(self, host, argv, timeout=60.0):
        self.calls.append(("ssh", host, *argv))
        return self._result(argv)

    def which(self, name):
        return f"/usr/bin/{name}" if name in self._binaries else None

    def read_text(self, path):
        return self._files.get(path)

    def glob(self, pattern):
        """Segment-wise, like the real thing: `*` does not cross a `/`.

        sysfs globs such as `/sys/class/infiniband/*/ports/*` land on
        directories, so this yields path prefixes at the pattern's depth rather
        than only whole file paths.
        """
        parts = pattern.split("/")
        hits = set()
        for path in self._files:
            segments = path.split("/")
            if len(segments) < len(parts):
                continue
            prefix = segments[: len(parts)]
            if all(fnmatch.fnmatch(seg, pat) for seg, pat in zip(prefix, parts)):
                hits.add("/".join(prefix))
        return sorted(hits)

    def _result(self, argv):
        stdout = ""
        rc = 0
        for token in argv:
            base = os.path.basename(str(token))
            if base in self._failures:
                rc = 127
            if base in self._outputs:
                stdout = self._outputs[base]
        return CommandResult(tuple(argv), rc, stdout, "", 0.1)


class ScriptedMeasurer:
    """A ladder rung with a predetermined answer."""

    def __init__(self, method: str, result: AnnotatedLink | None, *, available: bool = True, raises=None):
        self.method = method
        self._result = result
        self._available = available
        self._raises = raises
        self.calls = 0

    def available(self):
        return self._available

    def measure(self, a, b):
        self.calls += 1
        if self._raises:
            raise self._raises
        return self._result


def link(
    src="spark-01",
    dst="spark-02",
    all_reduce=10.2,
    sendrecv=9.0,
    latency=40.0,
    gdr=False,
    at=T0,
    method="nccl-tests",
    annotation=None,
) -> AnnotatedLink:
    return AnnotatedLink(
        src=src,
        dst=dst,
        all_reduce_gbps=all_reduce,
        sendrecv_gbps=sendrecv,
        latency_us=latency,
        gpudirect_rdma=gdr,
        measured_at=at,
        method=method,
        annotation=annotation or LinkAnnotation(),
    )


def service(tmp_path, measurer=None, clock=None, **kwargs) -> LinkService:
    return LinkService(
        path=tmp_path / "links.json",
        measurer=measurer,
        clock=clock or (lambda: T0),
        **kwargs,
    )


# --------------------------------------------------------------------------- sample output

# A GB10 pair with GPUDirect RDMA off. Bandwidth-bound sizes sit near 10.2 GB/s;
# the tool's own whole-sweep average is 5.9, dragged down by the tiny messages.
ALL_REDUCE_OUT = """# nThread 1 nGpus 1 minBytes 8 maxBytes 536870912 step: 2(factor) validation: 1
#  Rank  0 Group  0 Pid  14213 on   spark-01 device  0 [0x01] NVIDIA GB10
#  Rank  1 Group  0 Pid   9931 on   spark-02 device  0 [0x01] NVIDIA GB10
spark-01:14213:14219 [0] NCCL INFO NET/IB : GPU Direct RDMA Disabled for HCA 0 'mlx5_0'
spark-01:14213:14219 [0] NCCL INFO Channel 00/0 : 0[0] -> 1[0] [receive] via NET/IB/0
#                                                              out-of-place                       in-place
#       size         count      type   redop    root     time   algbw   busbw #wrong     time   algbw   busbw #wrong
           8             2     float     sum      -1    40.12    0.00    0.00      0    39.88    0.00    0.00      0
        1024           256     float     sum      -1    41.55    0.02    0.02      0    41.30    0.02    0.02      0
     1048576        262144     float     sum      -1   215.40    4.87    4.87      0   214.90    4.88    4.88      0
    16777216       4194304     float     sum      -1  1652.10   10.16   10.16      0  1650.30   10.17   10.17      0
   134217728      33554432     float     sum      -1 13120.40   10.23   10.23      0 13109.10   10.24   10.24      0
   536870912     134217728     float     sum      -1 52210.60   10.28   10.28      0 52198.20   10.29   10.29      0
# Out of bounds values : 0 OK
# Avg bus bandwidth    : 5.9155
"""

SENDRECV_OUT = """#       size         count      type   redop    root     time   algbw   busbw #wrong     time   algbw   busbw #wrong
           8             2     float     sum      -1    38.90    0.00    0.00      0    38.70    0.00    0.00      0
    16777216       4194304     float     sum      -1  1875.30    8.95    8.95      0  1873.10    8.96    8.96      0
   134217728      33554432     float     sum      -1 14900.20    9.01    9.01      0 14895.40    9.02    9.02      0
# Avg bus bandwidth    : 4.5051
"""

IB_WRITE_BW_OUT = """---------------------------------------------------------------------------------------
                    RDMA_Write BW Test
 Device         : mlx5_0
---------------------------------------------------------------------------------------
 #bytes     #iterations    BW peak[Gb/sec]    BW average[Gb/sec]   MsgRate[Mpps]
 2          5000             1.23               1.20                 0.075000
 8388608    5000             197.10             196.92               0.002934
"""

IB_WRITE_LAT_OUT = """ #bytes #iterations    t_min[usec]    t_max[usec]  t_typical[usec]    t_avg[usec]
 2       1000           1.85           28.40          2.05             2.11
"""

# Both cages lit.
SYSFS_TWO_PORTS = {
    "/sys/class/infiniband/mlx5_0/ports/1/state": "4: ACTIVE\n",
    "/sys/class/infiniband/mlx5_0/ports/1/phys_state": "5: LinkUp\n",
    "/sys/class/infiniband/mlx5_0/ports/1/rate": "200 Gb/sec (4X NDR)\n",
    "/sys/class/infiniband/mlx5_0/ports/1/link_layer": "Ethernet\n",
    "/sys/class/infiniband/mlx5_1/ports/1/state": "4: ACTIVE\n",
    "/sys/class/infiniband/mlx5_1/ports/1/phys_state": "5: LinkUp\n",
    "/sys/class/infiniband/mlx5_1/ports/1/rate": "200 Gb/sec (4X NDR)\n",
    "/sys/class/infiniband/mlx5_1/ports/1/link_layer": "Ethernet\n",
}

# One cable in, one cage dark.
SYSFS_ONE_PORT = dict(SYSFS_TWO_PORTS)
SYSFS_ONE_PORT["/sys/class/infiniband/mlx5_1/ports/1/state"] = "1: DOWN\n"
SYSFS_ONE_PORT["/sys/class/infiniband/mlx5_1/ports/1/phys_state"] = "3: Disabled\n"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Nothing here should depend on the developer's shell."""
    for name in (
        "SPARKPLANE_MPIRUN",
        "SPARKPLANE_MPIRUN_ARGS",
        "SPARKPLANE_NCCL_TESTS_DIR",
        "NCCL_TESTS_DIR",
        "SPARKPLANE_NODE_ID",
        "SPARKPLANE_DATA_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------- parsers


def test_bandwidth_comes_from_bandwidth_bound_messages_not_the_whole_sweep():
    """The tool's printed average answers a different question than ours.

    `# Avg bus bandwidth` averages the entire sweep, including 8-byte messages
    that are pure latency. Reporting it would put a GB10 pair at 5.9 GB/s and
    send the planner down a different branch than the link deserves.
    """
    result = parse_nccl_perf(ALL_REDUCE_OUT)
    assert result.reported_avg_busbw == pytest.approx(5.9155)
    assert result.bandwidth_gbps == pytest.approx(10.23, abs=0.05)
    assert not result.used_reported_average


def test_latency_is_the_smallest_message_in_the_sweep():
    assert parse_nccl_perf(ALL_REDUCE_OUT).latency_us == pytest.approx(40.0, abs=0.2)


def test_parses_rows_when_validation_is_off_and_wrong_prints_na():
    text = ALL_REDUCE_OUT.replace("      0    ", "    N/A    ").replace("      0\n", "    N/A\n")
    result = parse_nccl_perf(text)
    assert len(result.rows) == 6
    assert result.bandwidth_gbps == pytest.approx(10.23, abs=0.05)


def test_parses_op_variants_that_omit_metadata_columns():
    text = """#       size         count      type     time   algbw   busbw #wrong     time   algbw   busbw #wrong
           8             2     float    38.90    0.00    0.00      0    38.70    0.00    0.00      0
    67108864      16777216     float  7450.20    9.01    9.01      0  7448.10    9.00    9.00      0
"""
    result = parse_nccl_perf(text)
    assert result.bandwidth_gbps == pytest.approx(9.005, abs=0.01)


def test_a_sweep_with_no_large_messages_says_so():
    text = """#       size         count      type   redop    root     time   algbw   busbw #wrong     time   algbw   busbw #wrong
           8             2     float     sum      -1    40.12    0.00    0.00      0    39.88    0.00    0.00      0
# Avg bus bandwidth    : 0.0021
"""
    result = parse_nccl_perf(text)
    assert result.used_reported_average
    assert result.bandwidth_gbps == pytest.approx(0.0021)


def test_parsers_return_none_rather_than_guessing():
    assert parse_nccl_perf("bash: all_reduce_perf: command not found").bandwidth_gbps is None
    assert parse_ib_write_bw("segmentation fault") == (None, None)
    assert parse_iperf3('{"error": "unable to connect"}') is None
    assert parse_iperf3("not json at all") is None
    assert parse_ib_lat("") is None


@pytest.mark.parametrize(
    "text",
    [IB_WRITE_BW_OUT, IB_WRITE_BW_OUT.replace("Gb/sec", "MB/sec").replace("196.92", "23440.00")],
    ids=["gigabits", "mebibytes"],
)
def test_ib_write_bw_units_are_read_from_the_header(text):
    """perftest reports Gb/sec or MB/sec, and its MB is 2**20. Both land at ~24.6 GB/s."""
    gbps, _ = parse_ib_write_bw(text)
    assert gbps == pytest.approx(24.6, abs=0.1)


def test_ib_write_bw_takes_the_peak_not_the_small_message_rows():
    gbps, _ = parse_ib_write_bw(IB_WRITE_BW_OUT)
    assert gbps > 20  # the 2-byte row reports 1.2 Gb/sec and must not win


def test_iperf3_prefers_the_receivers_count():
    doc = json.dumps(
        {"end": {"sum_sent": {"bits_per_second": 99e9}, "sum_received": {"bits_per_second": 94.5e9}}}
    )
    assert parse_iperf3(doc) == pytest.approx(11.8125)


# --------------------------------------------------------------------------- GDR


def test_gdr_disabled_is_read_from_nccls_own_report():
    evidence = detect_gdr(ALL_REDUCE_OUT)
    assert evidence.enabled is False
    assert evidence.source == "nccl-debug"


def test_gdr_enabled_is_read_from_nccls_own_report():
    text = ALL_REDUCE_OUT.replace("GPU Direct RDMA Disabled", "GPU Direct RDMA Enabled")
    assert detect_gdr(text).enabled is True


def test_gdr_enabled_when_nccl_picks_a_gdrdma_transport():
    text = "NCCL INFO Channel 00/0 : 0[0] -> 1[0] [receive] via NET/IB/0/GDRDMA"
    evidence = detect_gdr(text)
    assert evidence.enabled is True


def test_gdr_falls_back_to_the_peer_memory_module_when_nccl_is_silent():
    loaded = FakeRunner(files={"/proc/modules": "nvidia_peermem 16384 0 - Live 0x0\n"})
    assert detect_gdr(None, loaded).enabled is True
    absent = FakeRunner(files={"/proc/modules": "nvidia 1000 0 - Live 0x0\n"})
    assert detect_gdr(None, absent).enabled is False


def test_gdr_with_no_evidence_reports_disabled_rather_than_assuming():
    evidence = detect_gdr(None, None)
    assert evidence.enabled is False
    assert "assumed" in (evidence.detail or "")


# --------------------------------------------------------------------------- QSFP ports


def test_detects_both_qsfp_ports():
    status = inspect_ports(FakeRunner(files=SYSFS_TWO_PORTS), "spark-01")
    assert (status.active, status.total) == (2, 2)
    assert status.inspected_on == "spark-01"


def test_a_single_lit_port_is_called_out_with_the_reason():
    """One cable caps near 100 Gb/s on a PCIe Gen5 x4 cage, whatever it negotiated.

    Without this note a half-speed measurement reads as a driver fault.
    """
    status = inspect_ports(FakeRunner(files=SYSFS_ONE_PORT), "spark-01")
    assert (status.active, status.total) == (1, 2)
    note = status.note()
    assert "1 of 2" in note and "Gen5 x4" in note and "100 Gb/s" in note


def test_falls_back_to_netdevs_for_a_connectx_in_ethernet_mode():
    files = {
        "/sys/class/net/enp1s0f0/device/uevent": "DRIVER=mlx5_core\n",
        "/sys/class/net/enp1s0f0/carrier": "1\n",
        "/sys/class/net/enp1s0f0/speed": "200000\n",
        "/sys/class/net/enp1s0f1/device/uevent": "DRIVER=mlx5_core\n",
        "/sys/class/net/enp1s0f1/carrier": "0\n",
        "/sys/class/net/eth0/device/uevent": "DRIVER=r8169\n",  # management NIC, ignored
        "/sys/class/net/eth0/carrier": "1\n",
    }
    status = inspect_ports(FakeRunner(files=files), "spark-01")
    assert (status.active, status.total) == (1, 2)
    assert status.source == "sysfs-net"


def test_no_fabric_visible_is_not_an_error():
    status = inspect_ports(FakeRunner(), "spark-01")
    assert status.known is False
    assert status.note() is None


# --------------------------------------------------------------------------- nccl measurer


def nccl_runner(**kwargs) -> FakeRunner:
    defaults = dict(
        binaries=("mpirun", "all_reduce_perf", "sendrecv_perf"),
        outputs={"all_reduce_perf": ALL_REDUCE_OUT, "sendrecv_perf": SENDRECV_OUT},
        files=SYSFS_TWO_PORTS,
    )
    defaults.update(kwargs)
    return FakeRunner(**defaults)


def test_real_spark_pair_lands_in_the_acceptance_band_with_gdr_off():
    """The headline acceptance criterion: 8 to 12 GB/s, GDR correctly reported off."""
    measurer = NcclMeasurer(nccl_runner(), clock=lambda: T0)
    result = measurer.measure(Endpoint("spark-01", "10.0.0.11"), Endpoint("spark-02", "10.0.0.12"))

    assert result is not None
    assert 8.0 <= result.all_reduce_gbps <= 12.0
    assert result.gpudirect_rdma is False
    assert result.method == "nccl-tests"
    assert result.annotation.estimated is False
    assert result.latency_us == pytest.approx(40.0, abs=1.0)


def test_all_reduce_and_sendrecv_are_measured_separately():
    """Neither is derivable from the other, and both are load-bearing.

    All-reduce decides whether tensor parallel is viable; sendrecv decides
    pipeline handoff and KV transfer.
    """
    result = NcclMeasurer(nccl_runner(), clock=lambda: T0).measure(
        Endpoint("spark-01", "h1"), Endpoint("spark-02", "h2")
    )
    assert result.all_reduce_gbps == pytest.approx(10.23, abs=0.05)
    assert result.sendrecv_gbps == pytest.approx(8.985, abs=0.05)
    assert result.all_reduce_gbps != result.sendrecv_gbps


def test_a_half_run_is_not_a_measurement():
    """all_reduce succeeded, sendrecv produced nothing. Fall through, do not guess."""
    runner = nccl_runner(outputs={"all_reduce_perf": ALL_REDUCE_OUT, "sendrecv_perf": "crashed"})
    assert NcclMeasurer(runner, clock=lambda: T0).measure(Endpoint("a", "h1"), Endpoint("b", "h2")) is None


def test_the_probe_does_not_tamper_with_the_transport_it_is_measuring():
    """Setting NCCL_NET_GDR_LEVEL or NCCL_IB_DISABLE would measure a fiction."""
    runner = nccl_runner()
    NcclMeasurer(runner, clock=lambda: T0).measure(Endpoint("a", "h1"), Endpoint("b", "h2"))
    flat = " ".join(" ".join(call) for call in runner.calls)
    assert "NCCL_NET_GDR_LEVEL" not in flat
    assert "NCCL_IB_DISABLE" not in flat
    assert "NCCL_DEBUG=INFO" in flat  # observability only


def test_single_lit_port_is_noted_on_the_measurement_itself():
    runner = nccl_runner(files=SYSFS_ONE_PORT)
    result = NcclMeasurer(runner, clock=lambda: T0).measure(
        Endpoint("spark-01", "h1", is_local=True), Endpoint("spark-02", "h2")
    )
    assert (result.annotation.active_ports, result.annotation.total_ports) == (1, 2)
    assert result.annotation.ports_inspected_on == "spark-01"
    assert any("Gen5 x4" in note for note in result.annotation.notes)


def test_gdr_off_is_explained_on_the_record():
    result = NcclMeasurer(nccl_runner(), clock=lambda: T0).measure(
        Endpoint("a", "h1"), Endpoint("b", "h2")
    )
    assert any("system memory" in note for note in result.annotation.notes)


def test_unavailable_without_the_binaries():
    assert NcclMeasurer(FakeRunner()).available() is False
    assert NcclMeasurer(FakeRunner(binaries=("mpirun", "all_reduce_perf"))).available() is False
    assert NcclMeasurer(nccl_runner()).available() is True


# --------------------------------------------------------------------------- fallbacks


def ib_runner(**kwargs) -> FakeRunner:
    defaults = dict(
        binaries=("ib_write_bw", "ib_write_lat"),
        outputs={"ib_write_bw": IB_WRITE_BW_OUT, "ib_write_lat": IB_WRITE_LAT_OUT},
        files=SYSFS_TWO_PORTS,
    )
    defaults.update(kwargs)
    return FakeRunner(**defaults)


def test_raw_rdma_is_scaled_before_it_is_reported():
    """24.6 GB/s of raw RDMA is not 24.6 GB/s of NCCL.

    Reporting it as such is the error that makes the ecosystem's defaults wrong,
    so the record carries the scaled figure, the raw figure, and the factor.
    """
    result = IbWriteBwMeasurer(ib_runner(), clock=lambda: T0).measure(
        Endpoint("spark-01", "h1"), Endpoint("spark-02", "h2")
    )
    assert result.method == "ib_write_bw"
    assert result.annotation.raw_gbps == pytest.approx(24.6, abs=0.1)
    assert result.all_reduce_gbps == pytest.approx(24.6 * IB_TO_NCCL_RATIO, abs=0.1)
    assert result.annotation.scale_factor == IB_TO_NCCL_RATIO
    assert result.annotation.estimated is True
    assert any("not NCCL bandwidth" in note for note in result.annotation.notes)


def test_the_ib_estimate_admits_it_cannot_tell_the_collectives_apart():
    result = IbWriteBwMeasurer(ib_runner(), clock=lambda: T0).measure(
        Endpoint("a", "h1"), Endpoint("b", "h2")
    )
    assert result.all_reduce_gbps == result.sendrecv_gbps
    assert any("cannot tell all-reduce from sendrecv" in note for note in result.annotation.notes)


def test_ib_latency_comes_from_ib_write_lat_when_present():
    result = IbWriteBwMeasurer(ib_runner(), clock=lambda: T0).measure(
        Endpoint("a", "h1"), Endpoint("b", "h2")
    )
    assert result.latency_us == pytest.approx(2.05, abs=0.01)


def test_tcp_probe_labels_itself_as_an_upper_bound(monkeypatch):
    monkeypatch.setattr("control_plane.links.measure.tcp_throughput_gbps", lambda *a, **k: 11.8)
    monkeypatch.setattr("control_plane.links.measure.tcp_rtt_us", lambda *a, **k: 55.0)
    result = TcpMeasurer(FakeRunner(files=SYSFS_TWO_PORTS), clock=lambda: T0).measure(
        Endpoint("a", "h1", is_local=True), Endpoint("b", "h2")
    )
    assert result.method == "tcp"
    assert result.annotation.estimated is True
    assert any("upper bound" in note for note in result.annotation.notes)


def test_ladder_prefers_nccl_and_stops_there():
    nccl = ScriptedMeasurer("nccl-tests", link())
    ib = ScriptedMeasurer("ib_write_bw", link(method="ib_write_bw"))
    result = LadderMeasurer([nccl, ib]).measure(Endpoint("a", "h1"), Endpoint("b", "h2"))
    assert result.method == "nccl-tests"
    assert ib.calls == 0


def test_ladder_descends_when_nccl_is_absent_and_labels_the_method_honestly():
    nccl = ScriptedMeasurer("nccl-tests", None, available=False)
    ib = ScriptedMeasurer("ib_write_bw", link(method="ib_write_bw", all_reduce=10.3))
    result = LadderMeasurer([nccl, ib]).measure(Endpoint("a", "h1"), Endpoint("b", "h2"))
    assert result.method == "ib_write_bw"
    assert any("nccl-tests (not installed)" in note for note in result.annotation.notes)


def test_ladder_returns_none_rather_than_fabricating():
    """A missing measurement is a state the planner handles. A wrong one is not."""
    rungs = [ScriptedMeasurer(m, None) for m in ("nccl-tests", "ib_write_bw", "tcp")]
    assert LadderMeasurer(rungs).measure(Endpoint("a", "h1"), Endpoint("b", "h2")) is None


def test_a_rung_that_raises_does_not_take_the_ladder_down():
    boom = ScriptedMeasurer("nccl-tests", None, raises=RuntimeError("mpirun exploded"))
    tcp = ScriptedMeasurer("tcp", link(method="tcp"))
    result = LadderMeasurer([boom, tcp]).measure(Endpoint("a", "h1"), Endpoint("b", "h2"))
    assert result.method == "tcp"


def test_port_state_is_not_claimed_for_a_pair_we_are_not_part_of():
    """The coordinator's own cages say nothing about a link between two other nodes."""
    result = NcclMeasurer(nccl_runner(files=SYSFS_ONE_PORT), clock=lambda: T0).measure(
        Endpoint("spark-02", "h2"), Endpoint("spark-03", "h3")  # neither is us
    )
    assert result.annotation.active_ports is None
    assert result.annotation.total_ports is None
    assert any("not recorded" in note for note in result.annotation.notes)


def test_the_fallback_client_runs_from_the_node_it_is_measuring_from():
    """Running ib_write_bw here would measure coordinator-to-b, then file it as a-to-b."""
    runner = ib_runner()
    IbWriteBwMeasurer(runner, clock=lambda: T0).measure(
        Endpoint("spark-01", "10.0.0.11"), Endpoint("spark-02", "10.0.0.12")
    )
    client_calls = [c for c in runner.calls if c[-1] == "10.0.0.12" and "ib_write_bw" in c]
    assert client_calls, "no client invocation recorded"
    assert all(c[0] == "ssh" and c[1] == "10.0.0.11" for c in client_calls)


def test_the_fallback_client_runs_locally_when_we_are_endpoint_a():
    runner = ib_runner()
    IbWriteBwMeasurer(runner, clock=lambda: T0).measure(
        Endpoint("spark-01", "10.0.0.11", is_local=True), Endpoint("spark-02", "10.0.0.12")
    )
    client_calls = [c for c in runner.calls if c[-1] == "10.0.0.12" and "ib_write_bw" in c]
    assert client_calls and all(c[0] != "ssh" for c in client_calls)


def test_the_builtin_tcp_sink_declines_a_link_this_node_is_not_on():
    """It is driven from this process, so it can only measure a link we are on."""
    result = TcpMeasurer(FakeRunner(), clock=lambda: T0).measure(
        Endpoint("a", "h1"), Endpoint("b", "h2")
    )
    assert result is None


# --------------------------------------------------------------------------- storage


def test_measurements_survive_a_process_restart(tmp_path):
    path = tmp_path / "links.json"
    LinkStore(path).put(link(annotation=LinkAnnotation(notes=("kept",), active_ports=2, total_ports=2)))

    reopened = LinkStore(path)  # as if the coordinator had been bounced
    restored = reopened.get("spark-01", "spark-02")
    assert restored is not None
    assert restored.all_reduce_gbps == 10.2
    assert restored.annotation.notes == ("kept",)
    assert restored.annotation.active_ports == 2


def test_a_pair_is_unordered(tmp_path):
    store = LinkStore(tmp_path / "links.json")
    store.put(link(src="spark-01", dst="spark-02"))
    assert store.get("spark-02", "spark-01") is not None
    store.put(link(src="spark-02", dst="spark-01", all_reduce=7.7))
    assert len(store.all()) == 1  # the same link, not a second one
    assert store.get("spark-01", "spark-02").all_reduce_gbps == 7.7


def test_pair_key_is_symmetric():
    assert pair_key("b", "a") == pair_key("a", "b") == ("a", "b")


def test_a_corrupt_store_starts_empty_instead_of_crashing(tmp_path):
    path = tmp_path / "links.json"
    path.write_text("{ this is not json")
    assert LinkStore(path).all() == []


def test_a_malformed_record_is_skipped_and_the_rest_survive(tmp_path):
    path = tmp_path / "links.json"
    good = to_json(link())
    path.write_text(json.dumps({"version": 1, "links": [good, {"src": "x"}]}))
    assert len(LinkStore(path).all()) == 1


def test_the_record_round_trips_through_json():
    original = link(annotation=LinkAnnotation(estimated=True, raw_gbps=24.6, notes=("a", "b")))
    assert from_json(to_json(original)) == original


def test_an_annotated_link_is_a_link_measurement():
    """Downstream codes against the contract; the annotation is extra, not different."""
    m = link()
    assert isinstance(m, LinkMeasurement)
    assert m.bare() == LinkMeasurement(
        "spark-01", "spark-02", 10.2, 9.0, 40.0, False, T0, "nccl-tests"
    )


# --------------------------------------------------------------------------- service


def test_an_unmeasured_pair_reads_as_none(tmp_path):
    assert service(tmp_path).get("spark-01", "spark-02") is None


def test_a_link_reads_back_in_the_direction_it_was_asked_for(tmp_path):
    svc = service(tmp_path)
    svc.put(link())
    assert (svc.get("spark-02", "spark-01").src, svc.get("spark-02", "spark-01").dst) == (
        "spark-02",
        "spark-01",
    )


def test_staleness_turns_over_at_seven_days(tmp_path):
    now = T0
    svc = service(tmp_path, clock=lambda: now)
    svc.put(link(at=T0))

    now = T0 + SEVEN_DAYS - 60
    assert svc.get("spark-01", "spark-02").annotation.stale is False
    now = T0 + SEVEN_DAYS + 60
    stale = svc.get("spark-01", "spark-02")
    assert stale.annotation.stale is True
    assert stale.all_reduce_gbps == 10.2  # still usable; the UI just marks it


def test_worst_all_reduce_over_three_nodes_returns_the_minimum(tmp_path):
    svc = service(tmp_path)
    svc.put(link("spark-01", "spark-02", all_reduce=10.2))
    svc.put(link("spark-01", "spark-03", all_reduce=9.4))
    svc.put(link("spark-02", "spark-03", all_reduce=11.1))

    worst = svc.worst_all_reduce(["spark-01", "spark-02", "spark-03"])
    assert worst.all_reduce_gbps == 9.4
    assert {worst.src, worst.dst} == {"spark-01", "spark-03"}


def test_worst_all_reduce_is_none_when_any_pair_is_unmeasured(tmp_path):
    """Better to plan conservatively than to plan on the pairs we happen to know."""
    svc = service(tmp_path)
    svc.put(link("spark-01", "spark-02", all_reduce=10.2))
    svc.put(link("spark-01", "spark-03", all_reduce=9.4))
    assert svc.worst_all_reduce(["spark-01", "spark-02", "spark-03"]) is None


def test_worst_all_reduce_ignores_duplicate_node_ids(tmp_path):
    svc = service(tmp_path)
    svc.put(link("spark-01", "spark-02", all_reduce=10.2))
    assert svc.worst_all_reduce(["spark-01", "spark-02", "spark-01"]).all_reduce_gbps == 10.2


def test_worst_all_reduce_of_fewer_than_two_nodes_is_none(tmp_path):
    svc = service(tmp_path)
    assert svc.worst_all_reduce(["spark-01"]) is None
    assert svc.worst_all_reduce([]) is None


def test_manual_entry_is_labelled_manual_and_flagged_as_unmeasured(tmp_path):
    svc = service(tmp_path)
    svc.put(link(all_reduce=25.0, method="nccl-tests"))  # caller claims a method
    stored = svc.get("spark-01", "spark-02")
    assert stored.method == "manual"
    assert stored.annotation.estimated is True
    assert any("entered by hand" in note for note in stored.annotation.notes)


def test_measure_stores_what_it_measured(tmp_path):
    svc = service(tmp_path, measurer=ScriptedMeasurer("nccl-tests", link()))
    result = svc.measure("spark-01", "spark-02")
    assert result.all_reduce_gbps == 10.2
    assert svc.get("spark-01", "spark-02") is not None


def test_measure_returns_none_when_the_whole_ladder_fails(tmp_path):
    svc = service(tmp_path, measurer=ScriptedMeasurer("nccl-tests", None))
    assert svc.measure("spark-01", "spark-02") is None
    assert svc.get("spark-01", "spark-02") is None


def test_a_node_has_no_link_to_itself(tmp_path):
    with pytest.raises(ValueError):
        service(tmp_path).measure("spark-01", "spark-01")


def test_measure_all_covers_every_pair(tmp_path):
    class PerPair:
        method = "nccl-tests"

        def available(self):
            return True

        def measure(self, a, b):
            return link(a.node_id, b.node_id)

    svc = service(tmp_path, measurer=PerPair())
    results = svc.measure_all(["spark-01", "spark-02", "spark-03"])
    assert len(results) == 3
    assert {pair_key(r.src, r.dst) for r in results} == {
        ("spark-01", "spark-02"),
        ("spark-01", "spark-03"),
        ("spark-02", "spark-03"),
    }


def test_probes_never_overlap(tmp_path):
    """Two collectives over one fabric measure each other's interference."""
    concurrent = 0
    peak = 0
    guard = threading.Lock()

    class Watcher:
        method = "nccl-tests"

        def available(self):
            return True

        def measure(self, a, b):
            nonlocal concurrent, peak
            with guard:
                concurrent += 1
                peak = max(peak, concurrent)
            time.sleep(0.05)
            with guard:
                concurrent -= 1
            return link(a.node_id, b.node_id)

    svc = service(tmp_path, measurer=Watcher())
    threads = [
        threading.Thread(target=svc.measure, args=("spark-01", f"spark-{i:02d}")) for i in range(2, 6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == 1


def test_a_measurement_in_progress_does_not_block_reads(tmp_path):
    """The UI and the planner keep answering through a thirty-second probe."""
    started = threading.Event()
    release = threading.Event()

    class Slow:
        method = "nccl-tests"

        def available(self):
            return True

        def measure(self, a, b):
            started.set()
            release.wait(5.0)
            return link(a.node_id, b.node_id)

    svc = service(tmp_path, measurer=Slow())
    svc.put(link("spark-01", "spark-02", all_reduce=10.2))

    prober = threading.Thread(target=svc.measure, args=("spark-03", "spark-04"))
    prober.start()
    try:
        assert started.wait(2.0)
        began = time.monotonic()
        assert svc.get("spark-01", "spark-02").all_reduce_gbps == 10.2
        assert len(svc.all()) == 1
        assert svc.worst_all_reduce(["spark-01", "spark-02"]) is not None
        assert svc.measuring() == [("spark-03", "spark-04")]
        assert time.monotonic() - began < 0.5  # not waiting on the probe
    finally:
        release.set()
        prober.join(timeout=5.0)
    assert svc.measuring() == []


def test_forget_returns_a_pair_to_the_unmeasured_state(tmp_path):
    svc = service(tmp_path)
    svc.put(link())
    assert svc.forget("spark-02", "spark-01") is True
    assert svc.get("spark-01", "spark-02") is None
    assert svc.forget("spark-01", "spark-02") is False


def test_the_data_plane_address_wins_over_the_management_ip(tmp_path):
    """The registry knows the management LAN. Measuring it answers the wrong question."""
    seen = {}

    class Capture:
        method = "nccl-tests"

        def available(self):
            return True

        def measure(self, a, b):
            seen["hosts"] = (a.host, b.host)
            return link(a.node_id, b.node_id)

    class Registry:
        def get_node(self, node_id):
            from tests.fixtures import SPARK_01, SPARK_02

            profile = {"spark-01": SPARK_01, "spark-02": SPARK_02}[node_id]
            return type("S", (), {"profile": profile})()

    svc = service(
        tmp_path,
        measurer=Capture(),
        registry=Registry(),
        data_plane_addresses={"spark-01": "192.168.100.1"},
    )
    svc.measure("spark-01", "spark-02")
    assert seen["hosts"] == ("192.168.100.1", "192.168.11.14")  # override, then registry


# --------------------------------------------------------------------------- stub


def test_the_stub_returns_the_fixture_measurement():
    """Agent E cannot start without this, and it must be real contract types."""
    stub = StubLinkService(clock=lambda: T0)
    measured = stub.get("spark-01", "spark-02")
    assert isinstance(measured, LinkMeasurement)
    assert measured.all_reduce_gbps == LINK_SPARK_10G.all_reduce_gbps == 10.2
    assert measured.sendrecv_gbps == LINK_SPARK_10G.sendrecv_gbps == 9.0
    assert measured.latency_us == LINK_SPARK_10G.latency_us == 40.0
    assert measured.gpudirect_rdma is LINK_SPARK_10G.gpudirect_rdma is False
    assert measured.method == LINK_SPARK_10G.method == "nccl-tests"


def test_the_stub_can_present_an_unmeasured_pair():
    stub = StubLinkService(clock=lambda: T0, unmeasured={("spark-01", "spark-03")})
    assert stub.get("spark-03", "spark-01") is None
    assert stub.worst_all_reduce(["spark-01", "spark-02", "spark-03"]) is None


def test_the_stub_satisfies_the_same_surface_as_the_service(tmp_path):
    required = ("get", "worst_all_reduce", "measure", "measure_all", "all", "put")
    real = service(tmp_path)
    for name in required:
        assert callable(getattr(StubLinkService(), name))
        assert callable(getattr(real, name))


def test_the_stub_worst_all_reduce_still_returns_the_minimum():
    stub = StubLinkService(clock=lambda: T0)
    stub.put(link("spark-01", "spark-03", all_reduce=4.1))
    worst = stub.worst_all_reduce(["spark-01", "spark-02", "spark-03"])
    assert worst.all_reduce_gbps == 4.1
