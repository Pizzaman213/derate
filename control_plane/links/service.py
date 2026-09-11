"""`LinkService` -- the component behind `LinkPort`.

Reads are lock-free and always answerable: the store swaps whole mappings, so a
thirty-second NCCL run never stalls the UI or the planner. Measurement itself is
serialised, because it is disruptive and two collectives running over the same
fabric at once would each report the other's interference as the link's speed.
"""

from __future__ import annotations

import functools
import logging
import os
import socket
import threading
import time
from dataclasses import replace

from control_plane.contracts.hardware import LinkMeasurement

from .measure import Endpoint, LadderMeasurer, default_measurer
from .record import AnnotatedLink, LinkAnnotation, annotate, pair_key
from .store import LinkStore

from control_plane.paths import CONTAINER_DATA_DIR, data_dir

log = logging.getLogger(__name__)

#: Only used to key a calibration record when no image is configured.
_DEFAULT_IMAGE = "ghcr.io/pizzaman213/derate/vllm-audio:latest"

#: Kept for callers that import it by name. The *resolved* root comes from
#: control_plane.paths, which falls back off this when /data is not writable --
#: and the value comes from there too, so the alias cannot outlive the thing it
#: aliases.
DEFAULT_DATA_DIR = str(CONTAINER_DATA_DIR)
DEFAULT_STORE_NAME = "links.json"


