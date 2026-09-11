"""Background scan that recovers a derate-launched container derate lost
track of.

``manager.py``'s own ``reconcile()`` already rehydrates state for every
record still on disk after a restart -- but a record that was never written
in the first place (a store file lost before it was, a launch that raced a
restart, anything that reached ``sparkrun run``/``docker`` directly) has
nothing for reconcile to find. sparkrun itself has no "list every cluster"
command, so the only trace left is the container.

This closes that gap by asking each node agent what it can already see:
``/agent/processes`` (a resident GPU process and its full command line --
``--pid=host`` is what makes the command line visible from the host at all)
and ``/agent/containers`` (which container, if any, owns that process, and
what sparkrun named it). A process already attributable to a known
deployment (:func:`control_plane.procmatch.matches_deployment`) is not a
candidate; one whose container name does not look like sparkrun's own is not
either. What is left is identified before anything is trusted: the parsed
command line's claimed model must actually answer for itself on
``/v1/models`` (``deploy/health.py::probe`` -- the same identity check every
other deployment's health watch already relies on) before
:meth:`DeploymentManager.adopt` ever runs. A container that fails that check
is left exactly as it was found: visible as an unmanaged process
(``ResidentProcesses.tsx``), never silently claimed.
"""

from __future__ import annotations

import asyncio
import logging

from control_plane.contracts import FitRequest, Modality, ParallelismKind, ParallelismPlan
from control_plane.procmatch import matches_deployment

from .adopt import cluster_id_from_container_name, parse_serve_command
from .health import probe

log = logging.getLogger(__name__)

#: How often every roster node is asked what is running on it. Short enough
#: that "auto" feels immediate; each round is at most one /agent/processes,
#: one /agent/containers and, only for a genuinely new container, one
#: /v1/models call per node -- cheap relative to the 5s deployment watch tick.
CONTAINER_AUTOADOPT_INTERVAL_S = 30.0

_AGENT_TIMEOUT_S = 5.0
_IDENTITY_TIMEOUT_S = 3.0
#: Default when a runtime's own KV dtype cannot be read off its command line
#: (none of the three serve templates expose one). Same default
#: GatewaySettings.default_kv_dtype documents for the same reason: the fit
#: gate's own kv_bytes_per_token supersedes this when it is available.
_DEFAULT_KV_DTYPE = "fp16"


