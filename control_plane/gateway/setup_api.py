"""First run: what a brand-new coordinator can say about itself.

A fifth ``APIRouter``, for the reason ``capacity_api.py`` and ``enroll_api.py``
both give -- ``internal_api.py`` is already two thousand lines in one closure
that several sessions edit at once, and a new module costs one
``include_router`` line and is never contended.

**Registration must stay ABOVE the ``StaticFiles`` mount in ``app.py``.** A
Starlette mount at ``"/"`` catches every path not matched by an *earlier*
route, so a router included below it answers ``index.html`` to a fetch that
expects JSON -- and this router in particular is the first thing the UI calls
on a fresh install, so getting it wrong turns the whole product into a blank
screen on the one boot where nobody has any context for what went wrong.

**This endpoint computes nothing about models, memory or fit.** The setup
screen it feeds asks ``/api/capacity`` for what will run here and
``POST /api/enroll`` for the join command, which is why neither a predicted
token rate nor an install line appears anywhere below. Those questions already
have an owner and a second answer would drift from the first -- the screen
would then promise a speed the launch path never agreed to. What is genuinely
new here, and the only reason the module exists, is the question *has anyone
set this cluster up yet*, which nothing else could answer.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from starlette.responses import JSONResponse

from control_plane.version import build_id

from . import errors, serialize, setup_state
from .deps import GatewayContext

log = logging.getLogger("gateway.setup")


def create_router(ctx: GatewayContext) -> APIRouter:
    router = APIRouter()
    store = setup_state.SetupStore()

    def _states() -> list:
        """Every node the roster knows, healthy or not.

        `list_nodes` rather than `healthy_nodes`: on a fresh install this
        coordinator has often only just probed itself, and a machine that has
        not yet passed a health check is still the machine the person is
        standing in front of. Reporting "no machine found" to somebody looking
        straight at it is the worst answer available here.
        """
        try:
            return list(ctx.deps.registry.list_nodes())
        except Exception:
            log.exception("registry unavailable")
            return []

    def _this_machine(states: list):
        """The machine the person is standing in front of, or nothing.

        Deliberately stricter than ``capacity_api._coordinator_id``, and not
        reused from it. That helper falls back to
        ``settings.coordinator_node_id``, which ``app.py`` derives from
        ``list_nodes()[0]`` -- an order nothing guarantees. For a capacity
        report, naming the wrong machine of several costs a number that is
        slightly off. Here it costs the whole first screen: setup opens by
        introducing "this machine" and its hardware, and a person who reads
        somebody else's GPU there has been told something false about the box
        under their desk, on the one screen they have no way to check.

        So the authoritative field is used when it is there, a single-node
        roster is unambiguous and needs no field, and anything else reports no
        machine at all. An honest absence, in the house style -- and the
        common first-run case, one machine that just installed itself, is the
        case that always answers.
        """
        local = getattr(ctx.deps.registry, "local_node_id", None)
        if isinstance(local, str) and local:
            return next((s for s in states if s.profile.node_id == local), None)
        if len(states) == 1:
            return states[0]
        return None

    def _counts() -> tuple[int, int]:
        """(deployments, providers). Either port failing counts as zero.

        A port that cannot answer must not be read as "this cluster is
        configured" -- that would suppress the wizard on the exact boot where
        something is already broken. Zero re-offers setup, which is dismissable.
        """
        try:
            deployments = len(ctx.deps.deployments.list())
        except Exception:
            log.exception("deployments unavailable")
            deployments = 0
        try:
            providers = len(ctx.deps.providers.list())
        except Exception:
            log.exception("providers unavailable")
            providers = 0
        return deployments, providers

    def _provider_routing() -> bool:
        """Can a remote provider be added and actually routed to?

        The setup screen renders no cloud step at all when this is False --
        not a disabled row and not an explanation, because a step nobody can
        take is noise on the one screen where every word is being read. The
        kinds table is the honest signal: it is what the add-provider form is
        driven from, so an empty one means there is nothing to add.
        """
        try:
            from control_plane.providers.serialization import kinds_public

            return bool(kinds_public())
        except Exception:
            log.exception("provider kinds unavailable")
            return False

    @router.get("/api/setup")
    async def setup_status() -> JSONResponse:
        """Is this a fresh install, and what is the machine it is running on?"""
        states = _states()
        deployments, providers = _counts()

        verdict = setup_state.is_complete(
            flag=store.load(), deployments=deployments, providers=providers
        )

        match = _this_machine(states)
        # The same row /api/nodes serves, not a subset assembled here.
        # `eligible`, `ineligible_reason` and `build_skew` are computed in one
        # place, and a setup screen that disagreed with the cluster graph about
        # whether this machine can be used would be the more confusing of the
        # two.
        machine = serialize.node_payload(match, None, build_id()) if match else None

        return JSONResponse(
            {
                "completed": verdict.completed,
                "reason": verdict.reason,
                "machine": machine,
                "cluster": {
                    "nodes": len(states),
                    "healthy": sum(1 for s in states if s.healthy),
                },
                "deployments": deployments,
                "providers": providers,
                "provider_routing": _provider_routing(),
            }
        )

    @router.post("/api/setup/complete")
    async def setup_complete() -> JSONResponse:
        """Remember that the wizard was finished, so it is not offered again.

        Deliberately survives a cluster that is still empty afterwards: someone
        who clicked through and chose to add nothing has answered the question,
        and asking it again on the next page load would be the product ignoring
        them. The derived signals in `setup_state.is_complete` cover the other
        direction -- a cluster that gets a model later is set up regardless of
        whether this was ever called.
        """
        try:
            store.mark_complete()
        except OSError as exc:
            # The data directory is the one thing this cannot work around, and
            # it is worth a real error rather than a silent success: a wizard
            # that says "done" and reappears on every reload is a bug report,
            # while one that says it could not write names the actual problem.
            log.warning("could not record setup completion: %s", exc)
            return errors.error_response(
                500,
                "Setup finished, but this coordinator could not record that it "
                "did: %s. Everything you configured is saved; only the "
                "reminder to set up is. Check that the data directory is "
                "writable." % errors.detail(exc),
                "setup_error",
                "setup_not_recorded",
            )
        return JSONResponse({"completed": True})

    return router
