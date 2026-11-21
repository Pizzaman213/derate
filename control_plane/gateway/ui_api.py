"""The mutable-settings surface, on its own router.

A second `APIRouter` rather than more handlers in `internal_api.py`. That file
is 900 lines with 24 endpoints in one closure and several sessions edit it
concurrently; adding to it means holding it. FastAPI composes routers natively
and `app.py` already includes two, so the cost of a separate module is one
`include_router` line and the benefit is that this file is never contended.

**Registration order is load-bearing, twice.** `app.py` mounts the built UI at
`"/"` with `StaticFiles`, and a Starlette mount at the root catches every path
not matched by an *earlier* route -- so this router's `include_router` must sit
physically above that mount line, not merely "before the catch-all" in spirit.
Get it wrong and `/api/settings` quietly returns `index.html`, which surfaces to
the UI as a JSON parse error three layers from the cause.

The settings themselves are the configuration surface §4.5 implied and never
provided: it specifies that COST_AWARE prices local targets against "a
configurable electricity rate", and until now there was nowhere to configure it.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, Response

from ..contracts.auth import AuditAction
from . import errors
from .body import BodyTooLarge, payload_too_large, read_json_object
from .authz import audit_request
from .deps import GatewayContext
from .settings_store import (
    MUTABLE_FIELDS,
    _coerce as _coerce_public,
    SettingsError,
    SettingsStore,
    resolve,
)
from .profiles_store import ProfileError, ProfileNotFound, ProfileStore

log = logging.getLogger("gateway.ui_api")


def _accounts_for_spend(providers: Any) -> bool:
    """Whether any port can tell us what has been spent.

    A cap the coordinator cannot measure against is a control that appears to
    guard money and does not. Better to refuse it and say why. ``spend_today``
    is what ``Router.rebuild`` sums against the cap, so it is the only proof.
    """
    return callable(getattr(providers, "spend_today", None))


def create_router(
    ctx: GatewayContext,
    store: SettingsStore | None = None,
    profiles: ProfileStore | None = None,
) -> APIRouter:
    router = APIRouter()
    settings = ctx.settings
    store = store or SettingsStore()
    profiles = profiles or ctx.profiles or ProfileStore()
    if ctx.profiles is None:
        ctx.profiles = profiles

    def _env_values() -> dict[str, Any]:
        """What the environment asked for.

        Only the electricity rate has an env var today; the other three are new
        and have none. `main.py` owns env reading, so this reads the resolved
        settings object rather than `os.environ` a second time -- one place
        parses the environment, not two.
        """
        out: dict[str, Any] = {}
        rate = getattr(settings, "electricity_rate_usd_per_kwh", None)
        if rate:
            # A zero rate is the code default, not an operator's instruction, so
            # it is left to fall through to "default" and be labelled as such.
            out["electricity_rate_usd_per_kwh"] = float(rate)
        return out

    def _defaults() -> dict[str, Any]:
        return {
            "electricity_rate_usd_per_kwh": 0.0,
            "local_only": False,
            "daily_spend_cap_usd": None,
            "auto_restart_crashed_deployments": True,
        }

    def _view() -> dict[str, Any]:
        resolved = resolve(store.load(), _env_values(), _defaults())
        body: dict[str, Any] = {key: resolved[key].value for key in MUTABLE_FIELDS}
        body["sources"] = {key: resolved[key].source for key in MUTABLE_FIELDS}
        body["writable"] = list(MUTABLE_FIELDS)
        # The honesty gate. The UI renders the cap disabled, with this as the
        # reason, rather than offering a control that silently fails open.
        body["daily_spend_cap_enforceable"] = _accounts_for_spend(ctx.deps.providers)
        return body

    @router.get("/api/settings")
    async def get_settings() -> JSONResponse:
        return JSONResponse(_view())

    @router.patch("/api/settings")
    async def patch_settings(request: Request) -> Response:
        try:
            patch = await read_json_object(request)
        except BodyTooLarge:
            return payload_too_large()
        except Exception:
            return errors.error_response(
                400, "Body must be JSON.", "invalid_request_error", "invalid_json"
            )
        if not isinstance(patch, dict):
            return errors.error_response(
                400,
                "Body must be a JSON object.",
                "invalid_request_error",
                "invalid_request",
            )

        # Validate the VALUES first. A negative cap is malformed whether or not
        # anything could enforce it, and answering 501 "cannot enforce" to a
        # number that was never legal tells the caller the wrong thing to fix.
        try:
            merged = {**store.load(), **{k: _coerce_public(k, v) for k, v in patch.items()}}
        except SettingsError as exc:
            return errors.error_response(
                400, str(exc), "invalid_request_error", "invalid_request"
            )

        # Only then: refuse a cap we cannot enforce, rather than storing it and
        # failing open. 501 with a sentence beats a control that looks armed.
        cap = patch.get("daily_spend_cap_usd", ...)
        if cap not in (..., None) and not _accounts_for_spend(ctx.deps.providers):
            return errors.error_response(
                501,
                "A daily spend cap cannot be enforced: no provider port reports "
                "spend, so there is nothing to measure the cap against.",
                "server_error",
                "not_implemented",
            )

        before_view = _view()
        try:
            store.save(merged)
        except SettingsError as exc:
            return errors.error_response(
                400, str(exc), "invalid_request_error", "invalid_request"
            )
        except OSError as exc:
            log.exception("could not persist settings")
            return errors.error_response(
                502,
                f"Could not persist settings: {type(exc).__name__}.",
                "server_error",
                "settings_write_failed",
            )

        # Apply to the live object so the change takes effect on the next
        # request rather than the next restart. `settings.coordinator_node_id`
        # is already mutated in place at startup, so this is the file's own
        # convention rather than a new one.
        merged = store.load()
        for key, value in merged.items():
            setattr(settings, key, value)

        rebuild = getattr(ctx.router, "rebuild", None)
        if callable(rebuild):
            try:
                rebuild(force_scores=True)
            except Exception:
                log.exception("router rebuild after settings change failed")

        after_view = _view()
        changed = [k for k in MUTABLE_FIELDS if before_view.get(k) != after_view.get(k)]
        audit_request(
            request, AuditAction.SETTINGS_CHANGED, "settings", ",".join(changed) or "-",
            before={k: before_view.get(k) for k in changed},
            after={k: after_view.get(k) for k in changed},
        )
        return JSONResponse(after_view)

    # -- profiles and pins ---------------------------------------------------

    def _bad(message: str) -> Response:
        return errors.error_response(400, message, "invalid_request_error", "invalid_request")

    def _not_found() -> Response:
        return errors.error_response(
            404, "No profile has that id.", "invalid_request_error", "profile_not_found"
        )

    async def _body(request: Request) -> tuple[Any, Response | None]:
        try:
            return await read_json_object(request), None
        except BodyTooLarge:
            return None, payload_too_large()
        except Exception:
            return None, errors.error_response(
                400, "Body must be a JSON object.", "invalid_request_error", "invalid_json"
            )

    def _actor(request: Request) -> str:
        session = request.scope.get("state", {}).get("session")
        return session.username if session is not None else "-"

    @router.get("/api/profiles")
    async def list_profiles(model_id: str | None = None) -> JSONResponse:
        return JSONResponse({"profiles": profiles.list(model_id)})

    @router.post("/api/profiles")
    async def create_profile(request: Request) -> Response:
        body, err = await _body(request)
        if err is not None:
            return err
        try:
            profile = profiles.create(body, actor=_actor(request))
        except ProfileError as exc:
            return _bad(str(exc))
        audit_request(request, AuditAction.PROFILE_CHANGED, "profile",
                      profile["profile_id"], before=None, after=profile)
        return JSONResponse(profile, status_code=201)

    @router.patch("/api/profiles/{profile_id}")
    async def patch_profile(profile_id: str, request: Request) -> Response:
        body, err = await _body(request)
        if err is not None:
            return err
        try:
            before, after = profiles.patch(profile_id, body)
        except ProfileNotFound:
            return _not_found()
        except ProfileError as exc:
            return _bad(str(exc))
        audit_request(request, AuditAction.PROFILE_CHANGED, "profile", profile_id,
                      before=before, after=after)
        return JSONResponse(after)

    @router.delete("/api/profiles/{profile_id}")
    async def delete_profile(profile_id: str, request: Request) -> Response:
        try:
            before = profiles.delete(profile_id)
        except ProfileNotFound:
            return _not_found()
        audit_request(request, AuditAction.PROFILE_CHANGED, "profile", profile_id,
                      before=before, after=None)
        return Response(status_code=204)

    @router.get("/api/pins")
    async def get_pins() -> JSONResponse:
        return JSONResponse({"pins": profiles.pins()})

    @router.put("/api/pins")
    async def put_pins(request: Request) -> Response:
        body, err = await _body(request)
        if err is not None:
            return err
        try:
            before, after = profiles.set_pins(body.get("pins") if isinstance(body, dict) else None)
        except ProfileError as exc:
            return _bad(str(exc))
        audit_request(request, AuditAction.PROFILE_CHANGED, "pins", "pins",
                      before={"pins": before}, after={"pins": after})
        return JSONResponse({"pins": after})

    return router
