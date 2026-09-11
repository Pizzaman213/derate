"""Day-0 stub so the planner is never blocked on real hardware.

Returns the fixture measurement for every pair: what two Sparks actually deliver
with GPUDirect RDMA off. It satisfies the whole `LinkPort` surface and returns
real contract types, because a stub that returns something else is worse than no
stub at all.

The real `LinkService` landed and `node.py` wires it in production behind
`GatewayDeps(strict=True, ...)`, which refuses to start if any port -- this
one included -- is still missing. This file stayed anyway: `__init__.py`
exports `StubLinkService` as this package's public fake, and
`tests/unit/test_links.py` reaches for it wherever a test needs a `LinkPort`
without real hardware behind it. The values are duplicated from
`tests/fixtures/links.py` rather than imported, because shipping code does
not depend on the test tree.
"""

from __future__ import annotations

import time

from control_plane.contracts.hardware import LinkMeasurement

from .record import AnnotatedLink, LinkAnnotation, annotate, pair_key

STUB_ALL_REDUCE_GBPS = 10.2
STUB_SENDRECV_GBPS = 9.0
STUB_LATENCY_US = 40.0
STUB_GPUDIRECT_RDMA = False
STUB_METHOD = "nccl-tests"


class StubLinkService:
    """A `LinkPort` that always answers, for wiring downstream work up early."""

    def __init__(self, clock=time.time, *, unmeasured: set[tuple[str, str]] | None = None) -> None:
        self._clock = clock
        self._overrides: dict[tuple[str, str], AnnotatedLink] = {}
        # Lets a downstream agent exercise the unmeasured path, which is a real
        # state the planner has to handle and the easiest one to forget.
        self._unmeasured = {pair_key(*p) for p in (unmeasured or set())}

    def get(self, a: str, b: str) -> LinkMeasurement | None:
        if a == b:
            return None
        key = pair_key(a, b)
        if key in self._unmeasured:
            return None
        if key in self._overrides:
            return self._overrides[key].oriented(a, b)
        return self._fixture(a, b)

    def record(self, a: str, b: str) -> AnnotatedLink | None:
        result = self.get(a, b)
        return result if isinstance(result, AnnotatedLink) else None

    def worst_all_reduce(self, node_ids: list[str]) -> LinkMeasurement | None:
        unique = list(dict.fromkeys(node_ids))
        if len(unique) < 2:
            return None
        worst: LinkMeasurement | None = None
        for i, a in enumerate(unique):
            for b in unique[i + 1 :]:
                link = self.get(a, b)
                if link is None:
                    return None
                if worst is None or link.all_reduce_gbps < worst.all_reduce_gbps:
                    worst = link
        return worst

    def measure(self, a: str, b: str) -> LinkMeasurement | None:
        if a == b:
            raise ValueError("a link needs two distinct nodes")
        self._unmeasured.discard(pair_key(a, b))
        return self.get(a, b)

    def measure_all(self, node_ids: list[str]) -> list[LinkMeasurement]:
        unique = list(dict.fromkeys(node_ids))
        out: list[LinkMeasurement] = []
        for i, a in enumerate(unique):
            for b in unique[i + 1 :]:
                link = self.measure(a, b)
                if link is not None:
                    out.append(link)
        return out

    def all(self) -> list[LinkMeasurement]:
        return list(self._overrides.values())

    def put(self, m: LinkMeasurement) -> None:
        link = annotate(m)
        self._overrides[pair_key(m.src, m.dst)] = AnnotatedLink(
            src=m.src,
            dst=m.dst,
            all_reduce_gbps=m.all_reduce_gbps,
            sendrecv_gbps=m.sendrecv_gbps,
            latency_us=m.latency_us,
            gpudirect_rdma=m.gpudirect_rdma,
            measured_at=m.measured_at if m.measured_at > 0 else self._clock(),
            method="manual",
            annotation=link.annotation.with_note("entered by hand, not probed"),
        )
        self._unmeasured.discard(pair_key(m.src, m.dst))

    def measuring(self) -> list[tuple[str, str]]:
        return []

    def forget(self, a: str, b: str) -> bool:
        self._unmeasured.add(pair_key(a, b))
        return self._overrides.pop(pair_key(a, b), None) is not None

    def _fixture(self, a: str, b: str) -> AnnotatedLink:
        return AnnotatedLink(
            src=a,
            dst=b,
            all_reduce_gbps=STUB_ALL_REDUCE_GBPS,
            sendrecv_gbps=STUB_SENDRECV_GBPS,
            latency_us=STUB_LATENCY_US,
            gpudirect_rdma=STUB_GPUDIRECT_RDMA,
            measured_at=self._clock(),
            method=STUB_METHOD,
            annotation=LinkAnnotation(
                gdr_detected_by="stub",
                notes=("stub measurement; replace with a real probe before believing it",),
            ),
        )