class ContainerAutoAdopter:
    """Owns one background task. Construct once, ``start()``/``stop()`` around it."""

    def __init__(
        self,
        *,
        registry,
        resolver,
        fit,
        deployments,
        interval_s: float = CONTAINER_AUTOADOPT_INTERVAL_S,
        enabled: bool = True,
    ) -> None:
        self._registry = registry
        self._resolver = resolver
        self._fit = fit
        self._deployments = deployments
        self._interval_s = interval_s
        self._enabled = enabled
        self._running = False
        self._task: asyncio.Task | None = None

    async def scan_once(self) -> list[str]:
        """One round over the roster. Returns the deployment_ids newly adopted.

        Never raises: a failure reading one node, or adopting one container,
        must not stop the rest of the round.
        """
        if not self._enabled or not callable(getattr(self._deployments, "adopt", None)):
            return []
        try:
            nodes = self._registry.list_nodes()
        except Exception:
            log.debug("container autoadopt: could not list nodes", exc_info=True)
            return []
        handles_by_node = self._handles_by_node()

        import httpx

        adopted: list[str] = []
        async with httpx.AsyncClient(timeout=_AGENT_TIMEOUT_S) as client:
            for state in nodes:
                profile = getattr(state, "profile", None)
                node_id = getattr(profile, "node_id", None)
                address = getattr(profile, "address", None)
                agent_url = self._agent_url(node_id)
                if not node_id or not address or not agent_url:
                    continue
                candidates = await self._candidates(client, agent_url)
                # Scoped to this node: matches_deployment() matches on a bare
                # port number, and two different nodes independently
                # allocating the same port (ordinary -- each starts counting
                # from the same base) would otherwise read as "already
                # tracked" for a container on a node that record has nothing
                # to do with.
                node_handles = handles_by_node.get(node_id, [])
                for container_name, ancestor_commands in candidates:
                    deployment_id = await self._adopt_one(
                        node_id, address, ancestor_commands, container_name, node_handles
                    )
                    if deployment_id:
                        adopted.append(deployment_id)
        return adopted

    def _handles_by_node(self) -> dict[str, list[dict]]:
        """Every known deployment's sparkrun handle, grouped by the node(s)
        its plan actually places it on. Handles alone (cluster_id/port) carry
        no node -- only the deployment's plan does."""
        try:
            handles = self._deployments.handles()
            deployments = self._deployments.list()
        except Exception:
            return {}
        by_node: dict[str, list[dict]] = {}
        for deployment in deployments:
            handle = handles.get(deployment.deployment_id)
            plan = getattr(deployment, "plan", None)
            if handle is None or plan is None:
                continue
            for node_id in plan.node_ids or []:
                by_node.setdefault(node_id, []).append(handle)
        return by_node

    def _agent_url(self, node_id: str | None) -> str | None:
        lookup = getattr(self._registry, "agent_url", None)
        if not node_id or not callable(lookup):
            return None
        try:
            return lookup(node_id)
        except Exception:
            return None

    async def _candidates(self, client, agent_url: str) -> list[tuple[int, str, str]]:
        """(container_name, ancestor_commands) for every GPU process on this
        node that belongs to a docker container. Empty on any failure to
        ask -- never raises, matching every other best-effort node read
        here. The GPU-holding PID's own command is deliberately not part of
        this: see registry/containers.py -- for a vLLM deployment it is a
        renamed ``VLLM::EngineCore``, never the flags routing needs."""
        base = agent_url.rstrip("/")
        try:
            containers_res = await client.get(f"{base}/agent/containers")
            containers_res.raise_for_status()
        except Exception:
            log.debug("container autoadopt: agent unreachable at %s", agent_url, exc_info=True)
            return []
        containers = (containers_res.json() or {}).get("containers") or []
        return [
            (c.get("container_name") or "", c.get("ancestor_commands") or [])
            for c in containers
        ]

    async def _adopt_one(
        self,
        node_id: str,
        address: str,
        ancestor_commands: list[str],
        container_name: str,
        node_handles: list[dict],
    ) -> str | None:
        cluster_id = cluster_id_from_container_name(container_name)
        if cluster_id is None:
            return None  # not sparkrun's own naming; nothing here to adopt
        if any(
            matches_deployment(command, h.get("cluster_id"), h.get("port"))
            for command in ancestor_commands
            for h in node_handles
        ):
            return None  # already tracked under some deployment_id on this node
        spec = None
        for command in ancestor_commands:
            spec = parse_serve_command(command)
            if spec is not None:
                break
        if spec is None:
            return None  # not a recognized vllm/sglang/tts invocation

        backend_url = "http://%s:%d/v1" % (address, spec.port)
        try:
            healthy, reason = await asyncio.to_thread(
                probe, backend_url, timeout=_IDENTITY_TIMEOUT_S, expect_model=spec.model_id
            )
        except Exception:
            log.debug("container autoadopt: identity probe failed for %s", backend_url, exc_info=True)
            return None
        if not healthy:
            # Never adopt on a miss: a mismatch (health.wrong_model(reason))
            # means someone else is on that port, and "nothing answered yet"
            # is the ordinary state of a container this build cannot
            # recognize a command from. Either way, leave it as an unmanaged
            # process for a human to look at.
            log.debug("container autoadopt: %s did not confirm %s (%s)", backend_url, spec.model_id, reason)
            return None

        try:
            shape = await asyncio.to_thread(self._resolver.resolve, spec.model_id)
        except Exception:
            log.warning("container autoadopt: could not resolve %s", spec.model_id, exc_info=True)
            return None

        plan = _reconstruct_plan(node_id, spec)
        req = FitRequest(
            shape=shape,
            context_length=spec.context_length,
            max_concurrent_seqs=spec.max_concurrent_seqs,
            kv_dtype=_DEFAULT_KV_DTYPE,
            plan=plan,
        )
        try:
            nodes = self._registry.list_nodes()
        except Exception:
            nodes = []
        node_profiles = [
            n.profile for n in nodes if getattr(getattr(n, "profile", None), "node_id", None) == node_id
        ]
        if not node_profiles:
            log.debug("container autoadopt: %s left the roster mid-scan", node_id)
            return None
        try:
            fit = await asyncio.to_thread(self._fit.check, req, node_profiles, allocatable=None)
        except Exception:
            log.warning("container autoadopt: fit check failed for %s", spec.model_id, exc_info=True)
            return None

        modality = _reconstruct_modality(spec, shape)
        try:
            deployment = self._deployments.adopt(
                shape=shape,
                plan=plan,
                fit=fit,
                runtime=spec.runtime,
                served_name=spec.served_name,
                backend_url=backend_url,
                context_length=spec.context_length,
                max_concurrent_seqs=spec.max_concurrent_seqs,
                cluster_id=cluster_id,
                node_address=address,
                port=spec.port,
                modality=modality,
            )
        except Exception:
            log.warning("container autoadopt: could not adopt %s on %s", spec.served_name, node_id, exc_info=True)
            return None
        return deployment.deployment_id if deployment is not None else None

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # a bad round must not kill the loop
                log.exception("container auto-adopt round failed")
            await asyncio.sleep(self._interval_s)

    async def start(self) -> None:
        """Idempotent. A no-op when disabled, so callers need not check first."""
        if not self._enabled or self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="deploy-container-autoadopt")

    async def stop(self) -> None:
        self._running = False
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


def _reconstruct_plan(node_id: str, spec) -> ParallelismPlan:
    if spec.tensor_parallel > 1:
        kind = ParallelismKind.TENSOR
    elif spec.pipeline_parallel > 1:
        kind = ParallelismKind.PIPELINE
    elif spec.expert_parallel:
        kind = ParallelismKind.EXPERT
    else:
        kind = ParallelismKind.SINGLE_NODE
    flags = "--tensor-parallel-size %d, --pipeline-parallel-size %d" % (
        spec.tensor_parallel,
        spec.pipeline_parallel,
    )
    if spec.expert_parallel:
        flags += ", --enable-expert-parallel"
    return ParallelismPlan(
        kind=kind,
        tensor_parallel=spec.tensor_parallel,
        pipeline_parallel=spec.pipeline_parallel,
        # vLLM couples expert-parallel degree to tensor-parallel degree when
        # the flag is on; the command line carries no separate number for it.
        expert_parallel=spec.tensor_parallel if spec.expert_parallel else 1,
        data_parallel=1,
        node_ids=[node_id],
        reason=(
            "reconstructed from a container already running on %s, not "
            "decided by the planner -- this coordinator never launched it. "
            "Observed %s." % (node_id, flags)
        ),
        measured_link_gbps=0.0,
        rejected=[],
    )


def _reconstruct_modality(spec, shape) -> Modality:
    """Best-effort. The command line does not distinguish a chat model from
    an embedding one -- both are an ordinary `vllm serve` -- so that
    distinction is not attempted here and an adopted embedding model is
    reported as TEXT until something else identifies it."""
    if spec.runtime == "tts":
        return Modality.SPEECH
    if getattr(shape, "is_encoder_decoder", False):
        return Modality.TRANSCRIPTION
    return Modality.TEXT
