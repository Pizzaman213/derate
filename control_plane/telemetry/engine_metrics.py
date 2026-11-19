"""Speculative-decoding counters, scraped from each local engine's /metrics.

vLLM keeps spec-decode statistics as Prometheus counters per engine, never
per request. A request's share is therefore a *delta* between two scrapes
that bracket it, and that delta belongs to the request only when nothing else
ran on the engine meanwhile -- otherwise it is ``ambiguous_concurrency``, and
saying so beats inventing a split.

Metric names were read off the pinned image, not guessed
(``ghcr.io/pizzaman213/derate/vllm-audio:latest``, vllm/v1/spec_decode/metrics.py)::

    vllm:spec_decode_num_drafts               Counter, exposed with _total
    vllm:spec_decode_num_draft_tokens         Counter, exposed with _total
    vllm:spec_decode_num_accepted_tokens      Counter, exposed with _total
    vllm:spec_decode_num_accepted_tokens_per_pos  Counter, label position="<i>"

Every series carries ``engine``/``model_name`` labels; values are summed
across them. The counters only exist when the engine was launched with a
speculative config, so a scrape without them is ``not_enabled``. Only the
vLLM runtime is understood: no sglang image is on this box to read names off,
so any other runtime is ``not_reported``.

The poller never raises and is never awaited on the request path.
"""

from __future__ import annotations

import asyncio
import bisect
import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Protocol

from control_plane.contracts.telemetry import FieldError

log = logging.getLogger(__name__)

_DRAFTS = "vllm:spec_decode_num_drafts"
_DRAFT_TOKENS = "vllm:spec_decode_num_draft_tokens"
_ACCEPTED = "vllm:spec_decode_num_accepted_tokens"
_PER_POS = "vllm:spec_decode_num_accepted_tokens_per_pos"

#: Runtimes whose metric names are known (see module docstring).
SUPPORTED_RUNTIMES = frozenset({"vllm"})

#: How long scrapes are kept per deployment. Attribution needs a sample at or
#: before a request's start, so this bounds the longest attributable request.
HISTORY_S = 1800.0

SCRAPE_TIMEOUT_S = 2.0

#: Draft positions kept from ``..._per_pos``. The label is the engine's to
#: write, so a compromised worker chose its size; real drafts are a handful.
MAX_SPEC_POSITIONS = 256

#: A ``/metrics`` body larger than this is discarded unparsed (CAP-4).
MAX_METRICS_BYTES = 4 << 20

_LINE = re.compile(r"^([A-Za-z_:][A-Za-z0-9_:]*)(\{[^}]*\})?\s+(\S+)")
_POSITION = re.compile(r'position="(\d+)"')


class EngineTarget(Protocol):
    """What the poller needs to know about a deployment.

    The gateway's ``deployments_fn`` returns these for every READY local text
    deployment and nothing else: ``deployment_id``, the backend's base URL
    (``http://host:port``, ``/metrics`` is appended), and the runtime name
    (``"vllm"``, ``"sglang"``, ...).
    """

    @property
    def deployment_id(self) -> str: ...

    @property
    def backend_url(self) -> str: ...

    @property
    def runtime(self) -> str: ...


@dataclass(frozen=True)
class SpecSample:
    """Counter values at one scrape, or the delta between two."""

    num_drafts: int
    draft_tokens: int
    accepted_tokens: int
    accepted_per_position: list[int] | None = None

    def minus(self, earlier: "SpecSample") -> "SpecSample":
        per_pos = None
        if self.accepted_per_position is not None:
            before = earlier.accepted_per_position or []
            per_pos = [
                v - (before[i] if i < len(before) else 0)
                for i, v in enumerate(self.accepted_per_position)
            ]
        return SpecSample(
            num_drafts=self.num_drafts - earlier.num_drafts,
            draft_tokens=self.draft_tokens - earlier.draft_tokens,
            accepted_tokens=self.accepted_tokens - earlier.accepted_tokens,
            accepted_per_position=per_pos,
        )


def parse_spec(text: str) -> SpecSample | None:
    """Spec-decode counters out of a Prometheus exposition. None when absent."""
    totals = {_DRAFTS: 0.0, _DRAFT_TOKENS: 0.0, _ACCEPTED: 0.0}
    seen = False
    per_pos: dict[int, float] = {}
    for line in text.splitlines():
        if not line or line[0] == "#":
            continue
        m = _LINE.match(line)
        if m is None:
            continue
        name, labels, raw = m.group(1), m.group(2) or "", m.group(3)
        base = name[:-6] if name.endswith("_total") else name
        if base not in totals and base != _PER_POS:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        seen = True
        if base == _PER_POS:
            pm = _POSITION.search(labels)
            if pm is not None:
                pos = int(pm.group(1))
                # A worker writes the label; an unbounded one used to size a
                # list on the event loop (CAP-4). Real drafts are a handful.
                if pos >= MAX_SPEC_POSITIONS:
                    continue
                per_pos[pos] = per_pos.get(pos, 0.0) + value
        else:
            totals[base] += value
    if not seen:
        return None
    positions = (
        [int(per_pos.get(i, 0.0)) for i in range(max(per_pos) + 1)] if per_pos else None
    )
    return SpecSample(
        num_drafts=int(totals[_DRAFTS]),
        draft_tokens=int(totals[_DRAFT_TOKENS]),
        accepted_tokens=int(totals[_ACCEPTED]),
        accepted_per_position=positions,
    )


