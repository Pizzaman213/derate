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
from dataclasses import replace
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

#: How many explicitly-named models one `/api/capacity?models=` may answer.
#:
#: Deliberately not `MAX_CATALOG_MODELS`, which bounds a different question:
#: that one caps a fixed curated walk, this one caps a set a client chose. The
#: merged model list asks for about ten at a time, so eight would push two rows
#: into an "unresolved" sentence on every single call.
MAX_CAPACITY_MODELS = 12

#: Resolves run concurrently, but not unboundedly: `HubClient` shares one
#: `requests.Session`, and an unauthenticated hub answers a wide fan-out with a
#: 429 -- which would turn "which of these fit" into "the hub said no" for the
#: whole screen. Four sits inside urllib3's default pool of ten, so no
#: connection is discarded, and twelve ids clear in three waves.
_RESOLVE_FANOUT = 4

#: One deadline for a whole batch, not per model. `_DETAIL_TIMEOUT_S` is one
#: model's budget; three waves of that would be 36s behind a polling client.
#: Crucially this DEGRADES: resolves that finished still produce rows, and the
#: stragglers are reported. A whole-request 504 would throw away verdicts
#: already in hand -- and with no HF_TOKEN, one permanently-gated id would take
#: every other answer down with it on every call.
_CAPACITY_TIMEOUT_S = 15.0

#: Resolutions, memoised. `ShapeCache` already caches on disk for 24h, but a
#: disk hit still deserialises JSON and rebuilds a Resolution per model per
#: poll; this makes the hot path allocation-only.
_RESOLVE_TTL_S = 600.0

