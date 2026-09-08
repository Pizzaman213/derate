"""Fold the live sources into one record per model. Pure, given its inputs.

This is the Python port of ``ui/src/tabs/models/rows.ts``'s five builders and
``mergeRows``. It is kept free of I/O and of the database for the same reason
``gateway/targets.py::build_index`` is "pure given its inputs": that is what
makes a merge testable, and this one is about to become the only merge.

The key is ``model_id``, **exact, never case-folded**. That is not an
oversight. OpenRouter spells a model ``qwen/qwen3-30b-a3b`` where the hub
spells it ``Qwen/Qwen3-30B-A3B``, and folding the two together would hand an
un-served remote row a local verdict for weights that are not the same thing.

The ``ondisk`` facet is deliberately absent from everything here. Weights on
disk cost a fan-out to every node agent and refresh on their own slower
cadence, so they live in their own tables and are joined at read time -- see
:mod:`control_plane.inventory.service`.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from .records import FACET_ORDER, ModelRecord, RowDeployment, RowProvider

log = logging.getLogger(__name__)


def build(
    *,
    deployments: Sequence[Any] = (),
    provider_facts: Sequence[Mapping[str, Any]] = (),
    catalogues: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    curated: Sequence[Any] = (),
    now: float = 0.0,
) -> list[ModelRecord]:
    """Merge the fast sources into one record per model id.

    ``provider_facts`` is ``ProviderService.public_list(include_models=False)``
    -- the provider-level runtime facts (display name, health, admission).
    ``catalogues`` maps a provider id to ``ProviderService.catalogue(id)``,
    every published model with an ``enabled`` flag saying whether the
    allowlist lets it be served. One call answers both halves: ``enabled``
    is exactly the predicate that decides whether a model appears in
    ``/api/providers``, so a row is `served` when it is true and `offered`
    when it is false.
    """
    out: dict[str, ModelRecord] = {}

    # Order matters, and matches the order the browser folded these in: local,
    # structured facts first. `_touch` keeps the first label it is given
    # unless a curated one arrives, because a human wrote that one.
    _add_running(out, deployments, now)
    _add_curated(out, curated, now)
    _add_providers(out, provider_facts, catalogues or {}, now)

    for record in out.values():
        seen = set(record.facets)
        record.facets = [f for f in FACET_ORDER if f in seen]
        record.served_names = sorted(set(record.served_names))
    return list(out.values())


def _touch(
    out: dict[str, ModelRecord],
    model_id: str,
    label: str,
    facet: str,
    now: float,
    *,
    curated: bool = False,
) -> ModelRecord:
    record = out.get(model_id)
    if record is None:
        record = ModelRecord(model_id=model_id, label=label or model_id, observed_at=now)
        out[model_id] = record
    elif curated:
        # The only label worth preferring over one already taken: the sort is
        # on the label, and a served name that differs from the repo id would
        # file the row somewhere surprising.
        record.label = label or record.label
    if facet not in record.facets:
        record.facets.append(facet)
    return record


def _add_running(
    out: dict[str, ModelRecord], deployments: Sequence[Any], now: float
) -> None:
    """Every deployment, including the finished ones.

    A FAILED or STOPPED deployment is still the answer to "what happened to
    this model", and the screen bands it by its verdict while saying what went
    wrong. Filtering them here would delete that sentence.
    """
    for dep in deployments:
        shape = getattr(dep, "shape", None)
        model_id = getattr(shape, "model_id", None)
        if not model_id:
            continue
        record = _touch(out, model_id, model_id, "running", now)
        served_name = getattr(dep, "served_name", "") or ""
        if served_name:
            record.served_names.append(served_name)
        plan = getattr(dep, "plan", None)
        state = getattr(dep, "state", None)
        record.deployments.append(
            RowDeployment(
                deployment_id=getattr(dep, "deployment_id", ""),
                served_name=served_name,
                state=getattr(state, "value", state) or "",
                runtime=getattr(dep, "runtime", "") or "",
                node_ids=list(getattr(plan, "node_ids", None) or []),
                last_error=getattr(dep, "last_error", None),
            )
        )


def _add_curated(out: dict[str, ModelRecord], curated: Sequence[Any], now: float) -> None:
    for entry in curated:
        model_id = getattr(entry, "model_id", None)
        if not model_id:
            continue
        record = _touch(
            out, model_id, getattr(entry, "label", "") or model_id, "catalog", now, curated=True
        )
        record.detail = getattr(entry, "detail", "") or record.detail
        if record.default_context is None:
            record.default_context = getattr(entry, "default_context", None)
        if record.default_concurrency is None:
            record.default_concurrency = getattr(entry, "default_concurrency", None)


def _add_providers(
    out: dict[str, ModelRecord],
    provider_facts: Sequence[Mapping[str, Any]],
    catalogues: Mapping[str, Sequence[Mapping[str, Any]]],
    now: float,
) -> None:
    """Both halves of every provider, from one catalogue call each.

    A provider with no catalogue entry contributes nothing rather than
    raising: ``catalogue()`` is one of the duck-typed reads (it is not on the
    frozen ``ProviderPort``), so a port that does not implement it degrades to
    a registry with no provider rows instead of a traceback.
    """
    facts = {f.get("provider_id"): f for f in provider_facts if f.get("provider_id")}
    for provider_id, rows in catalogues.items():
        fact = facts.get(provider_id, {})
        display = fact.get("display_name") or provider_id
        for model in rows or []:
            upstream_id = model.get("upstream_id")
            if not upstream_id:
                continue
            served = bool(model.get("enabled"))
            facet = "provider" if served else "offered"
            record = _touch(out, upstream_id, upstream_id, facet, now)
            served_name = model.get("served_name") or ""
            # An un-served model claims no served name. Nothing answers to it
            # at /v1, and letting it into `served_names` would make the row
            # searchable by a name that routes nowhere.
            if served and served_name:
                record.served_names.append(served_name)
            record.providers.append(
                RowProvider(
                    provider_id=provider_id,
                    display_name=display,
                    served_name=served_name,
                    upstream_id=upstream_id,
                    served=served,
                    provider_enabled=fact.get("enabled"),
                    context_length=model.get("context_length"),
                    modality=model.get("modality"),
                    input_cost_per_mtok=model.get("input_cost_per_mtok"),
                    output_cost_per_mtok=model.get("output_cost_per_mtok"),
                    supports_tools=model.get("supports_tools"),
                    supports_streaming=model.get("supports_streaming"),
                    # Provider-level facts, carried per row because that is
                    # where the screen reads them. Only meaningful for a
                    # served row: nothing is routing to an un-served one, so
                    # its health says nothing about it.
                    healthy=fact.get("healthy") if served else None,
                    last_error=fact.get("last_error") if served else None,
                    admitting=fact.get("admitting") if served else None,
                    admission_block=fact.get("admission_block") if served else None,
                )
            )
