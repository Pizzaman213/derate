"""Runtimes already running on a node, and the one click that adopts one.

A machine with no GPU cannot carry a rank. The Models tab already says so, and
says the remedy in a sentence: run Ollama on it, add it under Settings ->
Providers, and it becomes a target. That sentence is correct and it is three
manual steps, two of which are facts the coordinator already holds -- the
node's address, and that Ollama listens on 11434. Only the first, installing
the runtime, genuinely needs a human on that machine.

So this closes the other two. ``GET /api/nodes/{id}/runtime`` reports what is
listening; ``POST`` adopts it as a provider using the address the registry
already probed, rather than one somebody retyped.

Registered above the ``StaticFiles`` mount, like every other ``/api`` router
here, or the SPA answers ``index.html`` to a fetch expecting JSON.

Two positions worth stating, because both are refusals of something easier:

**Detection never registers on its own.** Finding a runtime produces a
suggestion. This is the same stance ``registry.offer_candidate`` takes about a
discovered node -- discovery proposes, a human accepts -- and it matters more
here, not less: a provider is a routing target, and one that appeared without
anybody choosing it is a request going somewhere nobody meant.

**Only nodes in the roster are probed.** There is no subnet scan. Every address
comes from a member a human already admitted, so this reaches nothing the
coordinator could not already reach.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from ..providers.detect import detect_runtime, set_resident
from ..providers.service import existing_provider_for
from . import errors

log = logging.getLogger(__name__)


def create_router(ctx) -> APIRouter:
    router = APIRouter()

    def _node(node_id: str):
        try:
            return ctx.deps.registry.get_node(node_id)
        except Exception:
            log.exception("registry unavailable")
            return None

    def _client():
        """The registry's HTTP client, or None on a registry that has no such
        thing. Duck-typed for the same reason every other read here is: a stub
        registry is a valid deployment of this surface, and it simply has
        nothing to probe with."""
        return getattr(ctx.deps.registry, "_client", None)

    def _existing_provider(base_url: str):
        """A provider already registered against this base_url, or None."""
        try:
            providers = ctx.deps.providers.list()
        except Exception:
            return None
        return existing_provider_for(providers, base_url)

    @router.get("/api/nodes/{node_id}/runtime")
    async def get_runtime(node_id: str) -> Response:
        """What is listening on this node that we could route to.

        ``detected: null`` is a normal answer and the common one. It is not an
        error and does not mean the node is unhealthy -- most machines are not
        running a model runtime, and a GPU node has no reason to.
        """
        state = _node(node_id)
        if state is None:
            return errors.error_response(
                404,
                f"No node '{node_id}'.",
                "invalid_request_error",
                "node_not_found",
            )
        client = _client()
        if client is None:
            return JSONResponse(
                {
                    "node_id": node_id,
                    "detected": None,
                    "reason": (
                        "This registry cannot probe a node, so nothing here is "
                        "a measurement and nothing is reported."
                    ),
                }
            )
        found = await detect_runtime(state.profile.address, client)
        if found is None:
            return JSONResponse({"node_id": node_id, "detected": None, "reason": None})
        existing = _existing_provider(found["base_url"])
        return JSONResponse(
            {
                "node_id": node_id,
                "detected": found,
                # Already adopted, so the UI offers nothing and says which
                # provider it is instead of a button that would duplicate it.
                "provider_id": getattr(existing, "provider_id", None) if existing else None,
                "reason": None,
            }
        )

    @router.post("/api/nodes/{node_id}/runtime")
    async def adopt_runtime(node_id: str) -> Response:
        """Adopt the runtime on this node as a provider. The one click.

        Takes no body on purpose. Every field a provider needs here is already
        known to the coordinator -- the kind from what answered, the base_url
        from the address the registry probed -- and accepting them from the
        caller would let a click register a target pointing somewhere else.
        """
        state = _node(node_id)
        if state is None:
            return errors.error_response(
                404, f"No node '{node_id}'.", "invalid_request_error", "node_not_found"
            )
        client = _client()
        if client is None:
            return errors.error_response(
                503,
                "This registry cannot probe a node, so there is nothing to adopt.",
                "server_error",
                "probe_unavailable",
            )
        found = await detect_runtime(state.profile.address, client)
        if found is None:
            return errors.error_response(
                404,
                f"Nothing is listening on {state.profile.address} that this "
                "build knows how to route to. Start the runtime on that "
                "machine and try again.",
                "invalid_request_error",
                "no_runtime",
            )
        existing = _existing_provider(found["base_url"])
        if existing is not None:
            # Idempotent: clicking twice is one provider, not two.
            return JSONResponse(
                {
                    "node_id": node_id,
                    "provider_id": existing.provider_id,
                    "created": False,
                }
            )
        # Named for the machine, not the product. Several nodes can run the
        # same kind, and "ollama" three times over is a list nobody can act on.
        provider_id = f"{node_id}-{found['kind']}"
        try:
            # add_async, not add: this route is already async, and add_async
            # pulls the model list as part of registering -- so a provider that
            # answered the probe but cannot serve a catalogue fails here, while
            # somebody is still looking at it, rather than silently later.
            provider = await ctx.deps.providers.add_async(
                {
                    "provider_id": provider_id,
                    "kind": found["kind"],
                    "base_url": found["base_url"],
                }
            )
        except Exception as exc:
            log.exception("could not register a provider for %s", node_id)
            return errors.error_response(
                400,
                f"Could not add {found['kind']} on {node_id} as a provider: {exc}",
                "invalid_request_error",
                "provider_rejected",
            )
        return JSONResponse(
            {
                "node_id": node_id,
                "provider_id": getattr(provider, "provider_id", provider_id),
                "created": True,
            },
            status_code=201,
        )

    @router.post("/api/nodes/{node_id}/runtime/model")
    async def set_model_resident(node_id: str, request: Request) -> Response:
        """Load a model into memory on this node's runtime, or evict it.

        The distinction this exposes is the one that matters on a small
        machine: a model on disk costs nothing, and the same model resident
        costs half the RAM of a Raspberry Pi. The roster already reports what
        is free; this is the control over what is spending it.

        Deliberately not a launch in the cluster sense. Nothing is placed, no
        rank is assigned and the fit gate is not consulted, because none of
        those apply to a machine derate does not schedule onto. It asks the
        runtime to hold a model it already has.
        """
        state = _node(node_id)
        if state is None:
            return errors.error_response(
                404, f"No node '{node_id}'.", "invalid_request_error", "node_not_found"
            )
        client = _client()
        if client is None:
            return errors.error_response(
                503,
                "This registry cannot reach a node, so nothing can be loaded.",
                "server_error",
                "probe_unavailable",
            )
        try:
            body = await request.json()
        except Exception:
            return errors.error_response(
                400, "Body must be JSON.", "invalid_request_error", "invalid_json"
            )
        model = str(body.get("model") or "").strip()
        if not model:
            return errors.error_response(
                400,
                "Name the model to load or unload, as the runtime names it.",
                "invalid_request_error",
                "model_required",
            )
        if "resident" not in body:
            return errors.error_response(
                400,
                "Say whether the model should be resident: true to load it, "
                "false to evict it.",
                "invalid_request_error",
                "resident_required",
            )
        resident = bool(body["resident"])
        try:
            reply = await set_resident(state.profile.address, model, resident, client)
        except Exception as exc:
            log.exception("could not change residency of %s on %s", model, node_id)
            return errors.error_response(
                502,
                f"The runtime on {node_id} would not "
                f"{'load' if resident else 'unload'} {model}: {exc}",
                "server_error",
                "runtime_refused",
            )
        return JSONResponse(
            {
                "node_id": node_id,
                "model": model,
                "resident": resident,
                # The runtime's own word for what it did. "load" and "unload"
                # are Ollama's; passed through rather than restated, so a
                # runtime that did something else is not described as if it
                # had complied.
                "done_reason": (reply or {}).get("done_reason"),
            }
        )

    return router
