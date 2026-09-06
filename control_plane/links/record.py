"""The persisted link record.

`LinkMeasurement` is frozen in the contracts and gains no fields here. Everything
an operator needs beyond those seven values -- whether the figure was derived
rather than directly measured, which QSFP cages were lit, what told us about
GPUDirect RDMA -- rides in a `LinkAnnotation` attached to an `AnnotatedLink`,
which *is* a `LinkMeasurement` by subclassing. Downstream code that only knows
the contract keeps working; code that wants the caveats can ask for them.

The caveats are not decoration. A number obtained from `ib_write_bw` and scaled
is a different kind of fact from one NCCL actually produced, and the difference
has to survive the trip to the UI.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace

from control_plane.contracts.hardware import LinkMeasurement

try:  # the day-0 constant if it exists, so we never drift from the rest of the tree
    from control_plane.contracts.constants import LINK_STALE_SECONDS as STALE_AFTER_S
except ImportError:  # pragma: no cover - the window is specified in Agent B's brief
    STALE_AFTER_S = 7 * 24 * 60 * 60

# Ratio of NCCL-effective bandwidth to raw RDMA bandwidth, derived from the
# GDR-disabled path where GPU tensors transit system memory before reaching the NIC.
# This ratio is applied regardless of whether GDR is enabled on the actual link;
# when GDR is enabled, the scaled figure may understate true NCCL bandwidth.
# Reporting raw RDMA as if it were NCCL bandwidth is the exact error that makes
# the ecosystem's defaults wrong; this factor is the correction, and the record
# says out loud that it was applied.
IB_TO_NCCL_RATIO = 0.42


@dataclass(frozen=True)
class LinkAnnotation:
    """Everything true about a measurement that the frozen contract cannot hold."""

    stale: bool = False
    estimated: bool = False
    """True when the figure was derived from a proxy rather than from NCCL itself."""
    raw_gbps: float | None = None
    """The proxy's own number, before any scaling. Kept so the scaling is auditable."""
    scale_factor: float | None = None
    active_ports: int | None = None
    total_ports: int | None = None
    ports_inspected_on: str | None = None
    """Which node's fabric we could see. Port state is a local observation."""
    gdr_detected_by: str | None = None
    duration_s: float | None = None
    notes: tuple[str, ...] = ()

    def with_note(self, note: str) -> LinkAnnotation:
        if note in self.notes:
            return self
        return replace(self, notes=self.notes + (note,))


@dataclass(frozen=True)
class AnnotatedLink(LinkMeasurement):
    """A `LinkMeasurement` that also carries its caveats."""

    annotation: LinkAnnotation = field(default_factory=LinkAnnotation)

    def bare(self) -> LinkMeasurement:
        """The contract type exactly, for callers that serialize by field list."""
        return LinkMeasurement(
            src=self.src,
            dst=self.dst,
            all_reduce_gbps=self.all_reduce_gbps,
            sendrecv_gbps=self.sendrecv_gbps,
            latency_us=self.latency_us,
            gpudirect_rdma=self.gpudirect_rdma,
            measured_at=self.measured_at,
            method=self.method,
        )

    def is_stale(self, now: float | None = None) -> bool:
        """Older than the staleness window.

        The planner may still use a stale measurement; the UI marks it. Refusing
        to plan on an old number is worse than planning on one and saying so.
        """
        reference = time.time() if now is None else now
        return (reference - self.measured_at) > STALE_AFTER_S

    def freshened(self, now: float | None = None) -> AnnotatedLink:
        """Recompute `stale` against the clock. Staleness is a read-time fact."""
        stale = self.is_stale(now)
        if stale == self.annotation.stale:
            return self
        return replace(self, annotation=replace(self.annotation, stale=stale))

    def oriented(self, src: str, dst: str) -> AnnotatedLink:
        """Same link, presented from the caller's direction.

        Storage is unordered; a caller asking get("spark-02", "spark-01") should
        not have to notice that we filed it the other way round.
        """
        if (self.src, self.dst) == (src, dst):
            return self
        return replace(self, src=src, dst=dst)


def annotate(m: LinkMeasurement, annotation: LinkAnnotation | None = None) -> AnnotatedLink:
    """Lift a plain contract measurement into an annotated one."""
    if isinstance(m, AnnotatedLink):
        return m if annotation is None else replace(m, annotation=annotation)
    return AnnotatedLink(
        src=m.src,
        dst=m.dst,
        all_reduce_gbps=m.all_reduce_gbps,
        sendrecv_gbps=m.sendrecv_gbps,
        latency_us=m.latency_us,
        gpudirect_rdma=m.gpudirect_rdma,
        measured_at=m.measured_at,
        method=m.method,
        annotation=annotation or LinkAnnotation(),
    )


def pair_key(a: str, b: str) -> tuple[str, str]:
    """Links are undirected. spark-01/spark-02 and spark-02/spark-01 are one link."""
    return (a, b) if a <= b else (b, a)


def to_json(link: AnnotatedLink) -> dict:
    ann = link.annotation
    return {
        "src": link.src,
        "dst": link.dst,
        "all_reduce_gbps": link.all_reduce_gbps,
        "sendrecv_gbps": link.sendrecv_gbps,
        "latency_us": link.latency_us,
        "gpudirect_rdma": link.gpudirect_rdma,
        "measured_at": link.measured_at,
        "method": link.method,
        "annotation": {
            "estimated": ann.estimated,
            "raw_gbps": ann.raw_gbps,
            "scale_factor": ann.scale_factor,
            "active_ports": ann.active_ports,
            "total_ports": ann.total_ports,
            "ports_inspected_on": ann.ports_inspected_on,
            "gdr_detected_by": ann.gdr_detected_by,
            "duration_s": ann.duration_s,
            "notes": list(ann.notes),
        },
    }


def from_json(d: dict) -> AnnotatedLink:
    raw = d.get("annotation") or {}
    ann = LinkAnnotation(
        stale=False,  # always recomputed against the current clock on read
        estimated=bool(raw.get("estimated", False)),
        raw_gbps=raw.get("raw_gbps"),
        scale_factor=raw.get("scale_factor"),
        active_ports=raw.get("active_ports"),
        total_ports=raw.get("total_ports"),
        ports_inspected_on=raw.get("ports_inspected_on"),
        gdr_detected_by=raw.get("gdr_detected_by"),
        duration_s=raw.get("duration_s"),
        notes=tuple(raw.get("notes", ())),
    )
    return AnnotatedLink(
        src=str(d["src"]),
        dst=str(d["dst"]),
        all_reduce_gbps=float(d["all_reduce_gbps"]),
        sendrecv_gbps=float(d["sendrecv_gbps"]),
        latency_us=float(d["latency_us"]),
        gpudirect_rdma=bool(d["gpudirect_rdma"]),
        measured_at=float(d["measured_at"]),
        method=str(d["method"]),
        annotation=ann,
    )
