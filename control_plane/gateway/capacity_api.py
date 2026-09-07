"""Memory truth, and what could run on it.

A separate router rather than more of ``internal_api.py``, which is already
955 lines and two dozen endpoints in one closure that several sessions edit at
once. FastAPI composes routers natively and ``app.py`` already includes four.

Registration must stay ABOVE the ``StaticFiles`` mount in ``app.py`` -- the
same trap ``ui_api`` documents at length. A root mount catches every path not
matched by an EARLIER route, so a router registered after it silently answers
``index.html`` instead of JSON.

Everything here reads. Nothing on these routes changes cluster state.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter
from starlette.responses import JSONResponse, Response

from control_plane.contracts import DEFAULT_GUARDRAIL

from . import errors, serialize
from .deps import GatewayContext

log = logging.getLogger("gateway.capacity")

#: Beyond this the answer stops being "what can I run" and starts being a
#: denial-of-service against the HuggingFace hub.
MAX_CATALOG_MODELS = 8

#: A resolve may need several hub round trips on a cold cache. Past this the
#: honest answer is a timeout with a sentence, not a request that never returns.
_DETAIL_TIMEOUT_S = 12.0

#: Enumerating a ladder costs a resolve plus a hub search plus a model_info per
#: GGUF repo. Bounded, and memoised, because a person clicking down a catalogue
#: would otherwise re-run all of it per click.
_VARIANTS_TIMEOUT_S = 20.0
_VARIANTS_TTL_S = 600.0

#: An unauthenticated hub is rate limited, and a person types faster than it
#: answers. The client debounces too; this stops several browsers undoing that.
_SEARCH_TIMEOUT_S = 6.0
_SEARCH_TTL_S = 60.0


def create_router(ctx: GatewayContext) -> APIRouter:
    router = APIRouter()

    def _registry_call(name: str):
        fn = getattr(ctx.deps.registry, name, None)
        return fn if callable(fn) else None

    def _node_ids() -> list[str]:
        try:
            return [n.profile.node_id for n in ctx.deps.registry.healthy_nodes()]
        except Exception:
            log.exception("registry unavailable")
            return []

    def _profiles() -> list:
        try:
            return [n.profile for n in ctx.deps.registry.healthy_nodes()]
        except Exception:
            log.exception("registry unavailable")
            return []

    def _report(node_id: str) -> dict | None:
        """One node's memory picture, plus the derived figures the UI needs.

        ``Registry.memory_report`` has existed since the registry was written
        and its docstring says it is "for the UI and for a refusal". Nothing
        ever called it. This is that caller.
        """
        report = _registry_call("memory_report")
        if report is None:
            return None
        try:
            body = report(node_id)
        except Exception:
            log.exception("memory_report failed for %s", node_id)
            return None
        if body is None:
            return None

        body = dict(body)
        ceiling = body.get("static_ceiling") or 0
        allocatable = body.get("allocatable")
        addressable = body.get("addressable") or 0
        used = body.get("pool_used") or 0

        # Which of the two limits is actually binding. On a unified part this
        # is usually the host, and saying so is the difference between "the
        # model is too big" and "the desktop is in the way".
        gpu_headroom = max(0, ceiling - used)
        host_available = body.get("host_available") or 0
        host_reserve = body.get("host_reserve") or 0
        host_headroom = max(0, host_available - host_reserve)
        if allocatable is None:
            binding = None
        elif body.get("unified_memory") and host_headroom <= gpu_headroom:
            binding = "host"
        else:
            binding = "gpu"

        body["binding_limit"] = binding
        body["guardrail"] = DEFAULT_GUARDRAIL
        # Three named percentages rather than one ambiguous one. The pressure
        # figure is the only one that accounts for both the foreign load and
        # the host reserve, and it is the one to colour on.
        body["memory_used_pct"] = (
            round(used / addressable * 100.0, 1) if addressable else None
        )
        body["memory_pressure_pct"] = (
            round((1 - allocatable / ceiling) * 100.0, 1)
            if ceiling and allocatable is not None
            else None
        )
        body["memory_warn_pct"] = 90.0
        body["memory_critical_pct"] = 95.0
        pressure = body["memory_pressure_pct"]
        body["memory_severity"] = (
            None
            if pressure is None
            else "critical"
            if pressure >= 95.0
            else "warning"
            if pressure >= 90.0
            else "ok"
        )
        body["sampled_at"] = time.time()
        return body

    @router.get("/api/memory")
    async def memory_all() -> Response:
        """Every node's memory picture. What the UI polls app-wide.

        One poll for the whole app, so the header, the node rails and the
        headroom view cannot disagree about a number they all show.
        """
        if _registry_call("memory_report") is None:
            return errors.error_response(
                503,
                "This registry does not report memory. Nothing here is a "
                "measurement, so nothing is reported.",
                "server_error",
                "memory_report_unavailable",
            )
        nodes = [r for r in (_report(n) for n in _node_ids()) if r is not None]
        return JSONResponse({"nodes": nodes, "measured_at": time.time()})

    @router.get("/api/nodes/{node_id}/memory")
    async def memory_one(node_id: str) -> Response:
        # No shadowing risk against internal_api's /api/nodes/{node_id}: a
        # path parameter does not span a "/".
        if _registry_call("memory_report") is None:
            return errors.error_response(
                503,
                "This registry does not report memory.",
                "server_error",
                "memory_report_unavailable",
            )
        body = _report(node_id)
        if body is None:
            return errors.error_response(
                404,
                f"No memory report for node '{node_id}'.",
                "invalid_request_error",
                "node_not_found",
            )
        return JSONResponse(body)

    @router.get("/api/catalog")
    async def catalog() -> Response:
        from control_plane.fit.catalog import catalog_payload

        return JSONResponse(catalog_payload())

    @router.get("/api/models/quant-table")
    async def quant_table() -> Response:
        """Every priced quantization scheme, with what each runtime makes of it.

        Static: no ports, no network, no failure mode. It exists so that no
        client ever re-types a bytes-per-parameter figure. A second copy of
        this table in TypeScript is a second answer that can disagree with the
        fit gate, and the disagreement would surface as a launch that was
        promised to fit and then did not.

        `schemes` is a list rather than an object keyed by name because the UI
        compiles under `noUncheckedIndexedAccess`, where every lookup into a
        record is `T | undefined` and every call site has to branch on a case
        that cannot happen.
        """
        from control_plane.contracts.quant import (
            BYTES_PER_PARAM,
            DEFAULT_DTYPE,
            QUANT_INFO,
        )
        from control_plane.fit.constants import QUANT_SUGGESTION_ORDER
        from control_plane.resolver.support import RUNTIMES

        schemes = []
        for key in sorted(BYTES_PER_PARAM, key=lambda k: BYTES_PER_PARAM[k]):
            info = QUANT_INFO[key]
            schemes.append(
                {
                    "key": key,
                    "bytes_per_param": BYTES_PER_PARAM[key],
                    "bits_per_weight": info.bits_per_weight,
                    "family": info.family,
                    "native_compute_capability": info.native_compute_capability,
                    "emulated_below_native": info.emulated_below_native,
                    "note": info.note,
                    "runtimes": {
                        name: profile.quants.get(key, "unsupported").value
                        if hasattr(profile.quants.get(key, None), "value")
                        else "unsupported"
                        for name, profile in RUNTIMES.items()
                    },
                }
            )
        return JSONResponse(
            {
                "default_dtype": DEFAULT_DTYPE,
                "suggestion_order": list(QUANT_SUGGESTION_ORDER),
                "schemes": schemes,
            },
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @router.get("/api/models/search")
    async def model_search(q: str = "", limit: int = 40) -> Response:
        """Models from three places at once, local first, none of them resolved.

        Deployments and provider catalogues are already in memory and answer
        instantly; the hub is the slow, failable one. Its failure degrades this
        endpoint rather than emptying it -- with no network you still get
        everything already running here, and `sources.huggingface.note` says in
        the hub's own words why there is nothing else.

        Nothing is resolved. Fifty rows would be fifty hub round trips per
        keystroke, so every row carries `"resolved": false` and the client is
        expected not to draw a shape it was not given.
        """
        query = (q or "").strip()
        limit = max(1, min(int(limit or 40), 50))
        sources: dict[str, dict] = {}
        results: list[dict] = []
        notes: list[str] = []

        try:
            for dep in ctx.deps.deployments.list():
                model_id = dep.shape.model_id if dep.shape else None
                if not model_id:
                    continue
                results.append({
                    "model_id": model_id, "origin": "deployment",
                    "served_name": dep.served_name, "state": dep.state.value,
                    "resolved": False,
                })
            sources["deployments"] = {"ok": True, "note": None}
        except Exception as exc:
            log.exception("deployment list unavailable during search")
            sources["deployments"] = {"ok": False, "note": str(exc)}

        try:
            # `models()` reports enabled providers only, so a disabled one's
            # catalogue is not advertised as something you could route to.
            for provider_id, model in ctx.deps.providers.models():
                results.append({
                    "model_id": model.upstream_id, "origin": "provider",
                    "served_name": model.served_name, "provider_id": provider_id,
                    "resolved": False,
                })
            sources["providers"] = {"ok": True, "note": None}
        except Exception as exc:
            log.exception("provider models unavailable during search")
            sources["providers"] = {"ok": False, "note": str(exc)}

        search = getattr(ctx.deps.resolver, "search_models", None)
        if not query:
            sources["huggingface"] = {
                "ok": True, "note": "type to search the hub",
            }
        elif not callable(search):
            sources["huggingface"] = {
                "ok": False, "note": "this resolver cannot search the hub",
            }
        else:
            cache_key = f"{query}\u0000{limit}"
            hits = _search_cache.get(cache_key)
            if hits is None:
                try:
                    hits = await asyncio.wait_for(
                        asyncio.to_thread(search, query, limit),
                        timeout=_SEARCH_TIMEOUT_S,
                    )
                    _search_cache.put(cache_key, hits)
                except asyncio.TimeoutError:
                    hits = None
                    sources["huggingface"] = {
                        "ok": False,
                        "note": f"the hub did not answer within {_SEARCH_TIMEOUT_S:.0f}s",
                    }
                except Exception as exc:
                    hits = None
                    # The resolver's own sentence: it names the cause, and on a
                    # gated repo it says to set HF_TOKEN.
                    sources["huggingface"] = {"ok": False, "note": str(exc)}
            if hits is not None:
                for hit in hits:
                    results.append({**hit, "origin": "hub"})
                sources["huggingface"] = {"ok": True, "note": None}

        from control_plane.resolver.hf import hf_token

        if not hf_token():
            notes.append(
                "HF_TOKEN is not set: gated repositories will not appear here "
                "and will fail to resolve."
            )

        if query:
            needle = query.lower()
            results = [
                r for r in results
                if needle in str(r.get("model_id", "")).lower()
                or needle in str(r.get("served_name") or "").lower()
            ]

        return JSONResponse(
            {"query": query, "sources": sources, "notes": notes, "results": results}
        )

    @router.get("/api/models/detail")
    async def model_detail(model_id: str = "", revision: str = "") -> Response:
        """One model, resolved, with this cluster's verdict on its quantization.

        The id travels as a query parameter, not a path segment: model ids
        contain "/", a local id can be an absolute path, and ASGI servers and
        proxies disagree about whether %2F is decoded before routing. There is
        a Vite dev proxy in the chain too.
        """
        from control_plane.resolver.types import (
            MetadataUnavailable,
            ModelNotFound,
            UnsupportedArchitecture,
        )

        model_id = (model_id or "").strip()
        if not model_id:
            return errors.error_response(
                400, "model_id is required.", "invalid_request_error", "invalid_request"
            )

        full = getattr(ctx.deps.resolver, "resolve_full", None)
        if not callable(full):
            return errors.error_response(
                501,
                "This resolver cannot describe a model in detail; it only "
                "reports shapes. Nothing further can be said about "
                f"{model_id!r} here.",
                "server_error",
                "resolver_lacks_detail",
            )

        try:
            # requests is blocking and this is an async route: off the loop, or
            # one slow hub call stalls every other request and the SSE stream.
            resolution = await asyncio.wait_for(
                asyncio.to_thread(full, model_id, None), timeout=_DETAIL_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            return errors.error_response(
                504,
                f"Resolving {model_id!r} took longer than {_DETAIL_TIMEOUT_S:.0f}s.",
                "server_error",
                "resolve_timeout",
            )
        except ModelNotFound as exc:
            return errors.error_response(
                404, str(exc), "invalid_request_error", "model_not_found"
            )
        except UnsupportedArchitecture as exc:
            return errors.error_response(
                422, str(exc), "invalid_request_error", "unsupported_architecture"
            )
        except MetadataUnavailable as exc:
            # The resolver's own sentence names all three causes -- wrong id,
            # gated repo, missing HF_TOKEN -- and is better than anything
            # written here. Passed through untouched.
            return errors.error_response(
                502, str(exc), "server_error", "metadata_unavailable"
            )
        except Exception as exc:
            log.exception("resolve failed for %s", model_id)
            return errors.error_response(
                502,
                f"Could not resolve {model_id!r}: {type(exc).__name__}.",
                "server_error",
                "resolve_failed",
            )

        body = serialize.resolution_payload(resolution)
        body["nodes"] = _quant_node_check(ctx, resolution)
        return JSONResponse(body)

    @router.get("/api/models/variants")
    async def model_variants(model_id: str = "", context: int = 8192,
                             concurrency: int = 1) -> Response:
        """Every obtainable quantization of a model, and which one to pick.

        The verdicts come from the same fit gate a launch goes through, fed each
        variant's own dtype and its *measured* on-disk size rather than a table
        estimate -- for a GGUF file the hub reports real bytes, and an Unsloth
        Dynamic mix has no fixed bits-per-weight for a table to hold.

        The recommendation is deliberately narrow: the largest variant that both
        fits and can actually be served here. Recommending the biggest thing
        that fits would keep pointing at GGUF files nothing in this cluster can
        load.
        """
        from control_plane.resolver.types import (
            MetadataUnavailable,
            ModelNotFound,
            UnsupportedArchitecture,
        )

        model_id = (model_id or "").strip()
        if not model_id:
            return errors.error_response(
                400, "model_id is required.", "invalid_request_error", "invalid_request"
            )

        enumerate_variants = getattr(ctx.deps.resolver, "quant_variants", None)
        if not callable(enumerate_variants):
            return errors.error_response(
                501,
                "This resolver cannot enumerate quantizations.",
                "server_error",
                "resolver_lacks_variants",
            )

        cached = _variant_cache.get(model_id)
        if cached is None:
            try:
                variants = await asyncio.wait_for(
                    asyncio.to_thread(enumerate_variants, model_id),
                    timeout=_VARIANTS_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                return errors.error_response(
                    504,
                    f"Enumerating quantizations of {model_id!r} took longer than "
                    f"{_VARIANTS_TIMEOUT_S:.0f}s.",
                    "server_error",
                    "variants_timeout",
                )
            except ModelNotFound as exc:
                return errors.error_response(
                    404, str(exc), "invalid_request_error", "model_not_found"
                )
            except (MetadataUnavailable, UnsupportedArchitecture) as exc:
                return errors.error_response(
                    502, str(exc), "server_error", "metadata_unavailable"
                )
            except Exception as exc:
                log.exception("variant enumeration failed for %s", model_id)
                return errors.error_response(
                    502,
                    f"Could not list quantizations of {model_id!r}: "
                    f"{type(exc).__name__}.",
                    "server_error",
                    "variants_failed",
                )
            _variant_cache.put(model_id, variants)
        else:
            variants = cached

        rows = await asyncio.to_thread(
            _variant_verdicts, ctx, model_id, variants, context, concurrency
        )
        recommended = _recommend(rows)
        return JSONResponse(
            {
                "model_id": model_id,
                "variants": rows,
                "recommended": recommended,
                # Not decoration. available_quants' own docstring: names are the
                # only signal most quantizers leave, so this is a shortlist to
                # offer a person, not a promise that each one loads.
                "heuristic": True,
                "note": (
                    "Names are the only signal most quantizers leave, so this "
                    "is a shortlist to offer a user, not a promise that each "
                    "one loads."
                ),
                "from_cache": cached is not None,
            }
        )

    @router.get("/api/capacity")
    async def capacity(
        context: int = 8192,
        concurrency: int = 1,
        kv_dtype: str = "",
        basis: str = "both",
    ) -> Response:
        """The largest model that runs here, under the live budget and under
        the static ceiling, so the difference between them is visible."""
        from control_plane.fit.capacity import largest_runnable
        from control_plane.fit.catalog import CURATED_MODELS
        from . import livefit

        profiles = _profiles()
        # A node reporting no addressable memory at all cannot hold anything,
        # and leaving it in makes every min() zero -- which would report a
        # static budget of 0.0 GiB beside rows that say "fits".
        usable_profiles = [p for p in profiles if p.addressable_memory > 0]
        skipped = [
            {
                "node_id": p.node_id,
                "reason": "reports 0 addressable bytes and was excluded "
                "from the capacity probe",
            }
            for p in profiles
            if p.addressable_memory <= 0
        ]
        if not usable_profiles:
            return errors.error_response(
                503,
                "No healthy node reports any addressable memory, so there is "
                "nothing to measure capacity against.",
                "server_error",
                "no_nodes",
            )
        # Probe the largest node: capacity is "what is the biggest thing this
        # cluster can run", and answering it against the smallest machine
        # would understate the cluster for no reason.
        profiles = sorted(
            usable_profiles, key=lambda p: p.addressable_memory, reverse=True
        )

        kv = kv_dtype or ctx.settings.default_kv_dtype
        budgets, excluded, unavailable = livefit.allocatable_map(
            ctx.deps.registry, [p.node_id for p in profiles]
        )
        budgets, zero_excluded = livefit.drop_zero_addressable(profiles, budgets)
        excluded = excluded + zero_excluded + skipped

        # Resolve first, once, and report what could not be resolved rather
        # than dropping it silently from an answer that claims completeness.
        shapes: list = []
        unresolved: list[dict] = []
        for model in list(CURATED_MODELS)[:MAX_CATALOG_MODELS]:
            try:
                resolved = await asyncio.to_thread(
                    _resolve, ctx, model.model_id
                )
            except Exception as exc:
                unresolved.append(
                    {
                        "model_id": model.model_id,
                        "reason": errors.detail(exc),
                    }
                )
                continue
            shape, weight_bytes = resolved
            shapes.append((shape, model.label, weight_bytes))

        out: dict[str, Any] = {
            "probed_node": profiles[0].node_id,
            "context": context,
            "concurrency": concurrency,
            "kv_dtype": kv,
            "nodes": [p.node_id for p in profiles],
            "measured_at": time.time(),
            "excluded": excluded,
            "unresolved": unresolved,
            "unavailable_reason": unavailable,
        }

        if basis in ("live", "both") and budgets:
            rows, best = await asyncio.to_thread(
                largest_runnable, shapes, profiles,
                context=context, max_seqs=concurrency, kv_dtype=kv,
                allocatable=budgets,
            )
            out["live"] = {
                "allocatable_per_node": budgets.get(
                    profiles[0].node_id, min(budgets.values())
                ),
                "rows": [_row_payload(r) for r in rows],
                "best": _row_payload(best) if best else None,
            }
        elif basis in ("live", "both"):
            out["live"] = None

        if basis in ("static", "both"):
            rows, best = await asyncio.to_thread(
                largest_runnable, shapes, profiles,
                context=context, max_seqs=concurrency, kv_dtype=kv,
                allocatable=None,
            )
            out["static"] = {
                "usable_per_node": profiles[0].usable_memory(DEFAULT_GUARDRAIL),
                "rows": [_row_payload(r) for r in rows],
                "best": _row_payload(best) if best else None,
            }

        return JSONResponse(out)

    return router




class _TTLCache:
    """A tiny time-bounded memo. Not an LRU: the working set here is however
    many models one person looks at in ten minutes."""

    def __init__(self, ttl_s: float, limit: int = 64) -> None:
        self._ttl = ttl_s
        self._limit = limit
        self._items: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        hit = self._items.get(key)
        if hit is None:
            return None
        stored_at, value = hit
        if time.monotonic() - stored_at > self._ttl:
            self._items.pop(key, None)
            return None
        return value

    def put(self, key: str, value: Any) -> None:
        if len(self._items) >= self._limit:
            oldest = min(self._items, key=lambda k: self._items[k][0])
            self._items.pop(oldest, None)
        self._items[key] = (time.monotonic(), value)


_variant_cache = _TTLCache(_VARIANTS_TTL_S)
_search_cache = _TTLCache(_SEARCH_TTL_S, limit=128)


def _variant_verdicts(
    ctx: GatewayContext, model_id: str, variants: list, context: int, concurrency: int
) -> list[dict]:
    """Ask the real fit gate about each variant, at its own dtype and size.

    Runs through ``largest_runnable`` with an empty ladder, so each variant is
    checked as it actually ships rather than being walked down to something
    else -- the ladder here *is* the variant list, and re-quantizing a row
    would answer a question nobody asked.
    """
    import dataclasses

    from control_plane.contracts.quant import QUANT_INFO
    from control_plane.fit.capacity import largest_runnable

    from . import livefit

    try:
        base_shape, _ = _resolve(ctx, model_id)
    except Exception:
        log.exception("could not resolve %s for variant verdicts", model_id)
        base_shape = None

    profiles = [
        p for p in _profiles_of(ctx) if p.addressable_memory > 0
    ]
    profiles.sort(key=lambda p: p.addressable_memory, reverse=True)
    allocatable = None
    if profiles:
        try:
            allocatable, _, _ = livefit.allocatable_map(
                ctx.deps.registry, [p.node_id for p in profiles]
            )
        except Exception:
            log.exception("live budget unavailable; falling back to the ceiling")

    verdicts: dict[str, Any] = {}
    if base_shape is not None and profiles:
        shapes = []
        for index, variant in enumerate(variants):
            shapes.append(
                (
                    dataclasses.replace(base_shape, dtype=variant.dtype),
                    str(index),
                    variant.file_bytes,
                )
            )
        try:
            rows, _ = largest_runnable(
                shapes,
                profiles,
                context=context,
                max_seqs=concurrency,
                kv_dtype=ctx.settings.default_kv_dtype,
                allocatable=allocatable,
                ladder=(),
            )
            verdicts = {row.label: row for row in rows}
        except Exception:
            log.exception("capacity walk failed for %s variants", model_id)

    out: list[dict] = []
    for index, variant in enumerate(variants):
        row = verdicts.get(str(index))
        info = QUANT_INFO.get(variant.dtype)
        out.append(
            {
                "dtype": variant.dtype,
                # As published. Rendering "q4_k_m" for a file called
                # "UD-Q4_K_XL" would paraphrase a name somebody else chose.
                "label": variant.label,
                "repo_id": variant.repo_id,
                "source": variant.source,
                "gguf_file": variant.gguf_file,
                # How many files that filename stands for. One name and a
                # summed size otherwise disagree about what is being described.
                "shard_count": getattr(variant, "shard_count", 1),
                "shard_files": list(getattr(variant, "shard_files", ()) or ()),
                # Measured or absent. Never an estimate dressed as a size.
                "file_bytes": variant.file_bytes,
                "downloads": variant.downloads,
                "launchable": variant.launchable,
                "note": variant.note,
                "bits_per_weight": info.bits_per_weight if info else None,
                "family": info.family if info else None,
                "verdict": row.verdict if row else None,
                "fits": row.fits if row else None,
                "headroom": row.headroom if row else None,
                "reason": row.reason if row else "",
                "predicted_decode_tps": row.predicted_decode_tps if row else None,
            }
        )
    return _ranked(out)


#: Fit tiers, best first. ``Verdict`` has exactly three and the ordering
#: question needs exactly three; a fourth bucket catches the rows the fit gate
#: could not judge at all, which belong last rather than anywhere flattering.
_TIER = {"fits": 0, "fits_degraded": 1, "wont_fit": 2}
_UNJUDGED_TIER = 3


def _rank_key(row: dict) -> tuple:
    """Fit first, then the best quality inside that tier.

    Sorting by size alone puts the smallest, most damaged quantization at the
    top of a list whose whole purpose is to say what you should run. Sorting by
    quality alone puts a 60 GB file above the 17 GB one that actually fits.
    Fit is the outer key and size is the inner one, so the first row is the
    largest thing that runs -- which is why this list needs no "recommended"
    badge to be readable.

    The refusals invert: among things that do not fit, the *smallest* is the
    interesting one, because it is the near miss. Ordering those biggest-first
    would bury the only row worth a second look under the ones that were never
    close.
    """
    tier = _TIER.get(row.get("verdict") or "", _UNJUDGED_TIER)
    size = row.get("file_bytes") or 0
    bpw = row.get("bits_per_weight") or 0.0
    # Four publishers ship the same quantization of the same model at byte-
    # identical sizes. Without a total tiebreak their order is whatever the hub
    # listed them in, and the table reshuffles under the reader between one
    # refetch and the next.
    identity = (row.get("label") or "", row.get("repo_id") or "", row.get("gguf_file") or "")
    if tier == _TIER["wont_fit"]:
        return (tier, size, bpw, *identity)
    return (tier, -size, -bpw, *identity)


def _ranked(rows: list[dict]) -> list[dict]:
    """Order the variants and stamp each with its position.

    The number is sent rather than the order alone so the browser sorts by an
    integer instead of reimplementing this, which is the rule everywhere else
    on this wire: a second implementation of a judgement is a second answer,
    and the one that disagrees with the fit gate is the one that misleads.
    """
    rows.sort(key=_rank_key)
    for position, row in enumerate(rows):
        row["rank"] = position
    return rows


def _recommend(rows: list[dict]) -> dict | None:
    """The largest variant that both fits and can be served here.

    Two filters, and the second is the one that matters. Picking the biggest
    thing that fits would recommend a GGUF file on every Unsloth repository,
    and nothing in this cluster can load one -- a recommendation you cannot act
    on is worse than none. Size is the quality proxy: among variants of one
    model, more bytes is less lossy.

    It reads the same order the list is drawn in rather than re-deriving a
    maximum, so the recommendation cannot disagree with the row sitting at the
    top of the table.
    """
    usable = [r for r in rows if r.get("fits") and r.get("launchable")]
    if not usable:
        return None
    best = min(usable, key=_rank_key)
    return {
        "repo_id": best["repo_id"],
        "label": best["label"],
        "dtype": best["dtype"],
        "reason": best.get("reason") or "",
    }


def _profiles_of(ctx: GatewayContext) -> list:
    try:
        return [n.profile for n in ctx.deps.registry.healthy_nodes()]
    except Exception:
        log.exception("registry unavailable")
        return []


def _quant_node_check(ctx: GatewayContext, resolution: Any) -> dict:
    """Can the nodes we actually have run this scheme?

    Built from ``QuantRequirement.check`` on the resolution itself rather than
    by importing ``resolver.support.check_nodes``: nothing under
    ``control_plane/gateway/`` imports ``control_plane.resolver``, and the
    verdict is one method call on an object the resolution already carries.

    An "emulated" pass is collected as a problem too. It is not a refusal, but
    on pre-Blackwell silicon a runtime that emulates MXFP4 upcasts the weights
    to bf16 and quadruples them, which the fit check was not told about.
    """
    support = getattr(resolution, "support", None)
    if support is None:
        return {"ok": True, "problems": [], "checked": 0}
    requirement = support.quant
    problems: list[str] = []
    ok = True
    checked = 0
    try:
        states = ctx.deps.registry.healthy_nodes()
    except Exception:
        log.exception("registry unavailable during quant node check")
        states = []
    for state in states:
        profile = state.profile
        checked += 1
        passed, reason = requirement.check(profile.compute_capability)
        if not passed:
            ok = False
            problems.append(f"{profile.node_id} ({profile.gpu_name}): {reason}")
        elif "emulated" in reason:
            problems.append(f"{profile.node_id} ({profile.gpu_name}): {reason}")
    return {"ok": ok, "problems": problems, "checked": checked}


def _resolve(ctx: GatewayContext, model_id: str):
    """Shape plus measured weight bytes, preferring resolve_full when the port
    has it -- the same preference internal_api's planning path applies."""
    full = getattr(ctx.deps.resolver, "resolve_full", None)
    if callable(full):
        resolution = full(model_id, None)
        return resolution.shape, resolution.effective_weight_bytes()
    return ctx.deps.resolver.resolve(model_id, None), None


def _row_payload(row) -> dict:
    return {
        "model_id": row.model_id,
        "label": row.label,
        "total_params": row.total_params,
        "native_dtype": row.native_dtype,
        "dtype": row.dtype,
        "requantized": row.requantized,
        "verdict": row.verdict,
        "fits": row.fits,
        "total": row.total,
        "headroom": row.headroom,
        "predicted_decode_tps": row.predicted_decode_tps,
        "reason": row.reason,
        "warnings": row.warnings,
    }