class LinkService:
    """Measures the interconnect, remembers what it found, and answers questions.

    `registry` is only ever used to turn a node id into an address; the service
    holds no opinion about node health, which is the registry's to have.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        registry=None,
        measurer: LadderMeasurer | None = None,
        clock=time.time,
        *,
        local_node_id: str | None = None,
        data_plane_addresses: dict[str, str] | None = None,
        auto_calibrate: bool | None = None,
        collective_fn=None,
    ) -> None:
        self._store = LinkStore(path or _default_store_path())
        self._registry = registry
        self._measurer = measurer or default_measurer(clock=clock)
        self._clock = clock
        self._local_node_id = local_node_id or os.environ.get("DERATE_NODE_ID")
        self._data_plane = dict(data_plane_addresses or {})
        #: How a calibration actually times a collective. Injectable for the
        #: same reason `measurer` and `clock` are: the real one starts two
        #: containers and ssh's to a peer, and a unit test that did that would
        #: be a test whose answer depends on what else is on the machine.
        self._collective_fn = collective_fn
        #: Whether a measurement of a never-calibrated pair also calibrates it.
        #: On by default -- the whole point is that it is automatic -- and off
        #: in every test, because a unit test must not shell out to docker.
        #: `DERATE_NCCL_AUTOCALIBRATE=0` turns it off on a box where the extra
        #: minutes at bring-up are unwelcome.
        self.auto_calibrate = (
            auto_calibrate
            if auto_calibrate is not None
            else os.environ.get("DERATE_NCCL_AUTOCALIBRATE", "1") not in ("0", "false", "no")
        )
        # Serialises probes without touching readers.
        self._measure_lock = threading.Lock()
        self._inflight: set[tuple[str, str]] = set()
        self._inflight_lock = threading.Lock()

    # ------------------------------------------------------------------ reads

    def get(self, a: str, b: str) -> LinkMeasurement | None:
        """The measurement for a pair, or None if it has never been probed.

        None is a real answer, not an error. The planner treats an unmeasured
        link as unknown and runs conservatively rather than assuming a number.
        """
        link = self._store.get(a, b)
        if link is None:
            return None
        return link.freshened(self._clock()).oriented(a, b)

    def record(self, a: str, b: str) -> AnnotatedLink | None:
        """Same thing, typed so callers can reach the annotation without a cast."""
        result = self.get(a, b)
        return result if isinstance(result, AnnotatedLink) else None

    def all(self) -> list[LinkMeasurement]:
        now = self._clock()
        return [link.freshened(now) for link in self._store.all()]

    def worst_all_reduce(self, node_ids: list[str]) -> LinkMeasurement | None:
        """The slowest pairwise link across a set.

        The slowest link governs any collective over the set, so this is the
        figure the planner reasons with. Returns None if *any* pair is
        unmeasured: planning on a partial picture would silently use the fastest
        links we happen to know about and ignore the one that will actually hurt.

        Fewer than two distinct nodes means there is no link to govern anything,
        which is also None -- callers check node count before they check bandwidth.
        """
        unique = list(dict.fromkeys(node_ids))
        if len(unique) < 2:
            return None

        now = self._clock()
        worst: AnnotatedLink | None = None
        for i, a in enumerate(unique):
            for b in unique[i + 1 :]:
                link = self._store.get(a, b)
                if link is None:
                    log.debug("worst_all_reduce: %s/%s unmeasured, returning None", a, b)
                    return None
                if worst is None or link.all_reduce_gbps < worst.all_reduce_gbps:
                    worst = link
        return None if worst is None else worst.freshened(now)

    def measuring(self) -> list[tuple[str, str]]:
        """Pairs with a probe in flight, so the UI can say so."""
        with self._inflight_lock:
            return sorted(self._inflight)

    # ------------------------------------------------------------------ writes

    def measure(self, a: str, b: str) -> LinkMeasurement | None:
        """Probe a pair now, store the result, and return it.

        Returns None when every rung of the ladder fails. `LinkPort` types this
        as `LinkMeasurement`, but the alternative to None is inventing a number,
        and the whole component exists because invented numbers are what put the
        rest of the ecosystem wrong. The planner already handles a missing link.
        """
        if a == b:
            raise ValueError("a link needs two distinct nodes")

        endpoint_a = self._endpoint(a)
        endpoint_b = self._endpoint(b)
        key = pair_key(a, b)

        with self._inflight_lock:
            self._inflight.add(key)
        try:
            # Serialised on purpose: concurrent probes measure each other.
            with self._measure_lock:
                started = time.monotonic()
                link = self._measurer.measure(endpoint_a, endpoint_b)
                if link is None:
                    return None
                link = replace(link, src=a, dst=b)
                if link.annotation.duration_s is None:
                    link = replace(
                        link,
                        annotation=replace(link.annotation, duration_s=round(time.monotonic() - started, 2)),
                    )
                self._store.put(link)
                measured = link.freshened(self._clock())
        finally:
            with self._inflight_lock:
                self._inflight.discard(key)

        # Calibrate on the same occasions a measurement happens -- bring-up and
        # on demand -- and only when this pair has nothing stored for this
        # image. That keeps the discipline the measurement path already has
        # ("never on a timer") while making the tuning automatic: a new pair,
        # or the same pair under a new image, calibrates once and then never
        # again until something it was keyed on changes.
        #
        # OUTSIDE the lock above, because `calibrate` takes it itself. Failure
        # is logged and swallowed: a link measurement that succeeded must not
        # be reported as failed because the tuning that followed it did not.
        if self.auto_calibrate and measured is not None:
            img = os.environ.get("DERATE_VLLM_IMAGE") or _DEFAULT_IMAGE
            try:
                if not self._calibrated(a, b, img):
                    self.calibrate(a, b, image=img)
            except Exception:
                log.warning("auto-calibration of %s/%s failed", a, b, exc_info=True)
        return measured

    #: The settings a calibration tries. One knob at a time against the
    #: default, because a combination sweep on a fabric this slow to set up
    #: buys less than it costs -- and because the winner has to be explicable.
    #:
    #: Deliberately short. `NCCL_NET_OVERHEAD` is absent because it was swept
    #: across 1/5/13/25/50 on this estate and moved nothing outside the repeat
    #: noise, and `NCCL_PROTO` is absent because forcing the small-message
    #: protocol costs 6x at 4 MiB -- NCCL already selects by size and a global
    #: override throws that away. What remains is the one that paid: capping
    #: channels, on a fabric with two rails and no GPUDirect RDMA, where the
    #: default count thrashes a path that has only two ways out.
    CALIBRATION_ENVS: tuple = (
        {},
        {"NCCL_MAX_NCHANNELS": "1"},
        {"NCCL_MAX_NCHANNELS": "2"},
        {"NCCL_MAX_NCHANNELS": "4"},
    )

    def calibrate(self, a: str, b: str, *, image: str | None = None) -> int:
        """Time this pair's collectives under each candidate setting, and store
        the rows. Returns how many records were written.

        **Serialised on the same lock as `measure`**, for the identical reason
        its docstring gives: every run saturates the fabric, so two at once
        produce two wrong numbers instead of one right one. It is slower than a
        measurement -- one run per candidate -- which is why it is not on a
        timer and why `measure` only triggers it when there is nothing stored.

        Never raises. A fabric that will not converge is recorded as a failure
        row, because "nobody tried" and "tried and it would not run" are
        different answers and an absent record cannot tell them apart.
        """
        from control_plane import measurements as M
        from control_plane.links import collective

        run = self._collective_fn or collective.run_collective
        endpoint_a, endpoint_b = self._endpoint(a), self._endpoint(b)
        local, peer = (endpoint_a, endpoint_b) if endpoint_a.is_local else (endpoint_b, endpoint_a)
        if not local.is_local:
            # Both remote: this coordinator is not an endpoint, so it cannot be
            # rank 0 and has nothing honest to say about the pair.
            log.debug("calibrate %s/%s: neither node is local, skipping", a, b)
            return 0

        img = image or os.environ.get("DERATE_VLLM_IMAGE") or _DEFAULT_IMAGE
        iface = collective.local_interface_for(local.host)
        sizes = [M.DECODE_COLLECTIVE_BYTES, M.BULK_COLLECTIVE_BYTES]
        key = pair_key(a, b)
        written = 0

        with self._inflight_lock:
            self._inflight.add(key)
        try:
            with self._measure_lock:
                stamp = self._clock()
                for env in self.CALIBRATION_ENVS:
                    result = run(
                        image=img, sizes=sizes, master_addr=local.host,
                        peer_host=peer.host, iface=iface, env=env,
                    )
                    if not result.ok:
                        log.info("calibrate %s/%s env=%s: %s", a, b, env or "default", result.error)
                        if M.save_nccl(M.NcclRecord(
                            src=local.node_id, dst=peer.node_id,
                            nccl_version=result.nccl or "unknown", image=img,
                            size_band=0, microseconds=0.0, busbw_gbps=0.0,
                            env=dict(env), measured_at=stamp, error=result.error,
                        )):
                            written += 1
                        # A default that cannot run means no candidate can be
                        # compared against anything. Stop rather than spend the
                        # fabric on rows nothing will read.
                        if not env:
                            break
                        continue
                    for row in result.rows:
                        if M.save_nccl(M.NcclRecord(
                            src=local.node_id, dst=peer.node_id,
                            nccl_version=result.nccl or "unknown", image=img,
                            size_band=M.band(row["bytes"]),
                            microseconds=row["us"], busbw_gbps=row["busbw_gbps"],
                            env=dict(env), measured_at=stamp,
                        )):
                            written += 1
        except Exception:
            log.warning("calibration of %s/%s failed", a, b, exc_info=True)
        finally:
            with self._inflight_lock:
                self._inflight.discard(key)

        if written:
            log.info(
                "calibrated %s/%s: %d rows, chose %s",
                a, b, written, M.tuning_env(local.node_id, peer.node_id, image=img) or "the default",
            )
        return written

    def _calibrated(self, a: str, b: str, image: str) -> bool:
        """Whether this pair already has rows for this image."""
        from control_plane import measurements as M

        return bool(M.matching_nccl(a, b, image=image))

    def measure_all(self, node_ids: list[str]) -> list[LinkMeasurement]:
        """Probe every pair in the set.

        Sequential, not parallel. Every probe saturates the fabric by design, so
        running two at once would produce two wrong numbers instead of one right
        one. This is why measurement happens at bring-up and on demand, and
        never on a timer.
        """
        unique = list(dict.fromkeys(node_ids))
        results: list[LinkMeasurement] = []
        for i, a in enumerate(unique):
            for b in unique[i + 1 :]:
                link = self.measure(a, b)
                if link is not None:
                    results.append(link)
                else:
                    log.warning("measure_all: no usable measurement for %s/%s", a, b)
        return results

    def put(self, m: LinkMeasurement) -> None:
        """Record a hand-entered figure.

        Stored with `method="manual"` and flagged as not measured, because a
        number somebody typed is a claim and the UI has to be able to say so.
        """
        if m.src == m.dst:
            raise ValueError("a link needs two distinct nodes")

        link = annotate(m)
        annotation = link.annotation.with_note(
            "entered by hand, not probed; re-measure to replace it with a real figure"
        )
        self._store.put(
            replace(
                link,
                method="manual",
                measured_at=m.measured_at if m.measured_at > 0 else self._clock(),
                annotation=replace(annotation, estimated=True),
            )
        )

    def forget(self, a: str, b: str) -> bool:
        """Drop a measurement, returning it to the unmeasured state."""
        return self._store.delete(a, b)

    # ------------------------------------------------------------------ internals

    def _endpoint(self, node_id: str) -> Endpoint:
        """Where a probe should dial to reach this node.

        Prefers an explicitly configured data-plane address, because the
        management IP the registry knows may not be on the ConnectX-7 fabric at
        all -- and measuring the management LAN would answer the wrong question.
        """
        host = self._data_plane.get(node_id) or os.environ.get(f"DERATE_DATAPLANE_{_env_key(node_id)}")
        if not host and self._registry is not None:
            state = self._registry.get_node(node_id)
            if state is not None:
                host = state.profile.address or state.profile.hostname
        if not host:
            # Node ids are hostnames in every deployment we ship. Better to try
            # than to refuse to measure over a naming detail.
            log.debug("no address known for %s; dialling the node id directly", node_id)
            host = node_id
        return Endpoint(node_id=node_id, host=host, is_local=self._is_local(node_id, host))

    def _is_local(self, node_id: str, host: str) -> bool:
        """Is this node the one we are running on?

        It decides where the client half of a two-sided probe runs, so getting
        it wrong measures the wrong link. An explicit node id wins; otherwise we
        fall back to matching the host against our own names and addresses,
        because the common single-container deployment sets nothing.
        """
        if self._local_node_id is not None:
            return node_id == self._local_node_id
        return node_id in _local_names() or host in _local_names()


@functools.lru_cache(maxsize=1)
def _local_names() -> frozenset[str]:
    """Every name and address that means "this machine"."""
    names = {"localhost", "127.0.0.1", "::1"}
    try:
        hostname = socket.gethostname()
        names.add(hostname)
        names.add(hostname.split(".", 1)[0])
        for info in socket.getaddrinfo(hostname, None):
            names.add(info[4][0])
    except OSError:  # pragma: no cover - a box that cannot resolve its own name
        log.debug("could not determine local hostname; relying on DERATE_NODE_ID")
    return frozenset(names)


def _default_store_path() -> str:
    return str(data_dir() / DEFAULT_STORE_NAME)


def _env_key(node_id: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in node_id).upper()
