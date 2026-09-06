"""Is GPUDirect RDMA actually on?

This is the flag that explains the number. With GDR off, a GPU tensor is copied
into system memory before the NIC ever sees it, which is why NCCL delivers
roughly 10 GB/s on a link that shows 24.6 GB/s to raw `ib_write_bw`.

It is also the flag most likely to change under us. If a driver update turns GDR
on, the bandwidth moves and the planner's answer flips from pipeline to tensor
parallel. That is correct behaviour, and it is the reason nothing downstream may
hold a constant instead of reading this.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .runner import CommandRunner

# NCCL says so itself, in as many words, when NCCL_DEBUG=INFO is set.
_EXPLICIT_RE = re.compile(r"GPU Direct RDMA (Enabled|Disabled)", re.IGNORECASE)
# ...and marks the transport it chose per channel.
_GDRDMA_RE = re.compile(r"via NET/\S*GDRDMA", re.IGNORECASE)

# The kernel modules that make GDR possible at all. Present is necessary, not
# sufficient, so this only ever counts as weak evidence.
_PEERMEM_PATHS = (
    "/sys/module/nvidia_peermem",
    "/sys/module/nv_peer_mem",
    "/sys/kernel/mm/memory_peers/nv_mem/version",
)


@dataclass(frozen=True)
class GdrEvidence:
    enabled: bool
    source: str
    """How we know. Goes into the record so the claim is traceable."""

    detail: str | None = None


def detect_gdr(nccl_output: str | None = None, runner: CommandRunner | None = None) -> GdrEvidence:
    """Decide GDR state from the best evidence available.

    NCCL's own report beats anything we can infer from the host, because NCCL is
    the thing whose bandwidth we care about. Host inspection is the fallback for
    when nccl-tests never ran.
    """
    if nccl_output:
        found = _EXPLICIT_RE.search(nccl_output)
        if found:
            enabled = found.group(1).lower() == "enabled"
            return GdrEvidence(
                enabled=enabled,
                source="nccl-debug",
                detail=found.group(0),
            )
        if _GDRDMA_RE.search(nccl_output):
            return GdrEvidence(
                enabled=True,
                source="nccl-debug",
                detail="NCCL selected a GDRDMA transport for at least one channel",
            )
        if "NET/IB" in nccl_output or "NET/Socket" in nccl_output:
            # NCCL told us about its transport and never mentioned GDR.
            return GdrEvidence(
                enabled=False,
                source="nccl-debug",
                detail="NCCL reported its transport without any GDRDMA path",
            )

    if runner is not None:
        peer = _peer_memory_module(runner)
        if peer is not None:
            return GdrEvidence(
                enabled=True,
                source="peer-memory-module",
                detail=f"{peer} present; NCCL did not report, so this is inferred",
            )
        return GdrEvidence(
            enabled=False,
            source="peer-memory-module",
            detail="no GPU peer-memory kernel module loaded",
        )

    return GdrEvidence(
        enabled=False,
        source="unknown",
        detail="no evidence either way; reported disabled rather than assumed on",
    )


def _peer_memory_module(runner: CommandRunner) -> str | None:
    for path in _PEERMEM_PATHS:
        if runner.read_text(path) is not None or runner.glob(path):
            return path
    modules = runner.read_text("/proc/modules")
    if modules:
        for name in ("nvidia_peermem", "nv_peer_mem"):
            if re.search(rf"^{name}\b", modules, re.MULTILINE):
                return name
    return None
