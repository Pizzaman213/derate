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

import asyncio
import logging

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, Response

from control_plane.auth.service import AdminExists
from control_plane.contracts.auth import AuditAction, InvalidCredentials, LoginLocked
from control_plane.version import build_id

from . import authz, errors, serialize, setup_state
from .auth_api import admin_dict, bad_request, json_object, set_session_cookie, string_fields
from .deps import GatewayContext

log = logging.getLogger("gateway.setup")

#: The default key minted at setup. A year: long enough to wire a first client
#: in without it lapsing, and one of the expiries every key is limited to.
DEFAULT_KEY_NAME = "default"
DEFAULT_KEY_EXPIRY_DAYS = 365


def create_router(ctx: GatewayContext) -> APIRouter:
    router = APIRouter()
    store = setup_state.SetupStore()

    def _create_first_admin(auth, username: str, email: str, password: str, context: dict):
        # Two first-admin posts racing both pass the guard's admin_exists();
        # create_first_admin decides inside one BEGIN IMMEDIATE, so only one
        # of them -- in this process or any other on the same auth.db -- wins.
        try:
            return auth.create_first_admin(username, password, email=email, **context)
        except AdminExists:
            return None

    @router.post("/api/setup/admin")
    async def setup_admin(request: Request) -> Response:
        """The first admin, and with it everything needed to use the cluster.

        Open only while no admin exists (``authz.AuthGuard`` answers 410
        ``setup_closed`` after). Creates the admin, their personal admin key
        (whose secret is discarded -- UI chat runs as it), and a normal key
        named ``default`` in the default org, and signs the admin in. The
        default key's secret comes back once, as ``secret_once``, here and
        nowhere else.
        """
        body = await json_object(request)
        if isinstance(body, Response):
            return body
        fields = string_fields(body, "username", "email", "password")
        if isinstance(fields, Response):
            return fields
        username, email, password = fields
        stores = ctx.auth_stores
        auth, keys = stores.auth, stores.keys
        context = authz.request_context(request)
        try:
            admin = await asyncio.to_thread(
                _create_first_admin, auth, username, email, password, context
            )
        except ValueError as exc:
            return bad_request(str(exc))
        if admin is None:
            return authz.setup_closed()
        org = keys.get_org(keys.default_org_id())
        admin_key = authz.mint_admin_key(stores, admin.username)
        minted = keys.create_key(
            name=DEFAULT_KEY_NAME,
            org_id=org.org_id,
            created_by=admin.username,
            expiry_days=DEFAULT_KEY_EXPIRY_DAYS,
        )
        for key in (admin_key, minted.key):
            authz.audit_request(
                request,
                AuditAction.KEY_CREATED,
                "key",
                key.key_id,
                after=authz.key_dict(key),
                actor=admin.username,
            )
        payload = {
            "admin": admin_dict(admin),
            "org": authz.org_dict(org),
            "key": authz.key_dict(minted.key),
            "secret_once": minted.secret_once,
        }
        try:
            session, cookie = await asyncio.to_thread(
                auth.login, admin.username, password, **context
            )
        except (LoginLocked, InvalidCredentials):
            # The admin and both keys are committed and the secret exists
            # nowhere else: answer it, unsigned in. The wizard says "sign in".
            return JSONResponse({**payload, "signed_in": False})
        response = JSONResponse({**payload, "signed_in": True})
        set_session_cookie(response, request, cookie)
        return response

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
    async def setup_complete(request: Request) -> JSONResponse:
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
            # The OSError text names paths on this machine, so it goes to
            # node.log and the answer is a fixed sentence.
            log.warning("could not record setup completion: %s", exc)
            return errors.error_response(
                500,
                "Setup finished, but this coordinator could not record that it "
                "did; check that the data directory is writable (details in node.log).",
                "setup_error",
                "setup_not_recorded",
            )
        authz.audit_request(
            request, AuditAction.SETTINGS_CHANGED, "setup", "complete",
            after={"setup_completed": True},
        )
        return JSONResponse({"completed": True})

    return router
