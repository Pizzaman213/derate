"""The 1 Hz metrics stream.

One producer, many subscribers. If a source is unavailable the event still
goes out with that field null: the UI should degrade a panel, not freeze.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .. import measurements, metrics_scrape
from ..contracts import DeploymentState
from . import serialize
from .settings import GatewaySettings
from .stats import StatsRegistry

log = logging.getLogger("gateway.metrics")


#: Decode tokens a scrape window must carry before its rate is written down.
#: Below this the denominator is a handful of decode steps, where graph-capture
#: warmup and scheduler jitter dominate -- a number precise to three figures and
#: wrong in the first.
MIN_DECODE_TOKENS = 200.0


class MetricsHub:
    def __init__(
        self,
        *,
        registry,
        deployments,
        stats: StatsRegistry,
        settings: GatewaySettings,
        providers=None,
    ) -> None:
        self._registry = registry
        self._deployments = deployments
        self._stats = stats
        self._settings = settings
        self._providers = providers
        self._subscribers: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None
        self._latest: dict[str, Any] | None = None
        # The prefix-cache scrape runs on its own, slower clock. See
        # _prefix_cache_loop for why it cannot share the 1 Hz one.
        self._cache_task: asyncio.Task | None = None
        self._cache_prev: dict[str, metrics_scrape.PrefixCache] = {}
        self._cache_hit_pct: float | None = None
        # Per deployment, and only for the ones actually speculating. A model
        # with no draft head never appears here at all -- see _spec_window.
        self._spec_prev: dict[str, metrics_scrape.SpecDecode] = {}
        self._spec: dict[str, dict[str, Any]] = {}
        # The engine's own load. Same body, same clock, third parse -- see
        # _sample_prefix_cache, which already fetches once and parses twice.
        self._load_prev: dict[str, metrics_scrape.EngineLoad] = {}
        self._load: dict[str, dict[str, Any]] = {}

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self._task = asyncio.create_task(self._produce_loop())
        self._cache_task = asyncio.create_task(self._prefix_cache_loop())

    async def stop(self) -> None:
        for name in ("_task", "_cache_task"):
            task = getattr(self, name)
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            setattr(self, name, None)

    async def _produce_loop(self) -> None:
        while True:
            try:
                event = self.snapshot()
                self._latest = event
                self._publish(event)
                await asyncio.sleep(self._settings.metrics_interval_s)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A broken source must not stop the stream.
                log.exception("metrics snapshot failed")
                await asyncio.sleep(self._settings.metrics_interval_s)

    # -- what the engines count about themselves ---------------------------

    async def _prefix_cache_loop(self) -> None:
        """Scrape each vLLM engine's own counters, slowly.

        On its own clock, and never inside :meth:`snapshot`. That method is
        synchronous and runs at 1 Hz, while ``metrics_scrape.fetch`` is a
        blocking ``urllib`` call with a five-second timeout -- so one wedged
        backend scraped inline would stall the whole metrics stream for every
        subscriber, which is exactly the failure the 1 Hz frame exists to
        report rather than to suffer. Everything this loop learns lands in
        ``self._cache_hit_pct`` and ``self._spec``, and ``snapshot`` reads
        those with no I/O.
        """
        while True:
            try:
                await asyncio.sleep(self._settings.prefix_cache_interval_s)
                await self._sample_prefix_cache()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Same contract as the frame above: a broken source must not
                # stop the loop, and the figure simply stays as it was until
                # the next round replaces it.
                log.exception("prefix cache scrape failed")

    async def _sample_prefix_cache(self) -> None:
        """One round: read every ready vLLM, difference it, store the rates.

        ONE fetch per engine, parsed twice. The prefix-cache counters and the
        speculation counters are in the same exposition body, so asking for it
        a second time would double the request count to learn nothing new.
        """
        targets: list[tuple[str, str]] = []
        served: dict[str, str] = {}
        for dep in self._deployments.list():
            # Only vLLM exports these series, and only a READY backend is
            # listening. sglang and tts would answer nothing useful and cost a
            # connection attempt every round for it.
            if dep.runtime != "vllm" or dep.state is not DeploymentState.READY:
                continue
            if dep.backend_url:
                targets.append((dep.deployment_id, dep.backend_url))
                served[dep.deployment_id] = dep.served_name

        # Concurrently, so one slow engine costs one timeout rather than
        # delaying every engine behind it.
        bodies = await asyncio.gather(
            *(asyncio.to_thread(metrics_scrape.fetch, url) for _, url in targets),
            return_exceptions=True,
        )

        queries = hits = 0.0
        seen: dict[str, metrics_scrape.PrefixCache] = {}
        seen_spec: dict[str, metrics_scrape.SpecDecode] = {}
        spec_frame: dict[str, dict[str, Any]] = {}
        seen_load: dict[str, metrics_scrape.EngineLoad] = {}
        load_frame: dict[str, dict[str, Any]] = {}

        for (deployment_id, _), body in zip(targets, bodies):
            if not isinstance(body, str):
                # An unreachable host, a 404, metrics turned off, or a thread
                # that raised. All four are "no reading" -- and dropping the
                # stored baseline with them matters: keeping it would let the
                # next successful read difference against a stale one and
                # report a whole outage's worth of queries as one window.
                continue

            reading = metrics_scrape.prefix_cache(body)
            seen[deployment_id] = reading
            previous = self._cache_prev.get(deployment_id)
            if previous is not None:
                # First sight of an engine says nothing: one read is a lifetime
                # total, not a window, so that round only sets the baseline.
                window = metrics_scrape.cache_delta(previous, reading)
                queries += window.queries
                hits += window.hits

            load = metrics_scrape.engine_load(body)
            seen_load[deployment_id] = load
            previous_load = self._load_prev.get(deployment_id)
            load_frame[deployment_id] = self._load_window(
                load,
                metrics_scrape.load_delta(previous_load, load)
                if previous_load is not None
                else None,
            )

            if previous_load is not None:
                self._remember_workload(
                    served.get(deployment_id),
                    metrics_scrape.load_delta(previous_load, load),
                )

            spec = metrics_scrape.spec_decode(body)
            seen_spec[deployment_id] = spec
            previous_spec = self._spec_prev.get(deployment_id)
            if previous_spec is not None:
                measured = self._spec_window(
                    metrics_scrape.delta(previous_spec, spec)
                )
                if measured is not None:
                    spec_frame[deployment_id] = measured

        # Rebuilt rather than updated, so an engine that stopped does not keep
        # its baseline for the life of the process.
        self._cache_prev = seen
        self._spec_prev = seen_spec
        self._spec = spec_frame
        self._load_prev = seen_load
        self._load = load_frame
        self._cache_hit_pct = (
            round(hits / queries * 100.0, 1) if queries > 0.0 else None
        )

    @staticmethod
    def _remember_workload(served_name, window) -> None:
        """File this window's prompt:generation split under the served name.

        What it buys is a launch decision, not a screen: a collective's cost
        depends on message size, prefill all-reduces megabytes and decode
        kilobytes, and NCCL's environment is fixed for a process's life. So the
        only moment the choice can be made is the next launch, and the only
        honest input is what this model's traffic actually looked like.

        Best effort and never raised into the metrics loop, which has a whole
        cluster's frame to finish. An unwritable record costs a tuning
        preference, not a frame.
        """
        if not served_name:
            return
        share = window.prefill_share
        if share is None:
            # Idle window. Writing 0.0 would read as "pure decode" and tune a
            # deployment for a regime it was simply not asked to do this round.
            return
        try:
            from control_plane import measurements as M

            M.save_workload(
                M.WorkloadRecord(
                    served_name=served_name,
                    prefill_share=share,
                    prompt_tokens=window.prompt_tokens,
                    generation_tokens=window.generation_tokens,
                    requests=window.decode_count,
                    measured_at=time.time(),
                )
            )
        except Exception:
            log.debug("could not record the workload shape", exc_info=True)

    @staticmethod
    def _load_window(
        latest: "metrics_scrape.EngineLoad",
        window: "metrics_scrape.EngineLoad | None",
    ) -> dict[str, Any]:
        """One deployment's load: the gauges now, the counters over the window.

        Unlike :meth:`_spec_window` this never returns None. Every ready vLLM
        has a KV cache and a queue, so "no reading" here means the engine could
        not be scraped -- which the caller already handled by skipping it.

        ``decode_tps`` IS None whenever nothing finished in the window, and that
        is the field that matters: it is the only measured number in the product
        that ``fit.predict_decode_tps`` can be checked against, and an idle
        engine reporting 0.0 would drag the comparison toward a fabricated
        disagreement.
        """
        frame: dict[str, Any] = {
            "kv_cache_usage": latest.kv_cache_usage,
            "requests_running": latest.requests_running,
            "requests_waiting": latest.requests_waiting,
        }
        if window is None:
            # First sight of this engine. One read of a counter is a lifetime
            # total, not a window, so the rates stay unmeasured this round.
            frame["preemptions"] = None
            frame["decode_tps"] = None
            frame["generated_tokens"] = None
            return frame
        frame["preemptions"] = window.preemptions
        frame["generated_tokens"] = window.generation_tokens
        tps = window.decode_tps
        frame["decode_tps"] = None if tps is None else round(tps, 1)
        return frame

    @staticmethod
    def _spec_window(window: "metrics_scrape.SpecDecode") -> dict[str, Any] | None:
        """One deployment's speculation, measured over one scrape window.

        None for the overwhelmingly common case: a deployment that is not
        speculating at all exports these series as zeros (or not at all), and
        an acceptance rate for a model that drafted nothing is not a zero, it
        is not a thing. The card must keep saying the rate is unmeasured
        rather than start claiming 0%.

        `accepted_per_pos` is CUMULATIVE acceptance per draft position and
        vLLM's own dashboard divides it by `num_drafts`; `acceptance_at` is
        that division. Reported per position rather than only as a mean
        because the shape is the useful part -- a head that lands position 0
        almost always and position 3 almost never is a head to run at a lower
        k, and a mean hides exactly that.
        """
        if window.draft_tokens <= 0.0 or window.drafts <= 0.0:
            return None
        if not window.consistent:
            # The per-position series did not sum to the aggregate. Something
            # was read mid-update or the image changed shape; either way this
            # is not a number to put on a screen.
            return None
        per_pos = [window.acceptance_at(i) for i in range(len(window.accepted_per_pos))]
        return {
            "drafts": int(window.drafts),
            "draft_tokens": int(window.draft_tokens),
            "accepted_tokens": int(window.accepted_tokens),
            # Accepted over proposed, across the window. The measured middle of
            # the floor/ceiling range the fit gate states -- and reported
            # BESIDE it, never in place of it.
            "acceptance": round(window.mean_acceptance, 4),
            "acceptance_per_pos": [
                None if v is None else round(v, 4) for v in per_pos
            ],
            # Drafted tokens settled per step, which is the figure that maps
            # onto a speedup. Named at the k the window actually ran at.
            "accepted_per_step": round(
                window.expected_accepted(len(window.accepted_per_pos)) or 0.0, 4
            ),
        }

    def _publish(self, event: dict) -> None:
        for queue in list(self._subscribers):
            if queue.full():
                # A slow subscriber loses the oldest event, never the newest.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    # -- subscription ------------------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._settings.metrics_queue_depth)
        if self._latest is not None:
            queue.put_nowait(self._latest)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # -- snapshot ----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        window = self._settings.metrics_rate_window_s
        now = time.time()

        nodes_payload: list[dict] | None
        total_power: float | None
        try:
            nodes = self._registry.list_nodes()
            nodes_payload = []
            total_power = 0.0
            for node in nodes:
                # Against physical memory, matching the architecture doc's
                # topology payload. See serialize.node_payload.
                total = node.profile.total_memory or node.memory_total or 0
                used_pct = (
                    round(node.memory_used / total * 100.0, 1) if total else None
                )
                nodes_payload.append(
                    {
                        "node_id": node.profile.node_id,
                        # Through serialize, not straight off the state: a node
                        # with no GPU reports 0.0 W, and this frame is what the
                        # UI prefers while it is fresh. Sending the raw figure
                        # here put a measured-looking 0 W beside the null that
                        # /api/nodes sends for the same machine.
                        "power_w": serialize.power_reading(node),
                        "temp_c": serialize.temp_reading(node),
                        "memory_used_pct": used_pct,
                        "util_pct": node.utilization_pct,
                        # When these four were measured. The UI greys a node
                        # whose sample has aged out rather than passing a
                        # frozen reading off as current.
                        "sample_ts": node.sample_ts or None,
                    }
                )
                total_power += serialize.power_reading(node) or 0.0
        except Exception:
            log.exception("registry unavailable for metrics")
            nodes_payload = None
            total_power = None

        deployments_payload: list[dict] | None
        try:
            deployments_payload = []
            for dep in self._deployments.list():
                st = self._stats.peek(dep.deployment_id)
                deployments_payload.append(
                    {
                        "deployment_id": dep.deployment_id,
                        "state": dep.state.value,
                        "tokens_per_sec": round(st.tokens_per_sec(window, now), 1)
                        if st
                        else 0.0,
                        "ttft_ms": round(st.ttft_ms, 1) if st and st.ttft_ms else None,
                        "queue_depth": st.outstanding if st else 0,
                        # What the ENGINE counted about its own speculation
                        # over the last scrape window, or null -- which is the
                        # ordinary case, because most deployments run no draft
                        # head and a model that drafted nothing has no
                        # acceptance rate. See _spec_window.
                        "speculative": self._spec.get(dep.deployment_id),
                        # The engine's own account of its memory and its rate.
                        # `decode_tps` here is MEASURED; the fit gate's
                        # `predicted_decode_tps` on the deployment record is
                        # arithmetic. Carrying both is the point -- until now
                        # nothing in the product could compare them.
                        "load": self._load.get(dep.deployment_id),
                    }
                )
        except Exception:
            log.exception("deployment list unavailable for metrics")
            deployments_payload = None

        # Models a provider serves, counted exactly as a deployment is one
        # block up: same registry, same window, same rounding. The cluster
        # screen draws a remote-served name with the same band as a local one
        # and reads its throughput from here, so the two figures have to come
        # from one place or the band above and the band below tick at
        # different rates for no reason a viewer could work out.
        #
        # Only targets the registry has actually seen. Every model of an
        # un-allowlisted OpenRouter key is several hundred rows, and this
        # payload goes out once a second -- a target nobody has routed to has
        # no counter to report and the UI reads its absence as the zero it is.
        remotes_payload: list[dict] | None
        try:
            if self._providers is None:
                remotes_payload = None
            else:
                remotes_payload = []
                servable = getattr(self._providers, "servable", self._providers.list)
                for provider in servable():
                    for model in provider.models:
                        # The same id gateway/targets.py builds a remote
                        # RouteTarget from, which is what the stats are keyed
                        # by and what the UI's band carries.
                        target_id = f"{provider.provider_id}:{model.upstream_id}"
                        st = self._stats.peek(target_id)
                        if st is None:
                            continue
                        remotes_payload.append(
                            {
                                "target_id": target_id,
                                "provider_id": provider.provider_id,
                                "served_name": model.served_name,
                                "state": "healthy" if provider.healthy else "unhealthy",
                                "tokens_per_sec": round(st.tokens_per_sec(window, now), 1),
                                "ttft_ms": round(st.ttft_ms, 1) if st.ttft_ms else None,
                                "queue_depth": st.outstanding,
                                # Always null, and it earns its place: the same
                                # counters under the same names is what lets one
                                # reader draw a remote band and a local one. We
                                # have no engine to scrape on somebody else's
                                # API, so "no reading" is the honest answer --
                                # not an omission for a reader to trip over.
                                "speculative": None,
                                # Same rule, same reason: somebody else's API
                                # has no KV cache we can see and no preemptions
                                # we could count. Null, never absent.
                                "load": None,
                            }
                        )
        except Exception:
            log.exception("provider list unavailable for metrics")
            remotes_payload = None

        return {
            "ts": now,
            "cluster": {
                "tokens_per_sec": round(self._stats.total_tokens_per_sec(window, now), 1),
                "total_power_w": round(total_power, 1) if total_power is not None else None,
                # vLLM's own `vllm:prefix_cache_{queries,hits}_total`, read by
                # _prefix_cache_loop and differenced into a rate over its
                # window -- never the engine's lifetime ratio, which answers a
                # question nobody on this screen asked.
                #
                # Still null, and honestly so, whenever nothing was measured:
                # no ready vLLM, a backend with metrics off, an unreachable
                # host, the first round after start-up (one read is a total,
                # not a window), or an engine that served no tokens at all.
                # A cluster that asked nothing has no hit rate; only one that
                # asked and missed has a real 0.0.
                "cache_hit_pct": self._cache_hit_pct,
            },
            "nodes": nodes_payload,
            "deployments": deployments_payload,
            "remotes": remotes_payload,
        }
