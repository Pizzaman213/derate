"""Day 0 stubs.

The whole HTTP surface served from fixtures, so Agent H can build the entire
UI before any other component works and only switch to live data at
integration. Every stub returns real contract types; a stub that returns
something else is worse than no stub.

Numbers come from the shared day-0 fixtures when they are importable, so the
stub agrees with every other agent's tests. It falls back to equivalent local
values when the gateway runs outside the repo.
"""

from __future__ import annotations

import time
from dataclasses import replace

from control_plane.contracts import (
    Deployment,
    DeploymentState,
    LinkMeasurement,
    Modality,
    ModelShape,
    ParallelismKind,
    ParallelismPlan,
    Provider,
    ProviderKind,
    ProviderModel,
)
from control_plane.providers import UnknownProviderError, looks_like_secret, spec_for
from control_plane.registry import NodeNotFound

try:  # shared day-0 fixtures: the same numbers every other agent tests against
    from tests.fixtures import (  # type: ignore[import-not-found]
        LINKS,
        MODEL_SHAPES,
        NODE_PROFILES,
        NODE_STATES,
        fits,
        pp2_plan,
        single_node_plan,
    )

    _HAVE_FIXTURES = True
except Exception:  # pragma: no cover - only when running outside the repo
    _HAVE_FIXTURES = False


GIB = 1024**3


def _fallback_shape(model_id: str) -> ModelShape:
    return ModelShape(
        model_id=model_id,
        num_layers=80,
        hidden_size=8192,
        num_attention_heads=64,
        num_kv_heads=8,
        vocab_size=128256,
        total_params=70_553_706_496,
        dtype="bf16",
        head_dim=128,
    )


class StubRegistry:
    """Two Sparks and a 3090, all healthy.

    Method names and exception types mirror the real
    :class:`control_plane.registry.Registry` (and its own day-0 stub,
    :class:`control_plane.registry.StubRegistry`) exactly, so the gateway's
    HTTP layer needs no stub-specific branch to degrade gracefully.
    """

    def __init__(self) -> None:
        self._nodes = list(NODE_STATES.values()) if _HAVE_FIXTURES else []

    def list_nodes(self):
        return list(self._nodes)

    def get_node(self, node_id: str):
        return next((n for n in self._nodes if n.profile.node_id == node_id), None)

    def healthy_nodes(self):
        return [n for n in self._nodes if n.healthy]

    # Beyond the port, mirroring control_plane.registry.Registry
    def admit(self, node_id: str):
        node = self.get_node(node_id)
        if node is None:
            raise NodeNotFound(node_id)
        return node

    def remove_node(self, node_id: str) -> None:
        self._nodes = [n for n in self._nodes if n.profile.node_id != node_id]

    def candidates(self) -> list[dict]:
        return []

    async def handle_join(self, token, profile, agent_url) -> dict:
        raise NotImplementedError("stub registry does not accept joins")


class StubLinks:
    """The two measured links: the ConnectX-7 pair and the ethernet hop."""

    def __init__(self) -> None:
        self._links = dict(LINKS) if _HAVE_FIXTURES else {}

    def get(self, a: str, b: str) -> LinkMeasurement | None:
        return self._links.get(tuple(sorted((a, b))))

    def worst_all_reduce(self, node_ids: list[str]) -> LinkMeasurement | None:
        candidates = [
            m
            for key, m in self._links.items()
            if key[0] in node_ids and key[1] in node_ids
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda m: m.all_reduce_gbps)

    def measure(self, a: str, b: str) -> LinkMeasurement:
        existing = self.get(a, b)
        if existing is not None:
            return existing
        return LinkMeasurement(
            src=a,
            dst=b,
            all_reduce_gbps=10.2,
            sendrecv_gbps=9.0,
            latency_us=40.0,
            gpudirect_rdma=False,
            measured_at=time.time(),
            method="manual",
        )