class _Track:
    __slots__ = ("runtime", "times", "samples", "enabled", "warned")

    def __init__(self, runtime: str) -> None:
        self.runtime = runtime
        self.times: deque[float] = deque()
        self.samples: deque[SpecSample] = deque()
        #: None until a scrape succeeded; then whether it carried spec counters.
        self.enabled: bool | None = None
        #: Whether a discarded scrape has been logged for this deployment.
        self.warned = False


class EngineMetricsPoller:
    """Scrapes ``/metrics`` of every engine ``deployments_fn`` names.

    ``http`` is an ``httpx.AsyncClient`` (or anything with an awaitable
    ``get(url, timeout=)`` returning ``.status_code``/``.text``).
    """

    def __init__(
        self,
        http: Any,
        deployments_fn: Callable[[], Iterable[EngineTarget]],
        interval_s: float = 1.0,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._http = http
        self._deployments_fn = deployments_fn
        self.interval_s = interval_s
        self._clock = clock
        self._tracks: dict[str, _Track] = {}
        self._task: asyncio.Task | None = None

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="engine-metrics")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self.interval_s)

    # -- scraping --------------------------------------------------------

    async def poll_once(self) -> None:
        """One scrape of every target. Never raises."""
        try:
            targets = list(self._deployments_fn() or ())
        except Exception as exc:
            log.debug("engine metrics: deployments_fn failed: %s", exc)
            return
        live = set()
        for target in targets:
            try:
                dep_id = str(target.deployment_id)
                runtime = str(getattr(target.runtime, "value", target.runtime) or "")
            except Exception:
                continue
            live.add(dep_id)
            track = self._tracks.get(dep_id)
            if track is None or track.runtime != runtime:
                track = self._tracks[dep_id] = _Track(runtime)
            if runtime not in SUPPORTED_RUNTIMES:
                continue
            await self._scrape(target, track)
        for gone in set(self._tracks) - live:
            del self._tracks[gone]

    async def _scrape(self, target: EngineTarget, track: _Track) -> None:
        url = str(target.backend_url).rstrip("/") + "/metrics"
        try:
            resp = await self._http.get(url, timeout=SCRAPE_TIMEOUT_S)
            if resp.status_code != 200:
                return
            size = len(resp.content)
            if size > MAX_METRICS_BYTES:
                if not track.warned:
                    track.warned = True
                    log.warning(
                        "engine metrics: %s: body %d bytes exceeds %d; ignored",
                        url, size, MAX_METRICS_BYTES,
                    )
                return
            sample = parse_spec(resp.text)
        except Exception as exc:
            log.debug("engine metrics: %s: %s", url, exc)
            return
        now = self._clock()
        if sample is None:
            track.enabled = False
            return
        track.enabled = True
        track.times.append(now)
        track.samples.append(sample)
        while track.times and track.times[0] < now - HISTORY_S:
            track.times.popleft()
            track.samples.popleft()

    # -- attribution -----------------------------------------------------

    def has_sample_after(self, deployment_id: str, ts: float) -> bool:
        track = self._tracks.get(deployment_id)
        return bool(track and track.times and track.times[-1] >= ts)

    def attribute(
        self, deployment_id: str, start_ts: float, end_ts: float, alone: bool
    ) -> SpecSample | FieldError:
        """The counter delta across one request, or why there is none.

        ``not_enabled`` when the engine's scrapes carry no spec counters;
        ``ambiguous_concurrency`` when the request shared the engine;
        ``not_reported`` when the runtime is unknown, or there is no sample at
        or before ``start_ts`` and one at or after ``end_ts``, or the counters
        went backwards (an engine restart).
        """
        try:
            track = self._tracks.get(deployment_id)
            if track is None or track.runtime not in SUPPORTED_RUNTIMES:
                return FieldError.NOT_REPORTED
            if track.enabled is False:
                return FieldError.NOT_ENABLED
            if not alone:
                return FieldError.AMBIGUOUS_CONCURRENCY
            times = list(track.times)
            i = bisect.bisect_right(times, start_ts) - 1
            j = bisect.bisect_left(times, end_ts)
            if i < 0 or j >= len(times):
                return FieldError.NOT_REPORTED
            delta = track.samples[j].minus(track.samples[i])
            if min(delta.num_drafts, delta.draft_tokens, delta.accepted_tokens) < 0 or any(
                v < 0 for v in delta.accepted_per_position or ()
            ):
                return FieldError.NOT_REPORTED
            return delta
        except Exception as exc:  # pragma: no cover - the contract is never raise
            log.debug("engine metrics: attribute failed: %s", exc)
            return FieldError.NOT_REPORTED

    async def attribute_when_ready(
        self, deployment_id: str, start_ts: float, end_ts: float, alone: bool
    ) -> SpecSample | FieldError:
        """attribute(), after waiting up to two intervals for the closing scrape.

        For a background task scheduled after settle(): the scrape that
        brackets the end of a request lands up to one interval later.
        """
        try:
            deadline = self._clock() + 2 * self.interval_s
            while not self.has_sample_after(deployment_id, end_ts) and self._clock() < deadline:
                track = self._tracks.get(deployment_id)
                if track is None or track.enabled is False or not alone:
                    break
                await asyncio.sleep(self.interval_s / 4)
        except Exception:
            pass
        return self.attribute(deployment_id, start_ts, end_ts, alone)
