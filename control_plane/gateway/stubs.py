"""Day 0 stubs.

The whole HTTP surface served from fixtures, so the UI could be built before
any other component worked. `GatewayDeps(strict=False)` still falls back to
these by default, and `contracts/routes.py` composes them into a dependency-
free gateway app purely to ask Starlette which routes it answers. Every stub
returns real contract types; a stub that returns something else is worse
than no stub.

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
    EngineOptions,
    Lifecycle,
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

    async def handle_join(self, proof, profile, agent_url, *, peer=None, facts=None) -> dict:
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
        modality=Modality.TEXT, served_name=None, extra_args=(), custom_command=(),
        revision=None, speculative_tokens=0, speculative=None,
        engine_options=EngineOptions(), original_window=None, lifecycle=Lifecycle(),
    ) -> Deployment:
        dep = _deployment(
            f"d-{len(self._deployments) + 1}",
            served_name or shape.model_id.split("/")[-1].lower(),
            shape,
            plan,
            fit,
            None,
            state=DeploymentState.LAUNCHING,
            context_length=ctx,
            max_concurrent_seqs=max_seqs,
        )
        dep.speculative_tokens = speculative_tokens
        # Kept as the real manager keeps them, so a deployment's flags read
        # back -- the cluster editor's flag chips and relaunch need them.
        dep.extra_args = tuple(extra_args)
        dep.custom_command = tuple(custom_command)
        dep.engine_options = engine_options
        dep.original_window = original_window
        dep.lifecycle = lifecycle
        self._deployments.append(dep)
        return dep

    def stop(self, deployment_id: str, *, reason: str | None = None) -> None:
        for dep in self._deployments:
            if dep.deployment_id == deployment_id:
                dep.state = DeploymentState.STOPPING

    def set_lifecycle(self, deployment_id: str, lifecycle: Lifecycle) -> Deployment | None:
        dep = self.get(deployment_id)
        if dep is not None:
            dep.lifecycle = lifecycle
        return dep

    def list(self) -> list[Deployment]:
        return list(self._deployments)

    def get(self, deployment_id: str) -> Deployment | None:
        return next(
            (d for d in self._deployments if d.deployment_id == deployment_id), None
        )


#: upstream_id -> the hosts OpenRouter would list for it:
#: (tag, provider_name, context_length, input $/Mtok, output $/Mtok, quantization).
#: Enough rows for the host picker to have a choice, a cheapest and a dearest.
_STUB_BACKENDS: dict[str, tuple[tuple, ...]] = {
    "qwen/qwen3-30b-a3b": (
        ("deepinfra", "DeepInfra", 40960, 0.08, 0.29, "fp8"),
        ("nebius", "Nebius AI Studio", 131072, 0.10, 0.30, None),
        ("together", "Together", 131072, 0.15, 0.60, "fp8"),
    ),
    "anthropic/claude-sonnet-4.5": (
        ("anthropic", "Anthropic", 200000, 3.0, 15.0, None),
        ("google-vertex", "Google Vertex", 200000, 3.5, 17.5, None),
        ("amazon-bedrock", "Amazon Bedrock", 200000, 3.0, 15.0, None),
    ),
}


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
                api_key_ref="DERATE_OPENROUTER_API_KEY",
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
        #: provider_id -> upstream_id -> pinned host tag / fallback flag, and
        #: (provider_id, upstream_id) -> the catalogue price a cleared pin
        #: restores. Beside the records for the same reason as the allowlist.
        self._pins: dict[str, dict[str, str]] = {}
        self._fallbacks: dict[str, dict[str, bool]] = {}
        self._catalogue_prices: dict[tuple[str, str], tuple[float | None, float | None]] = {}

    def list(self) -> list[Provider]:
        return list(self._providers)

    def add(self, spec: dict) -> Provider:
        # The same two screens the real service applies, in the same order and
        # with the same sentence. A stub that accepts what the coordinator
        # refuses is not a rehearsal of it: the add form learns what the key
        # fields mean from the 400 it gets back, and against this surface it
        # would learn the opposite.
        from control_plane.providers.service import (
            KEY_IN_REF_FIELD,
            REF_NOT_OURS,
            parse_daily_budget,
            minted_ref,
            ref_allowed,
        )

        kind = ProviderKind(spec.get("kind", "custom"))
        api_key = str(spec.get("api_key") or "").strip()
        api_key_ref = str(spec.get("api_key_ref") or "").strip()
        if looks_like_secret(api_key_ref):
            raise ValueError(KEY_IN_REF_FIELD)
        provider_id = spec.get("provider_id") or f"p-{len(self._providers) + 1}"
        if api_key_ref and not ref_allowed(api_key_ref, provider_id):
            raise ValueError(REF_NOT_OURS)
        parse_daily_budget(spec.get("daily_budget_usd"))
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

    def _screen(self, provider_id: str, patch: dict) -> Provider:
        """Every refusal ``update`` can make, before anything is written."""
        from control_plane.providers.service import (
            KEY_IN_REF_FIELD,
            REF_NOT_OURS,
            parse_daily_budget,
            ref_allowed,
        )

        provider = self._find(provider_id)
        if "daily_budget_usd" in patch:
            parse_daily_budget(patch["daily_budget_usd"])
        if "api_key_ref" in patch:
            ref = str(patch["api_key_ref"]).strip()
            if looks_like_secret(ref):
                raise ValueError(KEY_IN_REF_FIELD)
            if ref and not ref_allowed(ref, provider_id):
                raise ValueError(REF_NOT_OURS)
        if "priority" in patch:
            int(patch["priority"])
        if "enabled_models" in patch:
            wanted = {str(m) for m in (patch["enabled_models"] or [])}
            unknown = sorted(wanted - {m.upstream_id for m in provider.models})
            if unknown:
                raise ValueError(
                    f"{provider_id} does not publish {', '.join(unknown[:3])}"
                )
        if "backend_pins" in patch:
            self._screen_pins(provider, patch["backend_pins"])
        if "backend_fallbacks" in patch:
            self._screen_fallbacks(provider, patch["backend_fallbacks"])
        return provider

    def validate_patch(self, provider_id: str, patch: dict) -> None:
        """The real service's dry run: the same refusals, nothing written."""
        from control_plane.providers.service import WIRING_NO_API_KEY

        if not isinstance(patch, dict):
            raise ValueError("patch must be an object")
        if "api_key" in patch:
            raise ValueError(WIRING_NO_API_KEY)
        self._screen(provider_id, patch)

    def update(self, provider_id: str, patch: dict) -> Provider:
        provider = self._screen(provider_id, patch)
        if "backend_pins" in patch:
            # Merged a key at a time, and the pinned host's price becomes the
            # model's, as ProviderService._apply_pin_prices does; clearing
            # restores the catalogue price and drops the fallback flag.
            pins = self._pins.setdefault(provider_id, {})
            fallbacks = self._fallbacks.setdefault(provider_id, {})
            for upstream_id, row in self._screen_pins(provider, patch["backend_pins"]).items():
                model = next(m for m in provider.models if m.upstream_id == upstream_id)
                catalogue = self._catalogue_prices.setdefault(
                    (provider_id, upstream_id),
                    (model.input_cost_per_mtok, model.output_cost_per_mtok),
                )
                if row is None:
                    pins.pop(upstream_id, None)
                    fallbacks.pop(upstream_id, None)
                    model.input_cost_per_mtok, model.output_cost_per_mtok = catalogue
                else:
                    pins[upstream_id] = row["tag"]
                    model.input_cost_per_mtok = row["input_cost_per_mtok"]
                    model.output_cost_per_mtok = row["output_cost_per_mtok"]
        if "backend_fallbacks" in patch:
            fallbacks = self._fallbacks.setdefault(provider_id, {})
            for upstream_id, flag in self._screen_fallbacks(
                provider, patch["backend_fallbacks"]
            ).items():
                if flag:
                    fallbacks[upstream_id] = True
                else:
                    fallbacks.pop(upstream_id, None)
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
            self._enabled_models[provider_id] = frozenset(
                str(m) for m in (patch["enabled_models"] or [])
            )
        if "aliases" in patch:
            # The whole map, replaced, as ProviderService._apply_aliases does:
            # a model the map does not name serves under its upstream id.
            aliases = {str(k): str(v) for k, v in (patch["aliases"] or {}).items()}
            for model in provider.models:
                model.served_name = aliases.get(model.upstream_id, model.upstream_id)
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
                **serialize.provider_model_payload(
                    m,
                    pin=self._pins.get(provider_id, {}).get(m.upstream_id),
                    fallback=self._fallbacks.get(provider_id, {}).get(m.upstream_id, False),
                ),
                "enabled": allowed is None or m.upstream_id in allowed,
            }
            for m in provider.models
        ]

    def list_backends(self, provider_id: str, upstream_id: str) -> list[dict]:
        """A few hosts per model with their prices, the real call's row shape."""
        from control_plane.providers import AdapterUnsupportedError

        provider = self._find(provider_id)
        if not spec_for(provider.kind).supports_backend_routing:
            raise AdapterUnsupportedError(
                f"provider {provider_id!r} does not aggregate backend hosts per model"
            )
        pin = self._pins.get(provider_id, {}).get(upstream_id)
        return [
            {
                "tag": tag,
                "provider_name": name,
                "context_length": context,
                "input_cost_per_mtok": cost_in,
                "output_cost_per_mtok": cost_out,
                "quantization": quant,
                "pinned": tag == pin,
            }
            for tag, name, context, cost_in, cost_out, quant in _STUB_BACKENDS.get(upstream_id, ())
        ]

    def backend_pin_state(self, provider_id: str) -> dict[str, tuple[str, bool]]:
        self._find(provider_id)
        fallbacks = self._fallbacks.get(provider_id, {})
        return {
            upstream_id: (tag, bool(fallbacks.get(upstream_id)))
            for upstream_id, tag in self._pins.get(provider_id, {}).items()
        }

    def _screen_backend(self, provider: Provider, value: object, key: str) -> dict:
        """The real service's per-model screen, sentence for sentence."""
        if not spec_for(provider.kind).supports_backend_routing:
            raise ValueError(
                f"{provider.provider_id} does not aggregate backend hosts per model; "
                f"{key} does not apply"
            )
        if value is not None and not isinstance(value, dict):
            raise ValueError(f"{key} must be an object keyed by upstream model id")
        updates = {str(k): v for k, v in (value or {}).items()}
        unknown = sorted(set(updates) - {m.upstream_id for m in provider.models})
        if unknown:
            shown = ", ".join(unknown[:3])
            more = f" (and {len(unknown) - 3} more)" if len(unknown) > 3 else ""
            raise ValueError(
                f"{provider.provider_id} does not publish {shown}{more}; "
                "pin a backend only for a model from its own catalogue"
            )
        return updates

    def _screen_pins(self, provider: Provider, value: object) -> dict[str, dict | None]:
        """upstream_id -> the listed host row to pin to, or None to clear."""
        out: dict[str, dict | None] = {}
        for upstream_id, tag in self._screen_backend(provider, value, "backend_pins").items():
            tag = str(tag or "")
            if not tag:
                out[upstream_id] = None
                continue
            row = next(
                (r for r in self.list_backends(provider.provider_id, upstream_id)
                 if r["tag"] == tag),
                None,
            )
            if row is None:
                raise ValueError(
                    f"{provider.provider_id} lists no backend '{tag}' for {upstream_id}; "
                    "pick one from its backend list"
                )
            out[upstream_id] = row
        return out

    def _screen_fallbacks(self, provider: Provider, value: object) -> dict[str, bool]:
        updates = self._screen_backend(provider, value, "backend_fallbacks")
        for flag in updates.values():
            if not isinstance(flag, bool):
                raise ValueError("backend_fallbacks values must be true or false")
        return updates

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
        # A stub never holds real key material. The real provider service
        # resolves the real one from the environment or /data/secrets.json
        # at request time.
        return "stub-key-not-real"

    def health(self, provider_id: str) -> tuple[bool, str | None]:
        provider = next(
            (p for p in self._providers if p.provider_id == provider_id), None
        )
        if provider is None:
            return (False, "unknown provider")
        return (provider.healthy, provider.last_error)