class StubResolver:
    def resolve(self, model_id: str, dtype: str | None = None) -> ModelShape:
        if _HAVE_FIXTURES:
            key = model_id.split("/")[-1].lower()
            for name, shape in MODEL_SHAPES.items():
                if name in key or key in name or shape.model_id == model_id:
                    return shape
        return _fallback_shape(model_id)


class StubFit:
    def check(self, req, nodes):
        return fits() if _HAVE_FIXTURES else None

    def max_context(self, shape, plan, nodes, max_seqs, kv_dtype) -> int:
        return 131072


class StubPlanner:
    def plan(
        self,
        shape,
        nodes,
        link,
        target,
        concurrency,
        *,
        context_length: int | None = None,
        kv_dtype: str | None = None,
    ) -> ParallelismPlan:
        # context_length/kv_dtype are the real PlannerPort's keyword-only
        # extras (00-architecture.md 4.7); this fixed-shape stub has no use
        # for them but must not choke when a caller passes them.
        node_ids = [n.node_id for n in nodes] if nodes else ["spark-01"]
        if _HAVE_FIXTURES:
            if len(node_ids) >= 2:
                return pp2_plan(node_ids[:2])
            return single_node_plan(node_ids[0])
        return ParallelismPlan(
            kind=ParallelismKind.SINGLE_NODE,
            tensor_parallel=1,
            pipeline_parallel=1,
            expert_parallel=1,
            data_parallel=1,
            node_ids=node_ids[:1],
            reason="Stub plan.",
            measured_link_gbps=0.0,
            rejected=[],
        )


def _deployment(
    deployment_id: str,
    served_name: str,
    shape: ModelShape,
    plan: ParallelismPlan,
    fit,
    backend_url: str,
    *,
    state: DeploymentState = DeploymentState.READY,
    context_length: int = 32768,
    max_concurrent_seqs: int = 32,
) -> Deployment:
    return Deployment(
        deployment_id=deployment_id,
        served_name=served_name,
        shape=shape,
        plan=plan,
        fit=fit,
        runtime="vllm",
        state=state,
        backend_url=backend_url,
        context_length=context_length,
        max_concurrent_seqs=max_concurrent_seqs,
        started_at=time.time(),
        last_error=None,
    )


class StubDeployments:
    """One model on two deliberately unequal replicas, so the UI has weighted
    routing to draw on day 0, plus a single-replica model that is also served
    remotely, plus one still launching so the 503-with-state path is visible.
    """

    def __init__(self) -> None:
        self._deployments: list[Deployment] = []
        if not _HAVE_FIXTURES:
            return

        llama = MODEL_SHAPES["llama-3.3-70b"]
        qwen = MODEL_SHAPES["qwen3-30b-a3b"]
        gpt_oss = MODEL_SHAPES["gpt-oss-120b"]

        strong = fits()
        strong.predicted_decode_tps = 42.0
        weak = fits()
        weak.predicted_decode_tps = 12.0

        self._deployments = [
            _deployment(
                "d-1",
                "llama-3.3-70b",
                llama,
                pp2_plan(["spark-01", "spark-02"]),
                strong,
                "http://192.168.11.13:8000/v1",
            ),
            _deployment(
                "d-2",
                "llama-3.3-70b",
                llama,
                single_node_plan("ws-3090"),
                weak,
                "http://192.168.11.20:8000/v1",
            ),
            _deployment(
                "d-3",
                "qwen3-30b-a3b",
                qwen,
                single_node_plan("spark-02"),
                fits(),
                "http://192.168.11.14:8000/v1",
            ),
            _deployment(
                "d-4",
                "gpt-oss-120b",
                gpt_oss,
                pp2_plan(["spark-01", "spark-02"]),
                fits(),
                None,
                state=DeploymentState.LAUNCHING,
            ),
        ]

    def launch(
        self, shape, plan, fit, runtime, ctx, max_seqs, *,
        modality=Modality.TEXT, extra_args=(), custom_command=(),
    ) -> Deployment:
        dep = _deployment(
            f"d-{len(self._deployments) + 1}",
            shape.model_id.split("/")[-1].lower(),
            shape,
            plan,
            fit,
            None,
            state=DeploymentState.LAUNCHING,
            context_length=ctx,
            max_concurrent_seqs=max_seqs,
        )
        self._deployments.append(dep)
        return dep

    def stop(self, deployment_id: str) -> None:
        for dep in self._deployments:
            if dep.deployment_id == deployment_id:
                dep.state = DeploymentState.STOPPING

    def list(self) -> list[Deployment]:
        return list(self._deployments)

    def get(self, deployment_id: str) -> Deployment | None:
        return next(
            (d for d in self._deployments if d.deployment_id == deployment_id), None
        )


