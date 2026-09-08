"""``GET /api/models`` -- the one place to fetch every model.

The router lives here rather than in ``control_plane/gateway/`` so that this
feature adds exactly one line to a file somebody else owns. It reads the ports
off the gateway context and nothing else.

**One literal path, and deliberately no ``/api/models/{model_id}``.**
``capacity_api`` already owns ``/api/models/quant-table``, ``/search``,
``/detail`` and ``/variants``; a path-parameter sibling would shadow all four
depending on ``include_router`` order in ``create_app``, which is the kind of
breakage that arrives months later when somebody reorders the wiring for an
unrelated reason. A single model is ``/api/models?model_id=...``, and
``/api/models/detail`` already answers the resolver's version of that
question.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict
from typing import Any

from .records import ModelRecord

log = logging.getLogger(__name__)

#: How stale the fast half may be before a read refreshes it. Matches the
#: router's own index TTL: the sources are in-memory, so the only cost of
#: asking again is the merge itself, and the digest short-circuit means an
#: unchanged one writes nothing.
FAST_TTL_S = 2.0


def refresh_from_ports(inventory, deps, *, curated=None) -> None:
    """Read the live sources and hand them to the registry.

    Each feed is read under its own guard, in the ``_safe`` posture
    ``Router.rebuild`` already uses: a port that raises degrades **that feed
    only**, and the registry keeps the rows that feed contributed last time
    rather than emptying them. The sentence travels to the screen in
    ``sources``.
    """
    errors: dict[str, str] = {}

    deployments: list[Any] = []
    try:
        deployments = list(deps.deployments.list())
    except Exception as exc:
        log.warning("inventory: deployments unavailable: %s", exc)
        errors["deployments"] = f"The deployment manager did not answer: {exc}"

    provider_facts: list[dict] = []
    catalogues: dict[str, list[dict]] = {}
    providers = getattr(deps, "providers", None)
    if providers is not None:
        try:
            # `public_list` and `catalogue` are duck-typed, not on the frozen
            # ProviderPort. A port carrying only the protocol contributes no
            # provider rows rather than raising.
            lister = getattr(providers, "public_list", None)
            provider_facts = list(lister(include_models=False)) if lister else []
            catalogue = getattr(providers, "catalogue", None)
            if catalogue is not None:
                for fact in provider_facts:
                    pid = fact.get("provider_id")
                    if pid:
                        catalogues[pid] = list(catalogue(pid))
        except Exception as exc:
            log.warning("inventory: providers unavailable: %s", exc)
            errors["providers"] = f"The provider service did not answer: {exc}"

    if curated is None:
        try:
            from control_plane.fit.catalog import CURATED_MODELS

            curated = CURATED_MODELS
        except Exception as exc:  # pragma: no cover - a static import
            errors["catalog"] = f"The curated catalogue could not be read: {exc}"
            curated = ()

    inventory.refresh_fast(
        deployments=deployments,
        provider_facts=provider_facts,
        catalogues=catalogues,
        curated=curated,
        errors=errors,
    )


def record_payload(record: ModelRecord) -> dict[str, Any]:
    """One model, in the shape the screen already reads.

    ``providers`` and ``offers`` are split back apart here even though the
    database holds them in one table with a flag. The table is right -- it is
    what makes "served" and "merely published" one decision taken once -- and
    the two arrays are also right, because that is the distinction the pane
    draws, and a single list would let a Stop-serving button appear beside
    four hundred models nobody chose. Splitting on the way out costs a
    comprehension and keeps them disjoint by construction rather than by a
    filter the client has to remember to apply.
    """
    served = [p for p in record.providers if p.served]
    offered = [p for p in record.providers if not p.served]
    return {
        "model_id": record.model_id,
        "label": record.label,
        "detail": record.detail,
        "default_context": record.default_context,
        "default_concurrency": record.default_concurrency,
        "where": record.facets,
        "served_names": record.served_names,
        "deployments": [asdict(d) for d in record.deployments],
        "providers": [_provider_payload(p) for p in served],
        "offers": [_offer_payload(p) for p in offered],
        "cached_on": record.cached_on,
        "bytes_on_disk": record.bytes_on_disk,
    }


def _provider_payload(p) -> dict[str, Any]:
    return {
        "provider_id": p.provider_id,
        "display_name": p.display_name,
        "served_name": p.served_name,
        "upstream_id": p.upstream_id,
        "context_length": p.context_length,
        "modality": p.modality,
        "input_cost_per_mtok": p.input_cost_per_mtok,
        "output_cost_per_mtok": p.output_cost_per_mtok,
        "supports_tools": p.supports_tools,
        "supports_streaming": p.supports_streaming,
        "healthy": p.healthy,
        "last_error": p.last_error,
        "admitting": p.admitting,
        "admission_block": p.admission_block,
        "provider_enabled": p.provider_enabled,
    }


def _offer_payload(p) -> dict[str, Any]:
    """What a provider publishes but does not serve.

    No health, no admission: nothing is routing to it, so a health figure
    here would describe a path that does not exist.
    """
    return {
        "provider_id": p.provider_id,
        "display_name": p.display_name,
        "served_name": p.served_name,
        "upstream_id": p.upstream_id,
        "context_length": p.context_length,
        "modality": p.modality,
        "input_cost_per_mtok": p.input_cost_per_mtok,
        "output_cost_per_mtok": p.output_cost_per_mtok,
        "supports_tools": p.supports_tools,
        "supports_streaming": p.supports_streaming,
    }


def create_router(ctx):
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse

    from . import db as _db

    router = APIRouter()
    state = {"at": 0.0}

    @router.get("/api/models")
    async def list_models(model_id: str | None = None, where: str | None = None):
        """Every model this cluster knows about, from one place.

        Carries no fit verdict -- that is a function of (model, context,
        concurrency, node set) and stays on ``/api/capacity`` -- and no hub
        search hits, which stay on ``/api/models/search`` because that
        endpoint resolves nothing and its answers are a query's, not a fact
        about this cluster.
        """
        inventory = getattr(ctx, "inventory", None)
        if inventory is None:
            return JSONResponse(
                {
                    "models": [],
                    "sources": {
                        "store": {
                            "ok": False,
                            "reason": "This coordinator has no model registry.",
                        }
                    },
                    "revision": 0,
                    "schema_version": _db.SCHEMA_VERSION,
                    "generated_at": time.time(),
                }
            )

        now = time.time()
        if now - state["at"] >= FAST_TTL_S:
            state["at"] = now
            try:
                refresh_from_ports(inventory, ctx.deps)
            except Exception:
                # Never raise into a request handler over a view that can be
                # rebuilt. A stale answer with honest timestamps beats a 500.
                log.exception("inventory refresh failed; answering from the store")

        try:
            records = inventory.list_models()
            sources = inventory.sources()
            revision = inventory.revision
            store = {"ok": True, "reason": None}
        except Exception as exc:
            log.exception("inventory read failed")
            records, sources, revision = [], {}, 0
            store = {"ok": False, "reason": f"The model registry could not be read: {exc}"}

        rows = [record_payload(r) for r in records]
        if model_id:
            rows = [r for r in rows if r["model_id"] == model_id]
        if where:
            wanted = {w for w in where.split(",") if w}
            rows = [r for r in rows if wanted & set(r["where"])]

        sources["store"] = store
        return JSONResponse(
            {
                "models": rows,
                "sources": sources,
                "revision": revision,
                "schema_version": _db.SCHEMA_VERSION,
                "generated_at": now,
            }
        )

    return router
