"""Admission control: the request-level half of out-of-memory prevention.

Agent F watches nodes. The gateway watches requests. Before proxying anything
we estimate what it will cost in KV cache, refuse it if the deployment cannot
afford it, and hold the commitment until the response completes.

A cluster that accepts everything and OOMs an hour in has not solved the
problem this project exists to solve.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from control_plane.contracts import Deployment, DeploymentState, ModelShape

from .settings import GatewaySettings

log = logging.getLogger("gateway.admission")

# Bytes per element for KV cache dtypes. Only used by the fallback estimator
# below; Agent D's kv_bytes_per_token is preferred whenever it is available.
_KV_ELEMENT_BYTES = {
    "fp32": 4.0,
    "float32": 4.0,
    "bf16": 2.0,
    "bfloat16": 2.0,
    "fp16": 2.0,
    "float16": 2.0,
    "fp8": 1.0,
    "float8": 1.0,
    "fp8_e5m2": 1.0,
    "fp8_e4m3": 1.0,
    "int8": 1.0,
}

BLOCK_MEMORY_CRITICAL = "memory_critical"
BLOCK_DRAINING = "draining"
BLOCK_RATE_LIMITED = "rate_limited"


@dataclass
class AdmissionDecision:
    ok: bool
    status: int = 200
    code: str = ""
    message: str = ""
    retry_after_s: int | None = None
    kv_bytes: int = 0
    prompt_tokens: int = 0
    requested_tokens: int = 0


def kv_bytes_per_token_fallback(shape: ModelShape, kv_dtype: str) -> float:
    """Conservative per-token KV cost, used when Agent D exposes no estimator.

    Deliberately ignores sliding-window savings. Over-charging refuses a
    request that would have fit; under-charging OOMs the node.
    """
    elem = _KV_ELEMENT_BYTES.get(kv_dtype.lower(), 2.0)
    if shape.mla_latent_dim:
        # MLA stores one compressed latent per layer, not separate K and V.
        return shape.num_layers * shape.mla_latent_dim * elem
    return 2.0 * shape.num_layers * shape.num_kv_heads * shape.effective_head_dim * elem


class AdmissionController:
    def __init__(
        self,
        *,
        registry,
        fit,
        deployments,
        settings: GatewaySettings,
    ) -> None:
        self._registry = registry
        self._fit = fit
        self._deployments = deployments
        self._settings = settings
        # target_id -> set of reasons it is not admitting
        self._blocks: dict[str, set[str]] = {}
        # deployment_id -> outstanding KV commitment in bytes
        self._committed: dict[str, int] = {}
        self._task: asyncio.Task | None = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self.reconcile()
        self._task = asyncio.create_task(self._reconcile_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _reconcile_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._settings.admission_reconcile_interval_s)
                self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("admission reconcile failed")

    # -- blocking ----------------------------------------------------------

    def is_blocked(self, target_id: str) -> bool:
        return bool(self._blocks.get(target_id))

    def blocks(self, target_id: str) -> set[str]:
        return set(self._blocks.get(target_id, ()))

    def block(self, target_id: str, reason: str) -> None:
        if reason not in self._blocks.setdefault(target_id, set()):
            log.warning("no longer admitting to %s: %s", target_id, reason)
        self._blocks[target_id].add(reason)

    def unblock(self, target_id: str, reason: str) -> None:
        reasons = self._blocks.get(target_id)
        if not reasons:
            return
        reasons.discard(reason)
        if not reasons:
            self._blocks.pop(target_id, None)
            log.info("admitting to %s again", target_id)

    def set_memory_critical(self, deployment_id: str, critical: bool) -> None:
        """Called by Agent F when a deployment's memory goes critical.

        Stops new admissions. Never kills in-flight work: outstanding requests
        run to completion and release their commitments normally.
        """
        if critical:
            self.block(deployment_id, BLOCK_MEMORY_CRITICAL)
        else:
            self.unblock(deployment_id, BLOCK_MEMORY_CRITICAL)

    def set_draining(self, deployment_id: str, draining: bool) -> None:
        if draining:
            self.block(deployment_id, BLOCK_DRAINING)
        else:
            self.unblock(deployment_id, BLOCK_DRAINING)

    def reconcile(self) -> None:
        """Derive memory pressure from the registry's live node telemetry.

        Runs twice a second, which is what makes "stops admitting within one
        second of a critical memory event" true even if nobody calls
        set_memory_critical explicitly.
        """
        try:
            nodes = {n.profile.node_id: n for n in self._registry.list_nodes()}
        except Exception:
            log.exception("registry unavailable during admission reconcile")
            return
        try:
            deployments = self._deployments.list()
        except Exception:
            log.exception("deployment list unavailable during admission reconcile")
            return

        critical_nodes = set()
        for node_id, state in nodes.items():
            limit = state.profile.addressable_memory
            if limit > 0 and state.memory_used / limit >= self._settings.critical_memory_pct:
                critical_nodes.add(node_id)

        for dep in deployments:
            node_ids = list(dep.plan.node_ids) if dep.plan else []
            pressured = any(n in critical_nodes for n in node_ids)
            unhealthy = any(
                node_id in nodes and not nodes[node_id].healthy for node_id in node_ids
            )
            if pressured:
                self.block(dep.deployment_id, BLOCK_MEMORY_CRITICAL)
            else:
                self.unblock(dep.deployment_id, BLOCK_MEMORY_CRITICAL)

            if dep.state in (DeploymentState.STOPPING, DeploymentState.STOPPED):
                self.block(dep.deployment_id, BLOCK_DRAINING)
            elif unhealthy and dep.state is DeploymentState.FAILED:
                self.block(dep.deployment_id, BLOCK_DRAINING)
            else:
                self.unblock(dep.deployment_id, BLOCK_DRAINING)

    # -- cost estimation ---------------------------------------------------

    def kv_bytes_per_token(self, shape: ModelShape, kv_dtype: str) -> float:
        estimator = getattr(self._fit, "kv_bytes_per_token", None)
        if callable(estimator):
            try:
                return float(estimator(shape, kv_dtype))
            except Exception:
                log.exception("fit.kv_bytes_per_token failed, using fallback")
        return kv_bytes_per_token_fallback(shape, kv_dtype)

    def estimate_prompt_tokens(self, body: dict) -> int:
        """Character-based estimate. The gateway has no tokenizer and loading
        one per model would cost more than the estimate is worth.
        """
        cpt = max(1.0, self._settings.chars_per_token)
        chars = 0
        overhead = 0

        messages = body.get("messages")
        if isinstance(messages, list):
            for message in messages:
                overhead += self._settings.tokens_per_message_overhead
                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, str):
                    chars += len(content)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            chars += len(part["text"])
                elif content is not None:
                    chars += len(str(content))

        prompt = body.get("prompt")
        if isinstance(prompt, str):
            chars += len(prompt)
        elif isinstance(prompt, list):
            for item in prompt:
                chars += len(item) if isinstance(item, str) else 0

        inputs = body.get("input")
        if isinstance(inputs, str):
            chars += len(inputs)
        elif isinstance(inputs, list):
            for item in inputs:
                chars += len(item) if isinstance(item, str) else 0

        return int(chars / cpt) + overhead

    @staticmethod
    def explicit_max_tokens(body: dict) -> int | None:
        for key in ("max_tokens", "max_completion_tokens"):
            value = body.get(key)
            if isinstance(value, int) and value > 0:
                return value
        return None

    def kv_budget(self, deployment: Deployment) -> int:
        breakdown = getattr(deployment.fit, "breakdown", None)
        kv = getattr(breakdown, "kv_cache", 0) if breakdown else 0
        return int(kv * self._settings.kv_budget_fraction)

    @staticmethod
    def kv_divisor(deployment: Deployment) -> int:
        plan = deployment.plan
        if plan is None:
            return 1
        # KV is sharded across tensor-parallel ranks and split by layer across
        # pipeline stages, so the per-node share is divided by both.
        return max(1, plan.tensor_parallel * plan.pipeline_parallel)

    def committed(self, deployment_id: str) -> int:
        return self._committed.get(deployment_id, 0)

    # -- the gate ----------------------------------------------------------

    def check(self, deployment: Deployment, body: dict) -> AdmissionDecision:
        prompt_tokens = self.estimate_prompt_tokens(body)
        explicit_max = self.explicit_max_tokens(body)
        context_limit = deployment.context_length

        # 400: a single request that cannot fit the context, whatever the load.
        # Only explicit numbers are used here so a client is never refused over
        # a generation length it did not ask for.
        hard_tokens = prompt_tokens + (explicit_max or 0)
        if context_limit and hard_tokens > context_limit:
            return AdmissionDecision(
                ok=False,
                status=400,
                code="context_length_exceeded",
                message=(
                    f"Request needs about {hard_tokens} tokens "
                    f"({prompt_tokens} prompt + {explicit_max or 0} completion) "
                    f"but deployment '{deployment.served_name}' is configured for "
                    f"a context length of {context_limit}."
                ),
                prompt_tokens=prompt_tokens,
                requested_tokens=hard_tokens,
            )

        requested = prompt_tokens + (explicit_max or self._settings.assumed_max_tokens)
        per_token = self.kv_bytes_per_token(
            deployment.shape, self._settings.default_kv_dtype
        )
        kv_cost = int(per_token * requested / self.kv_divisor(deployment))

        budget = self.kv_budget(deployment)
        used = self.committed(deployment.deployment_id)
        if budget > 0 and used + kv_cost > budget:
            return AdmissionDecision(
                ok=False,
                status=429,
                code="kv_cache_exhausted",
                message=(
                    f"Deployment '{deployment.served_name}' has "
                    f"{max(0, budget - used) / 1024**2:.0f} MiB of KV cache free "
                    f"but this request needs about {kv_cost / 1024**2:.0f} MiB. "
                    "Retry when in-flight requests complete."
                ),
                retry_after_s=self._settings.retry_after_default_s,
                kv_bytes=kv_cost,
                prompt_tokens=prompt_tokens,
                requested_tokens=requested,
            )

        return AdmissionDecision(
            ok=True,
            kv_bytes=kv_cost,
            prompt_tokens=prompt_tokens,
            requested_tokens=requested,
        )

    def commit(self, deployment_id: str, kv_bytes: int) -> None:
        self._committed[deployment_id] = self.committed(deployment_id) + kv_bytes

    def release(self, deployment_id: str, kv_bytes: int) -> None:
        remaining = self.committed(deployment_id) - kv_bytes
        if remaining > 0:
            self._committed[deployment_id] = remaining
        else:
            self._committed.pop(deployment_id, None)