class StubProviders:
    """One remote provider. One of its models shares a served_name with a local
    deployment, which is the case worth demonstrating: one entry in /v1/models
    with both a local and a remote target behind it.
    """

    def __init__(self) -> None:
        self._providers = [
            Provider(
                provider_id="openrouter",
                kind=ProviderKind.OPENROUTER,
                display_name="OpenRouter",
                base_url="https://openrouter.ai/api/v1",
                api_key_ref="OPENROUTER_API_KEY",
                enabled=True,
                priority=10,
                models=[
                    ProviderModel(
                        served_name="qwen3-30b-a3b",
                        upstream_id="qwen/qwen3-30b-a3b",
                        context_length=131072,
                        supports_streaming=True,
                        supports_tools=True,
                        input_cost_per_mtok=0.10,
                        output_cost_per_mtok=0.30,
                    ),
                    ProviderModel(
                        served_name="claude-sonnet-4.5",
                        upstream_id="anthropic/claude-sonnet-4.5",
                        context_length=200000,
                        supports_streaming=True,
                        supports_tools=True,
                        input_cost_per_mtok=3.0,
                        output_cost_per_mtok=15.0,
                    ),
                ],
                healthy=True,
                last_error=None,
                last_refreshed=time.time(),
            )
        ]
        #: provider_id -> the models switched on, or absent for a provider that
        #: predates the allowlist and therefore serves all of them. Held beside
        #: the records rather than on them: `Provider` is a frozen contract and
        #: the real service keeps this on `ProviderRuntime` for the same reason.
        self._enabled_models: dict[str, frozenset[str]] = {}

    def list(self) -> list[Provider]:
        return list(self._providers)

    def add(self, spec: dict) -> Provider:
        # The same two screens the real service applies, in the same order and
        # with the same sentence. A stub that accepts what the coordinator
        # refuses is not a rehearsal of it: the add form learns what the key
        # fields mean from the 400 it gets back, and against this surface it
        # would learn the opposite.
        from control_plane.providers.service import KEY_IN_REF_FIELD, minted_ref

        kind = ProviderKind(spec.get("kind", "custom"))
        api_key = str(spec.get("api_key") or "").strip()
        api_key_ref = str(spec.get("api_key_ref") or "").strip()
        if looks_like_secret(api_key_ref):
            raise ValueError(KEY_IN_REF_FIELD)
        provider_id = spec.get("provider_id") or f"p-{len(self._providers) + 1}"
        # A stub has no secrets file to write to, so it does the visible half:
        # the reference the real service would have stored the key under. Off
        # this provider's own id, not the kind, because that is the name the
        # row's "Replace key" control predicts when it PATCHes later.
        # Dropping the field instead answered 201 with an empty reference and a
        # key_state of "not_needed", which reads as a paste path that silently
        # does nothing -- the one failure this surface exists to not have.
        if api_key and not api_key_ref:
            api_key_ref = minted_ref(provider_id)
        provider = Provider(
            provider_id=provider_id,
            kind=kind,
            display_name=spec.get("display_name", "Custom"),
            # The kind's default, the way the real service fills it. Reading
            # spec["base_url"] instead made the add form's own "leave it blank
            # and take the default" a KeyError, and a KeyError is not a
            # sentence naming what to change.
            base_url=spec.get("base_url") or spec_for(kind).base_url,
            api_key_ref=api_key_ref,
            enabled=spec.get("enabled", True),
            priority=spec.get("priority", 100),
            models=[],
            healthy=True,
            last_error=None,
            last_refreshed=time.time(),
        )
        self._providers.append(provider)
        return provider

    def _find(self, provider_id: str) -> Provider:
        provider = next(
            (p for p in self._providers if p.provider_id == provider_id), None
        )
        if provider is None:
            raise UnknownProviderError(provider_id)
        return provider

    def refresh(self, provider_id: str) -> Provider:
        provider = self._find(provider_id)
        provider.last_refreshed = time.time()
        return provider

    def update(self, provider_id: str, patch: dict) -> Provider:
        provider = self._find(provider_id)
        if "enabled" in patch:
            provider.enabled = bool(patch["enabled"])
        if "priority" in patch:
            provider.priority = int(patch["priority"])
        if "display_name" in patch:
            provider.display_name = str(patch["display_name"])
        if "base_url" in patch:
            provider.base_url = str(patch["base_url"])
        if "api_key_ref" in patch:
            provider.api_key_ref = str(patch["api_key_ref"])
        if "api_key" in patch:
            # A stub has no secrets file to write to, so it does the visible
            # half: the reference the real service would have stored under.
            # Dropping the field instead would report a rotation to a UI that
            # then shows the same key state it showed before.
            from control_plane.providers.service import minted_ref

            if "api_key_ref" not in patch:
                provider.api_key_ref = minted_ref(provider_id)
        if "enabled_models" in patch:
            wanted = {str(m) for m in (patch["enabled_models"] or [])}
            unknown = sorted(wanted - {m.upstream_id for m in provider.models})
            if unknown:
                raise ValueError(
                    f"{provider_id} does not publish {', '.join(unknown[:3])}"
                )
            self._enabled_models[provider_id] = frozenset(wanted)
        return provider

    def servable(self) -> list[Provider]:
        """The providers, carrying only the models switched on.

        The stub's two models both start on, because this provider was here
        before the allowlist was and the rule for such a record is that it
        keeps serving everything. Demonstrating an empty cluster would make
        the day-0 surface show nothing at all.
        """
        out = []
        for provider in self._providers:
            allowed = self._enabled_models.get(provider.provider_id)
            clone = replace(
                provider,
                models=[
                    m for m in provider.models
                    if allowed is None or m.upstream_id in allowed
                ],
            )
            out.append(clone)
        return out

    def catalogue(self, provider_id: str) -> list[dict]:
        """Every model, each saying whether it is switched on."""
        from control_plane.gateway import serialize

        provider = self._find(provider_id)
        allowed = self._enabled_models.get(provider_id)
        return [
            {
                **serialize.provider_model_payload(m),
                "enabled": allowed is None or m.upstream_id in allowed,
            }
            for m in provider.models
        ]

    def key_status(self, provider_id: str) -> dict:
        """Whether the credential resolves. A stub's always does.

        No source: there is no environment and no secrets.json behind this,
        and naming one of them would be a claim about a file that does not
        exist. The UI renders a state without a place.
        """
        provider = self._find(provider_id)
        if not provider.api_key_ref:
            return {"key_state": "not_needed", "key_source": None}
        return {"key_state": "set", "key_source": None}

    def remove(self, provider_id: str) -> None:
        provider = self._find(provider_id)
        self._providers.remove(provider)

    def models(self) -> list[tuple[str, ProviderModel]]:
        return [(p.provider_id, m) for p in self._providers for m in p.models]

    def resolve_key(self, provider_id: str) -> str:
        # A stub never holds real key material. Agent I resolves the real one
        # from the environment or /data/secrets.json at request time.
        return "stub-key-not-real"

    def health(self, provider_id: str) -> tuple[bool, str | None]:
        provider = next(
            (p for p in self._providers if p.provider_id == provider_id), None
        )
        if provider is None:
            return (False, "unknown provider")
        return (provider.healthy, provider.last_error)
