"""Link measurement.

What the interconnect actually delivers, rather than what the spec sheet claims.
The gap is the product: a GB10 pair negotiates 200GbE, shows roughly 24.6 GB/s
to raw `ib_write_bw`, and gives NCCL roughly 10 GB/s all-reduce, because
GPUDirect RDMA is off and GPU tensors route through system memory on their way
to the NIC. Every framework in the ecosystem plans against the nameplate, which
is why NVIDIA's own playbook recommends tensor parallel for two Sparks and loses
to pipeline parallel under batched load.

Nothing downstream may hold this number as a constant. If a driver update turns
GDR on, the measurement moves and the plan should flip. Read it from here, every
time.
"""

from .measure import (
    Endpoint,
    IbWriteBwMeasurer,
    LadderMeasurer,
    Measurer,
    NcclMeasurer,
    TcpMeasurer,
    default_measurer,
)
from .probe_server import DEFAULT_PROBE_PORT, ProbeServer, start_probe_server
from .qsfp import PortInfo, PortStatus, inspect_ports
from .gdr import GdrEvidence, detect_gdr
from .record import IB_TO_NCCL_RATIO, AnnotatedLink, LinkAnnotation, annotate, pair_key
from .service import LinkService
from .store import LinkStore
from .stub import StubLinkService

__all__ = [
    "AnnotatedLink",
    "DEFAULT_PROBE_PORT",
    "Endpoint",
    "GdrEvidence",
    "IB_TO_NCCL_RATIO",
    "IbWriteBwMeasurer",
    "LadderMeasurer",
    "LinkAnnotation",
    "LinkService",
    "LinkStore",
    "Measurer",
    "NcclMeasurer",
    "PortInfo",
    "PortStatus",
    "ProbeServer",
    "StubLinkService",
    "TcpMeasurer",
    "annotate",
    "default_measurer",
    "detect_gdr",
    "inspect_ports",
    "pair_key",
    "start_probe_server",
]