#: Resolutions that FAILED. The load-bearing one: `ShapeCache` never caches a
#: failure, so today every call pays a fresh hub round trip for a repo that
#: will never stop failing -- `meta-llama/*` with no HF_TOKEN is permanent, and
#: it is walked on every capacity request.
#:
#: Much shorter than the positive TTL, on purpose. A failure is a thing an
#: operator FIXES: sets a token, accepts a licence, corrects a typo. A screen
#: still repeating a refusal that has already been dealt with is a worse bug
#: than a few extra hub calls. Timeouts are not stored at all -- a hub that was
#: slow once is not a hub that is broken.
_RESOLVE_FAIL_TTL_S = 120.0


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

    @router.get("/api/models/speculative-heads")
    async def speculative_heads(
        model_id: str = "", limit: int = 12, refresh: int = 0
    ) -> Response:
        """Every way this model can speculate, priced and ranked, plus a pick.

        Deliberately NOT folded into `/api/plan`. That route answers on every
        keystroke in the Serve panel, and a cold scan here is a dozen hub
        searches plus a resolve per candidate -- 39 of them for Qwen3-30B-A3B.

        Nothing here launches anything, so it answers on a box with no free
        memory at all -- which is the half of speculative decoding that stays
        useful when the cluster is full.

        **Answered once per model, not once per view.** The scan is written to
        `head_scan.save_scan` keyed by the model AND the image version, so the
        second view is free and the control can offer a recommendation without
        being asked for one. `?refresh=1` goes past the record; `from_cache`
        and `scanned_at` say which happened.

        **The checkpoint's own options are in the same ranking as the hub's.**
        `mtp` declared by `num_nextn_predict_layers` and a separately-published
        EAGLE3 head are alternatives to each other, and two lists would make
        the reader compare them by hand.

        **`recommended_head` is not the top row, and cannot be.** The ranking
        is a ceiling, ngram wins it by construction -- see
        `head_scan.WEIGHTLESS_NOTE` -- and within a method family it ties. The
        pick applies `head_scan.recommend`; `caveat` keeps saying what the
        ranking does and does not mean.
        """
        wanted = (model_id or "").strip()
        if not wanted:
            return errors.error_response(
                400, "model_id is required.", "invalid_request_error",
                "invalid_request",
            )
        top = max(1, min(int(limit), 50))
        # Keyed by the limit too. It truncates the payload, so one entry served
        # for every limit hands a caller asking for 40 whatever the caller
        # asking for 6 got.
        memo = f"{wanted}|{top}"
        if not refresh:
            cached = _heads_cache.get(memo)
            if cached is not None:
                return JSONResponse(cached)

        resolver = ctx.deps.resolver
        if not callable(getattr(resolver, "search_models", None)) or not callable(
            getattr(resolver, "resolve_full", None)
        ):
            return JSONResponse({
                "model_id": wanted, "heads": [], "recommended": [], "rejected": [],
                "note": "this resolver cannot search the hub for draft heads",
            })

        def work() -> dict:
            from control_plane import head_scan, measurements
            from control_plane.resolver import support

            base = resolver.resolve_full(wanted)
            probed = support.probed("vllm")
            specs = getattr(probed, "speculators", None) if probed else None
            version = getattr(probed, "version", "") if probed else ""

            hit = None if refresh else head_scan.cached_scan(wanted, version or "")
            if hit is not None:
                usable, rejected, scanned_at = hit
                from_cache = True
            else:
                rows = head_scan.candidates(resolver, wanted)
                usable, rejected = head_scan.price(
                    resolver, base.shape, rows,
                    frozenset(specs) if specs else None,
                    target_model_type=getattr(base, "model_type", "") or "",
                )
                scanned_at = time.time()
                head_scan.save_scan(
                    wanted, version or "", usable, rejected, scanned_at
                )
                from_cache = False

            # The checkpoint's own options, ranked beside the hub's. `model_id`
            # is the TARGET for these -- for `mtp` the draft ships inside the
            # checkpoint, so naming the head means naming the model itself, and
            # `declared_by` is what tells a reader where it came from.
            #
            # Unlaunchable ones are left out rather than listed as rejections:
            # checkpoint DSpark is detected and refused with its own sentence,
            # and the picker below already shows it saying so. A ranking row
            # with no ceiling would be a third place to say the same thing.
            builtin = [
                ({"model_id": wanted, "downloads": None, "builtin": True}, option)
                for option in (getattr(base, "speculators", ()) or ())
                if option.launchable
            ]

            gpu, bandwidth = None, 0.0
            try:
                for node in ctx.deps.registry.healthy_nodes():
                    if node.profile.memory_bandwidth_gbps:
                        gpu = getattr(node.profile, "gpu_name", None)
                        bandwidth = node.profile.memory_bandwidth_gbps
                        break
            except Exception:
                gpu, bandwidth = None, 0.0

            baseline, scored = head_scan.rank(
                usable + builtin, base.shape, bandwidth or 273.0
            )

            # What a sweep actually got, per method, on THIS hardware under
            # THIS image. `matching` does the keying and a mismatch misses --
            # a measurement does not travel between unlike machines.
            measured: dict[str, list] = {}
            for method in {option.method.value for _, _, option in scored}:
                try:
                    measured[method] = measurements.matching(
                        wanted, method, gpu_name=gpu,
                        memory_bandwidth_gbps=bandwidth or None,
                        runtime_version=version or None,
                    )
                except Exception:
                    measured[method] = []
            best_measured = {
                method: max(r.best_tps for r in rows)
                for method, rows in measured.items() if rows
            }

            pick = head_scan.recommend(scored, best_measured)
            # Keyed by (model id, method): a built-in and a hub head can carry
            # the same repository -- `mtp` names the target -- and an id alone
            # would mark both when only one was chosen.
            picked = {(r[1]["model_id"], r[2].method.value)
                      for r in head_scan.shortlist(
                          [s for s in scored if (s[2].draft_params or 0) > 0],
                          4)}
            chosen = (pick[1]["model_id"], pick[2].method.value) if pick else None

            def row_of(ceiling, row, option) -> dict:
                return {
                    "model_id": row["model_id"],
                    "method": option.method.value,
                    "max_tokens": option.max_tokens,
                    "default_tokens": option.default_tokens,
                    "draft_bytes": option.draft_bytes,
                    "draft_params": option.draft_params,
                    "source": option.source,
                    "declared_by": option.declared_by,
                    "ceiling_tps": ceiling,
                    "downloads": row.get("downloads"),
                    "recommended": (row["model_id"], option.method.value) in picked,
                    "note": option.note,
                    "measured": [
                        serialize.measurement(r)
                        for r in measured.get(option.method.value, ())
                    ],
                }

            # The recommendations ALWAYS survive the limit. Truncating first
            # dropped the dflash and dspark picks off a `limit=6` answer and
            # left one lonely asterisk on an all-EAGLE3 list -- the one thing
            # this endpoint exists to show.
            shown = scored[:top] + [
                s for s in scored[top:]
                if (s[1]["model_id"], s[2].method.value) in picked
            ]
            weightless = [s for s in scored if (s[2].draft_params or 0) == 0]
            return {
                "model_id": wanted,
                "baseline_tps": baseline,
                "scanned_at": scanned_at,
                "from_cache": from_cache,
                "heads": [row_of(*s) for s in shown],
                # The single pick, as a whole row rather than an id: the screen
                # renders it beside a checkbox before anything is selected, so
                # it needs the method, the cost and the token count too.
                "recommended_head": row_of(*pick) if pick else None,
                # One per method family, best-established first. Separate from
                # `heads` because the top of a ranking and the thing to try are
                # not the same: the ceiling ties within a family, so the pick
                # is by downloads and will often not be the top row.
                "recommended": [
                    s[1]["model_id"] for s in scored
                    if (s[1]["model_id"], s[2].method.value) in picked
                ],
                "rejected": [
                    {"model_id": row.get("model_id", ""), "reason": str(why)}
                    for row, why in rejected[:20]
                ],
                "rejected_total": len(rejected),
                # Only when there IS one to explain away. Printed unconditionally
                # it would tell somebody about a row that is not on their screen.
                "ngram_note": head_scan.WEIGHTLESS_NOTE if weightless else "",
                "caveat": (
                    "Ranked by ceiling \u2014 every drafted token accepted. That "
                    "rewards a small head with a high k, and within a method "
                    "family it barely separates anything. Which of these is "
                    "actually fastest depends on the acceptance rate, which is "
                    "only knowable by measuring."
                ),
            }

        try:
            payload = await asyncio.wait_for(
                asyncio.to_thread(work), timeout=_HEADS_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            return JSONResponse({
                "model_id": wanted, "heads": [], "recommended": [], "rejected": [],
                "note": f"the hub did not answer within {_HEADS_TIMEOUT_S:.0f}s",
            })
        except Exception as exc:
            log.exception("scanning heads for %s failed", wanted)
            return errors.error_response(
                502, str(exc), "server_error", "head_scan_failed",
            )
        _heads_cache.put(memo, payload)
        return JSONResponse(payload)

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
    async def model_variants(
        model_id: str = "",
        context: int | None = None,
        concurrency: int | None = None,
        on: str = "",
    ) -> Response:
        """Every obtainable quantization of a model, and which one to pick.

        The verdicts come from the same fit gate a launch goes through, fed each
        variant's own dtype and its *measured* on-disk size rather than a table
        estimate -- for a GGUF file the hub reports real bytes, and an Unsloth
        Dynamic mix has no fixed bits-per-weight for a table to hold.

        The recommendation is deliberately narrow: the largest variant that both
        fits and can actually be served here. Recommending the biggest thing
        that fits would keep pointing at GGUF files nothing in this cluster can
        load.

        ``on=`` names the machines to size against -- the same comma-separated
        spelling the UI keeps in `?on=`. Absent, the coordinator's own host.
        ``context=`` and ``concurrency=`` are optional, and absent means "the
        gate chooses", per variant row.
        """
        max_seqs = concurrency if concurrency and concurrency > 0 else 1
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

        def _fail(status: int, message: str, type_: str, code: str, *, store: bool):
            """One exit for every enumeration failure.

            ``store`` is what separates a failure worth remembering from one
            that is already cheap to re-ask. A replayed failure says so, and
            carries `Retry-After`, so a client that offers a retry can tell the
            difference between "tried again and it still fails" and a button
            that appeared to do nothing.
            """
            if store:
                _variant_fail_cache.put(model_id, (status, message, type_, code))
            return errors.error_response(
                status, message, type_, code,
                headers={"Retry-After": f"{_VARIANT_FAIL_TTL_S:.0f}"} if store else None,
                from_cache=False,
            )

        # A failure inside the window is replayed rather than re-run. Checked
        # before the success cache only because the two are disjoint: a model
        # never has both.
        failed = _variant_fail_cache.get(model_id)
        if failed is not None:
            status, message, type_, code = failed
            return errors.error_response(
                status, message, type_, code,
                headers={"Retry-After": f"{_VARIANT_FAIL_TTL_S:.0f}"},
                from_cache=True,
            )

        cached = _variant_cache.get(model_id)
        if cached is None:
            try:
                variants = await asyncio.wait_for(
                    asyncio.to_thread(enumerate_variants, model_id),
                    timeout=_VARIANTS_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                return _fail(
                    504,
                    f"Enumerating quantizations of {model_id!r} took longer than "
                    f"{_VARIANTS_TIMEOUT_S:.0f}s.",
                    "server_error",
                    "variants_timeout",
                    store=True,
                )
            except ModelNotFound as exc:
                # Not stored: this fails on the first listing and is already
                # cheap, and a repo that appears on the hub should show up on
                # the next click rather than a minute later.
                return _fail(
                    404, str(exc), "invalid_request_error", "model_not_found",
                    store=False,
                )
            except (MetadataUnavailable, UnsupportedArchitecture) as exc:
                return _fail(
                    502, str(exc), "server_error", "metadata_unavailable",
                    store=False,
                )
            except Exception as exc:
                log.exception("variant enumeration failed for %s", model_id)
                return _fail(
                    502,
                    f"Could not list quantizations of {model_id!r}: "
                    f"{type(exc).__name__}.",
                    "server_error",
                    "variants_failed",
                    store=True,
                )
            _variant_cache.put(model_id, variants)
        else:
            variants = cached

        rows, sized_on = await asyncio.to_thread(
            _variant_verdicts, ctx, model_id, variants, context, max_seqs, on
        )
        recommended = _recommend(rows)
        return JSONResponse(
            {
                "model_id": model_id,
                "variants": rows,
                "recommended": recommended,
                # What the rows above were sized against: which machines, at
                # what degree, on which budget. On the wire so the screen can
                # state it instead of apologising for not knowing it.
                "sized_on": sized_on,
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
        context: int | None = None,
        concurrency: int | None = None,
        kv_dtype: str = "",
        basis: str = "both",
        models: str = "",
        on: str = "",
    ) -> Response:
        """The largest model that runs here, under the live budget and under
        the static ceiling, so the difference between them is visible.

        ``models=`` names a set explicitly and REPLACES the curated walk, so a
        client browsing search results can get a verdict for a model nobody
        curated. It replaces rather than extends because the caller already has
        `/api/catalog` and can ask for both in one request; appending four
        curated resolves to every keystroke-driven call would triple the hub
        cost of a list that is mostly cache hits.

        ``on=`` scopes the answer to named machines -- the same comma-separated
        spelling the UI keeps in `?on=`, so the verdict a person is shown is
        taken on the machines they ticked rather than on one this endpoint
        chose for them.

        ``context=`` and ``concurrency=`` are now OPTIONAL, and their absence
        is a different question from a value: absent means "choose one per
        model", which is what makes this endpoint answerable on a fresh install
        with nothing configured. An explicit value is still honoured verbatim.
        Each row therefore reports the numbers it was judged at, because with a
        derived context one number at the top no longer describes every row.

        The payload is the same either way -- same keys, same `unresolved[]`,
        same two sides -- because a second shape is a second thing to keep in
        step with the fit gate, which is this project's one stated failure mode.
        """
        from control_plane.fit.capacity import largest_runnable
        from control_plane.fit.catalog import CURATED_MODELS
        from . import livefit

        max_seqs = concurrency if concurrency and concurrency > 0 else 1
        profiles = _profiles()
        wanted_ids, unknown_ids = _parse_node_scope(on, profiles)
        if wanted_ids is not None:
            profiles = [p for p in profiles if p.node_id in wanted_ids]

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

        # No machine here has a GPU budget. That used to be a 503, on the
        # reasoning that there was nothing to measure against -- but on a
        # single GPU-less coordinator, which is what a fresh install on a Pi or
        # a laptop is, that made every model row on the screen permanently
        # "not checked" and the models screen useless. Answer from the live
        # host reading instead, and say plainly what the budget is.
        host_basis = False
        host_budget: dict[str, int] = {}
        if not usable_profiles:
            host_budget = _host_budget(ctx, profiles)
            if host_budget:
                host_basis = True
                usable_profiles = [p for p in profiles if p.node_id in host_budget]
                skipped = [e for e in skipped if e["node_id"] not in host_budget]

        if not usable_profiles:
            return errors.error_response(
                503,
                "No enrolled machine reports any memory to measure against -- "
                "neither GPU-addressable memory nor a host reading. Join a "
                "node, or add a remote provider and serve from there.",
                "server_error",
                "no_nodes",
            )

        profiles = _order_profiles(ctx, usable_profiles)
        if wanted_ids is None:
            # Nothing was named, so this is a question about ONE machine --
            # the coordinator's own host. Sharding across whatever happens to
            # be enrolled would answer "what could this cluster run if it were
            # replanned around this model", which is the planner's question and
            # needs a target and a link measurement. `on=` is how somebody asks
            # the other one.
            profiles = profiles[:1]

        kv = kv_dtype or ctx.settings.default_kv_dtype
        if host_basis:
            # The live budget IS the host reading here; asking the registry for
            # an allocatable figure would answer about a GPU that is not there.
            budgets = dict(host_budget)
            excluded, unavailable = [], None
        else:
            budgets, excluded, unavailable = livefit.allocatable_map(
                ctx.deps.registry, [p.node_id for p in profiles]
            )
            budgets, zero_excluded = livefit.drop_unbudgetable(profiles, budgets)
            excluded = excluded + zero_excluded
        excluded = excluded + skipped + unknown_ids

        # Resolve first, once, and report what could not be resolved rather
        # than dropping it silently from an answer that claims completeness.
        #
        # A named id is its own label. Prettifying it here -- `id.split("/")[-1]`
        # or similar -- would be a second naming rule competing with the one the
        # client already applies to its own rows.
        if models.strip():
            named, over_cap = _parse_models(models)
            wanted = [(model_id, model_id) for model_id in named]
        else:
            wanted = [
                (m.model_id, m.label) for m in list(CURATED_MODELS)[:MAX_CATALOG_MODELS]
            ]
            over_cap = []

        shapes, unresolved, natives = await _resolve_many(ctx, wanted)
        unresolved.extend(over_cap)

        plan_for = _plan_for(ctx, profiles)
        # The degree is a property of the model, so it is only knowable per
        # row. Reported as the set actually used rather than as the set asked
        # for: a client that ticked three machines and got TP=1 has to be able
        # to say so instead of implying a three-way split that never happened.
        tp_used = sorted(
            {plan_for(shape).tensor_parallel for shape, _l, _w in shapes}
        ) if plan_for and shapes else [1]

        # The caveat that rides every row under a host basis. Its second half
        # used to be "Nothing here can be launched on this machine -- serve it
        # from a provider", which was the honest containment while every
        # runtime needed a GPU: sizing against host RAM would otherwise print
        # "fits" for a model that could not load there at all.
        #
        # A CPU runtime changes which half is true. The budget is still host
        # memory and still worth saying -- it is a live reading of a pool the
        # operating system shares, not a ceiling -- but "nothing can be
        # launched" is now false, and a caveat that tells somebody to go
        # elsewhere when the machine in front of them can serve is worse than
        # no caveat at all.
        host_note = (
            (
                f"sized against the host memory {profiles[0].node_id} reports, "
                f"not against GPU memory: no GPU was found on it. Serve these "
                f"on llamacpp, which runs on the CPU and reads GGUF builds."
            )
            if _serves_from_host_memory()
            else (
                f"sized against the host memory {profiles[0].node_id} reports, "
                f"not against GPU memory: no GPU was found on it. Nothing here "
                f"can be launched on this machine -- serve it from a provider."
            )
        ) if host_basis else None

        out: dict[str, Any] = {
            "probed_node": profiles[0].node_id,
            # Null when the fit gate chose per model. The client must read the
            # number off each row in that case; one number here would describe
            # whichever row happened to be first.
            "context": context,
            "concurrency": max_seqs,
            "kv_dtype": kv,
            "nodes": [p.node_id for p in profiles],
            "tensor_parallel": tp_used,
            "budget_basis": "host_memory" if host_basis else "gpu",
            "local_serving": not host_basis or _serves_from_host_memory(),
            "measured_at": time.time(),
            "excluded": excluded,
            "unresolved": unresolved,
            "unavailable_reason": unavailable,
        }

        async def _side(allocatable):
            rows, best = await asyncio.to_thread(
                largest_runnable, shapes, profiles,
                context=context, max_seqs=max_seqs, kv_dtype=kv,
                allocatable=allocatable, plan_for=plan_for,
                native_context=natives,
            )
            return {
                "rows": [_row_payload(r, host_note) for r in rows],
                "best": _row_payload(best, host_note) if best else None,
            }

        if basis in ("live", "both") and budgets:
            side = await _side(budgets)
            side["allocatable_per_node"] = budgets.get(
                profiles[0].node_id, min(budgets.values())
            )
            out["live"] = side
        elif basis in ("live", "both"):
            out["live"] = None

        if basis in ("static", "both"):
            if host_basis:
                # There is no static side to report. The static ceiling is
                # derived from addressable memory, which is 0 here on purpose,
                # so every figure on this side would be 0.0 GiB beside rows
                # that say "fits" -- the exact confusion the zero-addressable
                # filter above exists to prevent.
                out["static"] = None
            else:
                side = await _side(None)
                side["usable_per_node"] = profiles[0].usable_memory(
                    DEFAULT_GUARDRAIL
                )
                out["static"] = side

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

    def clear(self) -> None:
        """Drop everything. These caches are module-global, so without this a
        test that warms one changes the answer a later test gets."""
        self._items.clear()


_variant_cache = _TTLCache(_VARIANTS_TTL_S)
_search_cache = _TTLCache(_SEARCH_TTL_S, limit=128)

#: Head scans are far more expensive than a search -- a resolve per
#: candidate -- and the answer moves only when somebody publishes a new
#: head, so it is cached for much longer and asked for far less often.
_HEADS_TTL_S = 30 * 60.0
_HEADS_TIMEOUT_S = 60.0
_heads_cache = _TTLCache(_HEADS_TTL_S, limit=64)
#: Enumerations that FAILED, so a retry is cheap.
#:
#: ``_variant_cache`` is only ever written on success, which meant a model
#: whose enumeration timed out paid the full 20s on every retry, for ever --
#: and a person watching a spinner fail clicks again. Short-lived on purpose:
#: a failure is a thing an operator fixes (sets HF_TOKEN, accepts a licence),
#: and a screen still repeating a refusal that has been dealt with is a worse
#: bug than a few extra hub calls. `ModelNotFound` is deliberately NOT stored
#: -- it fails on the first listing and is already cheap.
_VARIANT_FAIL_TTL_S = 60.0
_variant_fail_cache = _TTLCache(_VARIANT_FAIL_TTL_S, limit=64)


def _unsized(variant) -> bool:
    """A row this gate must not judge: launchable, and never measured.

    ``file_bytes`` is documented below as measured or absent, never an
    estimate dressed as a size -- and that was honest about the SIZE and
    silent about the VERDICT computed when it is absent. Handed ``None``,
    ``weight_bytes_per_rank`` falls back to ``total_params`` times the
    dtype's table figure, and the row goes out with a full-confidence
    verdict, a headroom and a Serve button.

    Measured on ``Qwen/Qwen3.8-Flash-Next``: an assumed 4.5 bits/weight
    predicted 47.3 GiB per rank where the checkpoint really holds 111.8.
    2.4x, in the direction that turns a refusal into an invitation --
    62 GiB of claimed headroom that was really 4.1 GiB of overflow.

    ``resolver.quant_variants`` now sizes these before they get here, so
    this is the residue: a gated repository, a hub 429, a repo that
    publishes no weight index at all. The rule is the one
    ``speculators.py`` already applies to a draft head whose shards cannot
    be counted -- name it and refuse it, because derate does not budget
    what it cannot measure.

    A GGUF row is never launchable and is priced from its own measured
    files, so this never fires on one.
    """
    return bool(variant.launchable) and variant.file_bytes is None


def _unsized_reason(variant) -> str:
    return (
        f"{variant.repo_id} publishes no readable weight index, so its size "
        f"could only be estimated from its config -- and for a memory gate "
        f"that estimate is a guess in the direction that invites a launch. "
        f"derate does not budget what it cannot measure. Check the "
        f"repository is public and HF_TOKEN is set if it is gated."
    )


def _variant_verdicts(
    ctx: GatewayContext,
    model_id: str,
    variants: list,
    context: int | None,
    concurrency: int,
    on: str = "",
) -> tuple[list[dict], dict]:
    """(one row per variant, what the rows were sized against).

    Ask the real fit gate about each variant, at its own dtype and size. Runs
    through ``largest_runnable`` with an empty ladder, so each variant is
    checked as it actually ships rather than being walked down to something
    else -- the ladder here *is* the variant list, and re-quantizing a row
    would answer a question nobody asked.

    The second return value is the whole point of this signature. These rows
    used to be sized on one machine, silently, while the board above them let
    somebody tick several -- and the screen apologised for the gap in prose
    instead of closing it. ``on=`` closes it: the rows are sized on the
    machines that were named, at the widest degree the model legally admits
    across them, and the basis travels back so the caption can state it as a
    fact rather than hedge.
    """
    import dataclasses

    from control_plane.contracts.quant import QUANT_INFO
    from control_plane.fit.capacity import (
        _single_node_plan as _single_ladder_plan,
        ladder_context,
        largest_runnable,
    )

    def _quality(dtype: str) -> float:
        """Bits per weight, for ordering the variants best-first.

        The ladder arrives in the hub's order and is ranked for display later
        by fit. The context has to be derived before any of that, so it needs
        its own ordering, and quality is the one that matters: the derivation
        takes the best scheme that still leaves a workable window.
        """
        info = QUANT_INFO.get(dtype)
        return info.bits_per_weight if info else 0.0

    from . import livefit

    try:
        base_shape, _, base_window = _resolve(ctx, model_id)
    except Exception:
        log.exception("could not resolve %s for variant verdicts", model_id)
        base_shape = None
        base_window = None

    all_profiles = _profiles_of(ctx)
    wanted_ids, _unknown = _parse_node_scope(on, all_profiles)
    if wanted_ids is not None:
        all_profiles = [p for p in all_profiles if p.node_id in wanted_ids]

    profiles = [p for p in all_profiles if p.addressable_memory > 0]
    # Same fallback as `/api/capacity`, for the same reason: on a machine with
    # no GPU the ladder is the only thing that can say how big these files are
    # and whether the box could hold them at all, and a blank column says
    # nothing. `local_serving` below says whether a Serve button may be drawn
    # from it -- which used to be "no, this was budgeted against host memory"
    # and is now a question for the runtime table. See
    # `_serves_from_host_memory`.
    host_basis = False
    if not profiles:
        host_budget = _host_budget(ctx, all_profiles)
        if host_budget:
            host_basis = True
            profiles = [p for p in all_profiles if p.node_id in host_budget]

    profiles = _order_profiles(ctx, profiles)
    # Every GPU node, unless somebody named a subset. NOT the `[:1]` clamp that
    # `/api/capacity` keeps: that endpoint answers "what can this machine run",
    # and widening it there would silently turn it into the planner's question.
    # This ladder answers "which published variant of THIS model should I pull",
    # which is a question about the cluster the operator actually has -- a
    # two-Spark roster answered as one Spark reports refusals for variants the
    # pair holds at TP=2. The degree still comes from `_plan_for`, i.e. from
    # `valid_tp_degrees`, so a model that only splits one way is judged on one
    # machine however many are enrolled.
    allocatable = None
    if profiles and host_basis:
        allocatable = _host_budget(ctx, profiles)
    elif profiles:
        try:
            allocatable, _, _ = livefit.allocatable_map(
                ctx.deps.registry, [p.node_id for p in profiles]
            )
        except Exception:
            log.exception("live budget unavailable; falling back to the ceiling")

    plan_for = _plan_for(ctx, profiles)
    basis = {
        # Filled in from the PLACEMENT below, once there is one. Empty until
        # then, and empty is the honest answer when the model could not be
        # resolved: no walk happened, every row reports "not checked", and
        # naming machines beside that would describe a probe nobody ran. It
        # also keeps the invariant the caption relies on -- the node list and
        # the degree always describe the same placement -- which the widened
        # default would otherwise break by listing the whole roster at TP=1.
        "nodes": [],
        "probed_node": None,
        "budget_basis": "host_memory" if host_basis else "gpu",
        "local_serving": bool(profiles)
        and (not host_basis or _serves_from_host_memory()),
        "tensor_parallel": 1,
        # The two denominators, so the caption can name the number the verdicts
        # were taken against instead of leaving the reader to assume it was the
        # nameplate. Filled in below against the PLACEMENT, not the candidate
        # set, and both are the binding (smallest) figure across it -- which is
        # what `FitCalculator._budget` actually gates on.
        "allocatable_per_node": None,
        "usable_per_node": None,
        "budget_is_live": bool(allocatable),
    }

    verdicts: dict[str, Any] = {}
    # The same rows judged against the hardware's own ceiling instead of what
    # is free this second. Empty when there is no second question to ask: no
    # live budget (the one walk already IS the static one), or `host_basis`.
    static_verdicts: dict[str, Any] = {}
    if base_shape is not None and profiles:
        # One placement for the whole ladder: every row is the same model at a
        # different precision, and the legal degrees come from the head counts,
        # which no quantization changes.
        placement = (
            plan_for(base_shape)
            if plan_for is not None
            else _single_ladder_plan([profiles[0].node_id])
        )
        # The placement, not the candidate set. Somebody who ticks three
        # machines for a model that only splits two ways has two machines under
        # it, and reporting all three would restate the mismatch this parameter
        # exists to remove.
        basis["tensor_parallel"] = placement.tensor_parallel
        basis["nodes"] = list(placement.node_ids)
        basis["probed_node"] = placement.node_ids[0]
        placed = [p for p in profiles if p.node_id in set(placement.node_ids)]
        if allocatable:
            budgets = [
                allocatable[p.node_id]
                for p in placed
                if p.node_id in allocatable
            ]
            if budgets:
                basis["allocatable_per_node"] = min(budgets)
        if placed and not host_basis:
            basis["usable_per_node"] = min(
                p.usable_memory(DEFAULT_GUARDRAIL) for p in placed
            )
        shapes = []
        for index, variant in enumerate(variants):
            if _unsized(variant):
                # Never judged, so never judged wrongly. See _unsized.
                continue
            shapes.append(
                (
                    dataclasses.replace(base_shape, dtype=variant.dtype),
                    str(index),
                    variant.file_bytes,
                )
            )
        # ONE context for the whole list, when the caller named none.
        #
        # `largest_runnable` is called with an empty ladder here -- each
        # variant is checked as it actually ships, never walked down to
        # something else -- so its own per-model derivation would fire once per
        # ROW and hand every row a different context. That is four questions
        # printed as one table: "fits at 31744" beside "fits at 32256", with
        # headroom figures underneath that cannot be read against each other.
        # Derive once, over the same variants in quality order, and hold it
        # fixed down the list, which is what makes the column comparable.
        row_context = context
        if row_context is None:
            rungs = sorted(
                (
                    (shape, str(index), weight_bytes)
                    for index, (shape, _label, weight_bytes) in enumerate(shapes)
                ),
                key=lambda rung: -_quality(rung[0].dtype),
            )
            row_context, _rung = ladder_context(
                rungs,
                profiles,
                placement,
                max_seqs=concurrency,
                kv_dtype=ctx.settings.default_kv_dtype,
                allocatable=allocatable,
                native_window=base_window,
            )

        def _side(budget):
            """One walk of the whole ladder against one budget.

            Same shape `/api/capacity` uses for its `live`/`static` pair, so
            there are two implementations of "judge these shapes" in this file
            and not three. `row_context` is closed over deliberately: BOTH
            sides are judged at the one context derived above, on the governing
            budget. Deriving it per side would print a live headroom taken at
            one window beside a static headroom taken at another, and the two
            columns could not be read against each other -- the same failure
            `ladder_context` exists to prevent within a single side.
            """
            rows, _ = largest_runnable(
                shapes,
                profiles,
                context=row_context,
                max_seqs=concurrency,
                kv_dtype=ctx.settings.default_kv_dtype,
                allocatable=budget,
                ladder=(),
                plan_for=plan_for,
            )
            return {row.label: row for row in rows}

        try:
            verdicts = _side(allocatable)
        except Exception:
            log.exception("capacity walk failed for %s variants", model_id)

        # The static side, and only where it means something. Under
        # `host_basis` the static ceiling derives from addressable memory,
        # which is 0 on purpose there, so every figure on that side would be
        # "0.0 GiB" printed beside rows that say "fits" -- the exact confusion
        # `/api/capacity` suppresses it for.
        #
        # With no live reading there is no second question: the walk above was
        # ALREADY taken against the ceiling. Mirror it rather than reporting
        # None, so "does the hardware hold this at all" has an answer whenever
        # one exists. Left None, a screen asking that question would render
        # "unknown" over a verdict it was holding.
        if not host_basis:
            if allocatable:
                try:
                    static_verdicts = _side(None)
                except Exception:
                    log.exception("static walk failed for %s variants", model_id)
            else:
                static_verdicts = verdicts

    out: list[dict] = []
    for index, variant in enumerate(variants):
        row = verdicts.get(str(index))
        spec = static_verdicts.get(str(index))
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
                # An unsized row was never handed to the gate, so it has no
                # verdict and every field above stays null. It still owes the
                # reader a sentence, and the sentence is why -- not "not
                # checked", which reads as a spinner that has not landed yet.
                "reason": row.reason if row else (
                    _unsized_reason(variant) if _unsized(variant) else ""
                ),
                "predicted_decode_tps": row.predicted_decode_tps if row else None,
                # What this row was judged at. The caller named no context on
                # the default path, so the gate chose one -- and a verdict
                # whose question is not on screen beside it is not checkable.
                "context": row.context if row else None,
                "max_seqs": row.max_seqs if row else None,
                # The same row against the static ceiling. Additive, and never
                # the governing answer: `verdict`/`fits` above keep meaning
                # exactly what they meant, so nothing that reads them flips.
                # This is here to separate two sentences the screen used to
                # print identically -- "this machine cannot hold it" and "this
                # machine is full right now" -- which want different actions
                # from whoever is reading. None when no static side was walked.
                "static_verdict": spec.verdict if spec else None,
                "static_fits": spec.fits if spec else None,
                "static_headroom": spec.headroom if spec else None,
                "static_reason": spec.reason if spec else (
                    _unsized_reason(variant) if _unsized(variant) else ""
                ),
                "static_predicted_decode_tps": (
                    spec.predicted_decode_tps if spec else None
                ),
            }
        )
    return _ranked(out), basis


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


def _coordinator_id(ctx: GatewayContext) -> str | None:
    """This machine's own node id.

    ``Registry.local_node_id`` is the authoritative field -- it is set from
    the profile the coordinator probed of itself. ``coordinator_node_id``
    on settings is the fallback and only a fallback: ``app.py`` derives it
    from ``list_nodes()[0]``, whose order nothing guarantees, so it names
    the right machine only by luck on a multi-node roster.
    """
    local = getattr(ctx.deps.registry, "local_node_id", None)
    if isinstance(local, str) and local:
        return local
    return ctx.settings.coordinator_node_id


def _order_profiles(ctx: GatewayContext, profiles: list) -> list:
    """The machines to probe, the one the answer is ABOUT first.

    The order matters because ``probed_node``, the live denominator and the
    static denominator are all read off ``profiles[0]``.

    1. the coordinator's own host, when it has a budget. This is the
       machine somebody looking at a fresh install is standing in front of,
       and answering about a different one -- with nothing on screen saying
       which -- is how a verdict becomes untrustworthy.
    2. otherwise the largest by addressable memory, which is what this
       always did: capacity is "what is the biggest thing this cluster can
       run", and answering it against the smallest machine would understate
       the cluster for no reason.

    The rest follow in descending size either way, so a multi-node probe
    still shards onto the roomiest machines it was given.
    """
    ordered = sorted(profiles, key=lambda p: p.addressable_memory, reverse=True)
    local = _coordinator_id(ctx)
    for index, profile in enumerate(ordered):
        if profile.node_id == local:
            return [ordered.pop(index)] + ordered
    return ordered


def _plan_for(ctx: GatewayContext, profiles: list):
    """A per-shape placement across ``profiles``, or None for one machine.

    None means "let the fit gate use its single-node probe", which is the
    answer whenever there is one machine to talk about -- the overwhelming
    majority of requests, and the path that must not grow a planner call.

    The degree comes from the planner port's ``valid_tp_degrees``, not from
    ``len(profiles)``: tensor parallelism has to divide both the query and
    the KV heads, so three machines take a model at TP=1 however many boxes
    somebody ticked. Reporting a fit at a degree the runtime would refuse
    to start at is exactly the class of answer this project does not give.
    """
    node_ids = [p.node_id for p in profiles]
    if len(node_ids) < 2:
        return None
    from control_plane.fit.capacity import probe_plan

    legal = getattr(ctx.deps.planner, "valid_tp_degrees", None)

    def build(shape):
        degrees = {1}
        if callable(legal):
            try:
                degrees = set(legal(shape, len(node_ids))) or {1}
            except Exception:
                log.exception("valid_tp_degrees failed for %s", shape.model_id)
                degrees = {1}
        tp = max(d for d in degrees if 1 <= d <= len(node_ids))
        return probe_plan(node_ids, tp)

    return build


def _serves_from_host_memory() -> bool:
    """Whether any runtime in this build places a model in host RAM.

    `local_serving` used to be `not host_basis`, and that was a correct
    shorthand exactly once: while every runtime needed a GPU, "we had to
    budget against host memory" and "nothing here can serve this" were the
    same statement. The ladder said so on screen -- *"against host memory -- no
    GPU was found there, so nothing below can be served from it"* -- and the
    Serve button was withheld on that basis.

    `llamacpp` places into host RAM deliberately, so the shorthand is now
    false: a GPU-less box budgeted against its own RAM is a box that can
    serve. Asked of the runtime table rather than hardcoded, so a build
    without that runtime -- or with a second one -- answers for itself.

    Lazily imported for the reason every other `deploy.flags` use in this
    package is: nothing under `control_plane/gateway/` may depend on
    `control_plane.deploy` being importable.
    """
    try:
        from control_plane.deploy.flags import RUNTIMES
    except Exception:  # pragma: no cover - deploy not installed
        return False
    return any(spec.memory_pool == "host" for spec in RUNTIMES.values())


def _host_budget(ctx: GatewayContext, profiles: list) -> dict[str, int]:
    """Host memory, for machines the fit gate has no GPU budget for.

    ``NodeProfile.addressable_memory`` is 0 on a machine ``nvidia-smi`` did
    not answer for, and that 0 is deliberate: it is what the fit gate
    budgets against, so RAM no model can reach must not appear in it. That
    rule is kept -- nothing here writes to a profile that outlives the
    request.

    What it does instead is answer the question anyway, from the LIVE host
    reading the node already reports -- ``NodeState.memory_total``, read
    straight off the roster rather than through ``memory_report``, which
    does not carry it. The distinction between that and a static slice of
    host RAM matters: a static ceiling is a fabricated number the planner
    would then place a rank on, whereas this is a measurement, and it is
    reported as a budget the caller is told not to launch against.
    """
    wanted = {p.node_id for p in profiles}
    out: dict[str, int] = {}
    try:
        states = ctx.deps.registry.healthy_nodes()
    except Exception:
        log.exception("registry unavailable")
        return out
    for state in states:
        node_id = state.profile.node_id
        if node_id not in wanted:
            continue
        total = int(getattr(state, "memory_total", 0) or 0)
        if total > 0:
            out[node_id] = total
    return out

def _quant_node_check(ctx: GatewayContext, resolution: Any) -> dict:
    """Can the nodes we actually have run this scheme?

    Built from ``QuantRequirement.check`` on the resolution itself rather than
    by importing ``resolver.support.check_nodes``: nothing under
    ``control_plane/gateway/`` imports ``control_plane.resolver``, and the
    verdict is one method call on an object the resolution already carries.

    An "emulated" pass is collected as a problem too. It is not a refusal, but
    on pre-Blackwell silicon a runtime that emulates MXFP4 upcasts the weights
    to bf16 and quadruples them, which the fit check was not told about.

    Only nodes with a GPU are asked, and here that is the right filter even
    though a CPU node can now serve. ``healthy_nodes()`` filters on liveness
    alone, so it includes machines the probe found no GPU on -- and those
    answer every quantization question with "compute capability '' is
    unreadable", which describes a silicon generation problem on hardware that
    has no silicon to describe. Worse, it set ``ok`` False for a model every
    real node can run.

    The `llamacpp` runtime did not change this. Every question
    ``QuantRequirement.check`` asks is about a CUDA capability -- does this
    silicon have the tensor cores that scheme needs -- and a machine with no
    GPU is not a node that answers "no", it is a node the question is not
    about. What changed is only the sentence: "derate launches CUDA runtimes
    only" was true when it was written and is not now.

    Skipped nodes are reported rather than dropped, for the reason
    ``drop_unbudgetable`` gives: dropping one silently is its own lie.
    ``ok`` keeps its meaning -- "no candidate objected" -- so a cluster with no
    candidates at all leaves it True and lets ``skipped`` carry the story.
    """
    support = getattr(resolution, "support", None)
    if support is None:
        return {"ok": True, "problems": [], "checked": 0}
    requirement = support.quant
    problems: list[str] = []
    skipped: list[dict[str, str]] = []
    ok = True
    checked = 0
    try:
        states = ctx.deps.registry.healthy_nodes()
    except Exception:
        log.exception("registry unavailable during quant node check")
        states = []
    for state in states:
        profile = state.profile
        if profile.addressable_memory <= 0:
            skipped.append(
                {
                    "node_id": profile.node_id,
                    "reason": (
                        "no GPU, so there is no silicon generation to check a "
                        "CUDA quantization scheme against"
                    ),
                }
            )
            continue
        checked += 1
        passed, reason = requirement.check(profile.compute_capability)
        if not passed:
            ok = False
            problems.append(f"{profile.describe()}: {reason}")
        elif "emulated" in reason:
            problems.append(f"{profile.describe()}: {reason}")
    return {"ok": ok, "problems": problems, "checked": checked, "skipped": skipped}


def _resolve(ctx: GatewayContext, model_id: str):
    """(shape, measured weight bytes, the model's own context window).

    Prefers ``resolve_full`` when the port has it -- the same preference
    internal_api's planning path applies.

    The window is the third element because the fit gate now chooses a context
    when the caller names none, and a choice made without knowing the model's
    own limit is a number vLLM would refuse to start with. It lives on
    ``Resolution`` and not on ``ModelShape`` (a frozen contract), so a port
    that only implements ``resolve`` reports None and the caller falls back.
    """
    full = getattr(ctx.deps.resolver, "resolve_full", None)
    if callable(full):
        resolution = full(model_id, None)
        return (
            resolution.shape,
            resolution.effective_weight_bytes(),
            getattr(resolution, "max_position_embeddings", None),
        )
    return ctx.deps.resolver.resolve(model_id, None), None, None


def _parse_node_scope(
    raw: str, profiles: list
) -> tuple[set[str] | None, list[dict]]:
    """``on=`` into (the ids to keep, entries for ids nobody here has).

    None -- not an empty set -- when nothing was named, because "every machine"
    and "no machine" are different questions and collapsing them would silently
    answer the first when somebody asked the second.

    An id that matches no enrolled machine is REPORTED in ``excluded[]`` rather
    than ignored. A stale `?on=` in a bookmarked URL otherwise narrows the
    answer to nothing and looks like a cluster that lost its nodes.
    """
    named = [chunk.strip() for chunk in (raw or "").split(",")]
    named = [n for n in named if n]
    if not named:
        return None, []
    known = {p.node_id for p in profiles}
    unknown = [
        {
            "node_id": node_id,
            "reason": "named in `on=` but no healthy node here has that id",
        }
        for node_id in dict.fromkeys(named)
        if node_id not in known
    ]
    return set(named), unknown


def _parse_models(raw: str) -> tuple[list[str], list[dict]]:
    """``models=`` into (ids to walk, entries for ids we will not walk).

    Case-sensitive de-duplication, preserving first-seen order. Two spellings
    are two ids to the hub, and folding them would answer a question about
    ``qwen/qwen3`` under the row for ``Qwen/Qwen3`` -- an identity inference,
    which is not this endpoint's to make.

    Ids past the cap are REPORTED, never silently dropped: they go back as
    ``unresolved`` entries, which is the field a client already renders as "why
    this row has no verdict". A truncated list that does not say it was
    truncated reads as a complete answer.
    """
    ids: list[str] = []
    seen: set[str] = set()
    for chunk in (raw or "").split(","):
        model_id = chunk.strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        ids.append(model_id)

    over = ids[MAX_CAPACITY_MODELS:]
    return ids[:MAX_CAPACITY_MODELS], [
        {
            "model_id": model_id,
            "reason": (
                f"not checked: this request named {len(ids)} models and the "
                f"capacity probe answers at most {MAX_CAPACITY_MODELS} at a time"
            ),
        }
        for model_id in over
    ]


_resolve_cache = _TTLCache(_RESOLVE_TTL_S, limit=256)
_resolve_fail_cache = _TTLCache(_RESOLVE_FAIL_TTL_S, limit=256)


def _resolve_memo(ctx: GatewayContext, model_id: str):
    """`_resolve`, with both outcomes remembered.

    Raises the stored sentence rather than a stored exception: the sentence is
    what `unresolved[]` carries, and re-raising a pickled cause chain would
    only invite somebody to inspect a type that is no longer live.
    """
    hit = _resolve_cache.get(model_id)
    if hit is not None:
        return hit
    failed = _resolve_fail_cache.get(model_id)
    if failed is not None:
        raise _CachedResolveFailure(failed)
    try:
        resolved = _resolve(ctx, model_id)
    except Exception as exc:
        _resolve_fail_cache.put(model_id, errors.detail(exc))
        raise
    _resolve_cache.put(model_id, resolved)
    return resolved


class _CachedResolveFailure(Exception):
    """A resolve that already failed inside the negative TTL."""


async def _resolve_many(
    ctx: GatewayContext,
    wanted: list[tuple[str, str]],
    *,
    timeout: float = _CAPACITY_TIMEOUT_S,
) -> tuple[list[tuple], list[dict], dict[str, int | None]]:
    """(shapes for `largest_runnable`, unresolved entries, context windows).

    ``wanted`` is (model_id, label). Bounded fan-out, one shared deadline, and
    it degrades: whatever resolved inside the window produces rows, and
    everything else is reported with a sentence. Nothing is ever guessed at and
    nothing is ever dropped -- every id in goes out in exactly one of the two
    lists.

    Returns each model's own context window alongside, keyed by LABEL to match
    what ``largest_runnable`` wants: the variant ladder walks one model under
    several labels, so the model id is not a key there.
    """
    if not wanted:
        return [], [], {}

    gate = asyncio.Semaphore(_RESOLVE_FANOUT)

    async def one(model_id: str):
        async with gate:
            return await asyncio.to_thread(_resolve_memo, ctx, model_id)

    tasks = {asyncio.ensure_future(one(mid)): (mid, label) for mid, label in wanted}
    done, pending = await asyncio.wait(tasks, timeout=timeout)

    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    shapes: list[tuple] = []
    unresolved: list[dict] = []
    natives: dict[str, int | None] = {}
    # Iterate `wanted`, not `done`: the answer's order must be the order that
    # was asked for, whatever order the hub replied in.
    for task, (model_id, label) in tasks.items():
        if task not in done:
            unresolved.append({
                "model_id": model_id,
                "reason": (
                    f"the hub did not answer within {timeout:.0f}s, so this "
                    "model has no verdict"
                ),
            })
            continue
        exc = task.exception()
        if exc is not None:
            reason = (
                exc.args[0]
                if isinstance(exc, _CachedResolveFailure)
                else errors.detail(exc)
            )
            unresolved.append({"model_id": model_id, "reason": reason})
            continue
        shape, weight_bytes, max_positions = task.result()
        # The answer is keyed by the QUESTION. A resolver that canonicalises an
        # id -- follows a hub redirect, folds case -- would otherwise return a
        # row the caller cannot find, and a client joining these rows by the id
        # it asked for would leave that model "checking" for ever. Logged when
        # they differ so a redirect stays observable rather than silently
        # renamed; nothing else about the shape is touched.
        if shape.model_id != model_id:
            log.info(
                "capacity: %r resolved as %r; reporting under the id asked for",
                model_id, shape.model_id,
            )
            shape = replace(shape, model_id=model_id)
        shapes.append((shape, label, weight_bytes))
        natives[label] = max_positions
    return shapes, unresolved, natives


def _row_payload(row, extra_warning: str | None = None) -> dict:
    warnings = list(row.warnings)
    if extra_warning:
        warnings.append(extra_warning)
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
        "warnings": warnings,
        # What this row was judged at. Per row, because the fit gate chooses
        # per model when the caller names no context.
        "context": row.context,
        "max_seqs": row.max_seqs,
    }
