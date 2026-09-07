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

from . import errors
from .deps import GatewayContext
from .settings_store import (
    MUTABLE_FIELDS,
    _coerce as _coerce_public,
    SettingsError,
    SettingsStore,
    resolve,
)

log = logging.getLogger("gateway.ui_api")


def _accounts_for_spend(providers: Any) -> bool:
    """Whether any port can tell us what has been spent.

    A cap the coordinator cannot measure against is a control that appears to
    guard money and does not. Better to refuse it and say why.
    """
    return callable(getattr(providers, "public_list", None)) or callable(
        getattr(providers, "spend_today", None)
    )


def create_router(ctx: GatewayContext, store: SettingsStore | None = None) -> APIRouter:
    router = APIRouter()
    settings = ctx.settings
    store = store or SettingsStore()

    def _env_values() -> dict[str, Any]:
        """What the environment asked for.

        Only the electricity rate has an env var today; the other two are new
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
            patch = await request.json()
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

        return JSONResponse(_view())

    return router
