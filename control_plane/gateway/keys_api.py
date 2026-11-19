"""Orgs, API keys, limits, usage, the audit log, local pricing, and the
``POST /api/wiring`` batch (provider and routing changes, validated whole).

Everything here is behind the session gate (``authz.AuthGuard``), so every
handler acts as ``request.state.session.username`` and every mutation is
written to the audit log under that name with a before/after -- the key
service itself audits nothing. No response carries a secret except
``POST /api/keys`` and ``POST /api/keys/{key_id}/rotate``, whose
``secret_once`` is the one-time reveal; ``ApiKey`` has no hash field to leak.

Limit values are checked against the org (a key's limit may not exceed the
org's, a key's allowlist must sit inside the org's) before anything is
written, and the refusal names the field.

**Registration must stay ABOVE the ``StaticFiles`` mount in ``app.py``.**
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, Response

from control_plane.contracts.auth import (
    AllowlistExceedsOrg,
    AuditAction,
    KeyStatus,
    LimitExceedsOrg,
    LimitScope,
)
from control_plane.contracts import RoutingPolicy, TargetKind
from control_plane.providers import UnknownProviderError
from control_plane.telemetry import query as tquery

from . import authz, errors, serialize
from .auth_api import (
    bad_request,
    json_object,
    json_object_or_empty,
    optional_json_object,
    reauthenticated,
)
from .deps import GatewayContext
from .errors import error_response

log = logging.getLogger("gateway.keys")

#: The window a key or org row's usage summary covers.
USAGE_WINDOW_S = 30 * 86400
#: Fields a PATCH may change. Expiry is not one: rotate, or create a new key.
KEY_FIELDS = (
    "name",
    "notes",
    "limits",
    "allowed_models",
    "routing_policy",
    "cloud_fallback_local_model",
)
ORG_FIELDS = (
    "name",
    "allowed_models",
    "limits",
    "default_key_limits",
    "default_key_expiry_days",
    "routing_policy",
)

#: The most ops one ``POST /api/wiring`` may carry.
WIRING_MAX_OPS = 200


class WiringRefusal(Exception):
    """One ``/api/wiring`` op refused in validation: its sentence and code."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def not_found(what: str, ident: str) -> Response:
    return error_response(404, f"No {what} with id '{ident}'.", "invalid_request_error", "not_found")


def _refusal(exc: Exception) -> Response:
    """KeyService's errors as responses: its sentences verbatim."""
    if isinstance(exc, (LimitExceedsOrg, AllowlistExceedsOrg)):
        return error_response(400, exc.message, "invalid_request_error", exc.code.value)
    if isinstance(exc, KeyError):
        return error_response(
            404, f"No such id: {exc.args[0] if exc.args else ''}.", "invalid_request_error", "not_found"
        )
    return bad_request(str(exc))


def _bool(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(value: str | None, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _csv_response(text: str, filename: str) -> Response:
    return Response(
        content=text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def usage_totals(archive, group: str, ids: list[str], start: float, end: float) -> dict[str, dict]:
    """``{id: {requests, tokens, cost_usd}}`` for keys or orgs over a window.

    ``group`` is ``key_id`` or ``org_id``. An id with no traffic maps to zeros
    -- a key that was never used has used nothing, which is a measurement --
    and ``cost_usd`` stays None until a priced request lands.
    """
    filters = {"key_ids": ids} if group == "key_id" else {"org_ids": ids}
    result = tquery.usage(archive, start=start, end=end, bucket="1d", group_by=[group], **filters)
    out = {i: {"requests": 0, "tokens": 0, "cost_usd": None} for i in ids}
    for row in result.get("rows", []):
        ident = row["group"].get(group)
        if ident not in out:
            continue
        acc = out[ident]
        acc["requests"] += row.get("requests") or 0
        acc["tokens"] += (row.get("tokens_in") or 0) + (row.get("tokens_out") or 0)
        cost = row.get("cost_usd")
        if cost is not None:
            acc["cost_usd"] = (acc["cost_usd"] or 0.0) + cost
    return out


def create_router(ctx: GatewayContext) -> APIRouter:
    router = APIRouter()

    def stores() -> authz.AuthStores:
        return ctx.auth_stores

    def archive():
        telemetry = getattr(ctx, "telemetry", None)
        return getattr(telemetry, "archive", None) if telemetry else None

    def exposed() -> list[str]:
        """What /v1/models lists right now: an org with no allowlist allows this."""
        try:
            return list(ctx.router.index().served_names())
        except Exception:
            return []

    def audit(request: Request, action: AuditAction, kind: str, ident: str, *, before=None, after=None):
        authz.audit_request(request, action, kind, ident, before=before, after=after)

    def key_row(key, usage: dict | None) -> dict:
        row = authz.key_dict(key)
        row["limit_states"] = authz.limit_states(stores(), LimitScope.KEY, key.key_id)
        row["usage_30d"] = None if usage is None else usage.get(key.key_id)
        return row

    def org_row(org, usage: dict | None) -> dict:
        row = authz.org_dict(org)
        row["limit_states"] = authz.limit_states(stores(), LimitScope.ORG, org.org_id)
        row["usage_30d"] = None if usage is None else usage.get(org.org_id)
        return row

    async def keyed_usage(group: str, ids: list[str]) -> dict | None:
        if archive() is None:
            return None
        if not ids:
            return {}
        try:
            now = time.time()
            return await asyncio.to_thread(
                usage_totals, archive(), group, ids, now - USAGE_WINDOW_S, now
            )
        except Exception:
            log.exception("usage summary failed")
            return None

    def check_fallback(key_fields: dict, org, allowed: list[str] | None) -> None:
        """``cloud_fallback_local_model`` must be a model this key may use and
        that a LOCAL target serves now -- else it would fall back to a refusal."""
        name = key_fields.get("cloud_fallback_local_model")
        if not name:
            return
        index = ctx.router.index()
        targets = index.targets.get(name) or []
        if not any(t.kind is TargetKind.LOCAL for t in targets):
            raise ValueError(
                f"cloud_fallback_local_model '{name}' is not served by a local deployment "
                "right now; deploy it first or pick another"
            )
        org_list = org.allowed_models if org.allowed_models is not None else exposed()
        effective = [m for m in (allowed if allowed is not None else org_list) if m in set(org_list)]
        if name not in effective:
            raise ValueError(
                f"cloud_fallback_local_model '{name}' is not one of this key's allowed models"
            )

    # -- orgs -----------------------------------------------------------------

    @router.get("/api/orgs")
    async def list_orgs(include_deleted: str = "") -> JSONResponse:
        orgs = stores().keys.list_orgs(include_deleted=bool(_bool(include_deleted)))
        usage = await keyed_usage("org_id", [o.org_id for o in orgs])
        default_id = stores().keys.default_org_id()
        rows = []
        for org in orgs:
            row = org_row(org, usage)
            row["is_default"] = org.org_id == default_id
            rows.append(row)
        return JSONResponse(rows)

    @router.post("/api/orgs")
    async def create_org(request: Request) -> Response:
        body = await json_object(request)
        if isinstance(body, Response):
            return body
        name = body.get("name")
        if not isinstance(name, str):
            return bad_request("'name' is required and must be a string.", "missing_field")
        fields = {k: body[k] for k in ORG_FIELDS if k in body and k != "name"}
        try:
            org = stores().keys.create_org(name, actor=request.state.session.username, **fields)
        except (ValueError, KeyError) as exc:
            return _refusal(exc)
        audit(request, AuditAction.ORG_CREATED, "org", org.org_id, after=authz.org_dict(org))
        return JSONResponse(org_row(org, await keyed_usage("org_id", [org.org_id])))

    @router.get("/api/orgs/{org_id}")
    async def get_org(org_id: str) -> Response:
        org = stores().keys.get_org(org_id)
        if org is None:
            return not_found("org", org_id)
        row = org_row(org, await keyed_usage("org_id", [org_id]))
        row["is_default"] = org_id == stores().keys.default_org_id()
        row["key_count"] = len(stores().keys.list_keys(org_id=org_id))
        return JSONResponse(row)

    @router.patch("/api/orgs/{org_id}")
    async def patch_org(org_id: str, request: Request) -> Response:
        body = await json_object(request)
        if isinstance(body, Response):
            return body
        keys = stores().keys
        before = keys.get_org(org_id)
        if before is None:
            return not_found("org", org_id)
        unknown = sorted(set(body) - set(ORG_FIELDS))
        if unknown:
            return bad_request(
                f"unknown org field(s) {', '.join(unknown)}; editable: {', '.join(ORG_FIELDS)}"
            )
        try:
            after = keys.update_org(org_id, actor=request.state.session.username, **body)
        except (ValueError, KeyError) as exc:
            return _refusal(exc)
        audit(
            request, AuditAction.ORG_UPDATED, "org", org_id,
            before=authz.org_dict(before), after=authz.org_dict(after),
        )
        for field in ("limits", "default_key_limits"):
            if getattr(before, field) != getattr(after, field):
                audit(
                    request, AuditAction.LIMIT_CHANGED, "org", org_id,
                    before={field: dict(getattr(before, field))},
                    after={field: dict(getattr(after, field))},
                )
        return JSONResponse(org_row(after, await keyed_usage("org_id", [org_id])))

    @router.delete("/api/orgs/{org_id}")
    async def delete_org(org_id: str, request: Request) -> Response:
        keys = stores().keys
        before = keys.get_org(org_id)
        if before is None:
            return not_found("org", org_id)
        try:
            keys.delete_org(org_id, actor=request.state.session.username)
        except (ValueError, KeyError) as exc:
            return _refusal(exc)
        audit(request, AuditAction.ORG_DELETED, "org", org_id, before=authz.org_dict(before))
        return JSONResponse({"deleted": org_id})

    @router.get("/api/orgs/{org_id}/limits")
    async def org_limits(org_id: str) -> Response:
        if stores().keys.get_org(org_id) is None:
            return not_found("org", org_id)
        return JSONResponse(authz.limit_states(stores(), LimitScope.ORG, org_id))

    @router.post("/api/orgs/{org_id}/keys/disable")
    async def disable_org_keys(org_id: str, request: Request) -> Response:
        """Containment: disable every active key of the org in one call, admin
        keys included. One ``key_disabled`` row per key, with the reason, so
        each reads exactly like a single disable. Open realtime sessions on
        those keys close within ``realtime.RECHECK_S``."""
        body = await json_object_or_empty(request)
        if isinstance(body, Response):
            return body
        reason = body.get("reason")
        if reason is not None and not isinstance(reason, str):
            return bad_request("'reason' must be a string.")
        keys = stores().keys
        if keys.get_org(org_id) is None:
            return not_found("org", org_id)
        actor = request.state.session.username
        disabled: list[str] = []
        for before in keys.list_keys(org_id=org_id):
            if before.status is not KeyStatus.ACTIVE:
                continue
            try:
                after = keys.disable_key(before.key_id, actor=actor, reason=reason or None)
            except (ValueError, KeyError):
                continue  # deleted between the list and the update
            audit(
                request, AuditAction.KEY_DISABLED, "key", before.key_id,
                before=authz.key_dict(before), after=authz.key_dict(after),
            )
            disabled.append(before.key_id)
        # With nothing to disable no row is written here, and the gate's own
        # ``api_write`` row records that the request was made.
        return JSONResponse({"disabled": disabled})

    # -- keys -----------------------------------------------------------------

    @router.get("/api/keys")
    async def list_keys(
        org_id: str = "",
        status: str = "",
        near_limit: str = "",
        expiring_days: str = "",
        q: str = "",
        include_deleted: str = "",
    ) -> JSONResponse:
        """Keys, filtered. ``near_limit`` keeps keys with any limit near or at;
        ``expiring_days=N`` keeps keys expiring within N days; ``q`` is a
        case-insensitive substring of name, key id or notes."""
        keys = stores().keys.list_keys(
            org_id=org_id or None, include_deleted=bool(_bool(include_deleted))
        )
        if status:
            keys = [k for k in keys if k.status.value == status]
        days = _int(expiring_days)
        if days is not None:
            horizon = time.time() + days * 86400
            keys = [k for k in keys if k.expires_at is not None and k.expires_at <= horizon]
        if q:
            needle = q.lower()
            keys = [
                k
                for k in keys
                if needle in k.name.lower() or needle in k.key_id or needle in (k.notes or "").lower()
            ]
        usage = await keyed_usage("key_id", [k.key_id for k in keys])
        rows = [key_row(k, usage) for k in keys]
        if _bool(near_limit):
            rows = [r for r in rows if any(s["near"] or s["at"] for s in r["limit_states"])]
        return JSONResponse(rows)

    @router.post("/api/keys")
    async def create_key(request: Request) -> Response:
        """A new key. ``secret_once`` is in this response and nowhere else, ever."""
        body = await json_object(request)
        if isinstance(body, Response):
            return body
        keys = stores().keys
        name = body.get("name")
        if not isinstance(name, str):
            return bad_request("'name' is required and must be a string.", "missing_field")
        org_id = body.get("org_id") or keys.default_org_id()
        org = keys.get_org(org_id)
        if org is None or org.deleted_at is not None:
            return not_found("org", str(org_id))
        limits = body.get("limits")
        allowed = body.get("allowed_models")
        try:
            keys.validate_against_org(
                org=org,
                limits=limits if limits is not None else dict(org.default_key_limits),
                allowed_models=allowed,
                exposed=exposed(),
            )
            check_fallback(body, org, allowed)
            minted = keys.create_key(
                name=name,
                org_id=org_id,
                created_by=request.state.session.username,
                expiry_days=body.get("expiry_days"),
                limits=limits,
                allowed_models=allowed,
                routing_policy=body.get("routing_policy"),
                notes=body.get("notes") or "",
                cloud_fallback_local_model=body.get("cloud_fallback_local_model"),
            )
        except (ValueError, KeyError, LimitExceedsOrg, AllowlistExceedsOrg) as exc:
            return _refusal(exc)
        audit(request, AuditAction.KEY_CREATED, "key", minted.key.key_id, after=authz.key_dict(minted.key))
        row = key_row(minted.key, await keyed_usage("key_id", [minted.key.key_id]))
        return JSONResponse({"key": row, "secret_once": minted.secret_once})

    @router.get("/api/keys/{key_id}")
    async def get_key(key_id: str) -> Response:
        """One key, deleted or not -- its stats outlive it."""
        key = stores().keys.get_key(key_id)
        if key is None:
            return not_found("key", key_id)
        row = key_row(key, await keyed_usage("key_id", [key_id]))
        org = stores().keys.get_org(key.org_id)
        row["org_name"] = org.name if org else None
        return JSONResponse(row)

    @router.patch("/api/keys/{key_id}")
    async def patch_key(key_id: str, request: Request) -> Response:
        body = await json_object(request)
        if isinstance(body, Response):
            return body
        keys = stores().keys
        before = keys.get_key(key_id)
        if before is None:
            return not_found("key", key_id)
        unknown = sorted(set(body) - set(KEY_FIELDS))
        if unknown:
            hint = (
                " (expiry cannot change; rotate or create a new key)"
                if {"expires_at", "expiry_days"} & set(unknown)
                else ""
            )
            return bad_request(
                f"unknown key field(s) {', '.join(unknown)}{hint}; editable: {', '.join(KEY_FIELDS)}"
            )
        org = keys.get_org(before.org_id)
        limits = body["limits"] if "limits" in body else before.limits
        allowed = body["allowed_models"] if "allowed_models" in body else before.allowed_models
        try:
            keys.validate_against_org(
                org=org, limits=limits or {}, allowed_models=allowed, exposed=exposed()
            )
            check_fallback(
                {"cloud_fallback_local_model": body.get(
                    "cloud_fallback_local_model", before.cloud_fallback_local_model
                )},
                org,
                allowed,
            )
            after = keys.update_key(key_id, actor=request.state.session.username, **body)
        except (ValueError, KeyError, LimitExceedsOrg, AllowlistExceedsOrg) as exc:
            return _refusal(exc)
        audit(
            request, AuditAction.KEY_UPDATED, "key", key_id,
            before=authz.key_dict(before), after=authz.key_dict(after),
        )
        if before.limits != after.limits:
            audit(
                request, AuditAction.LIMIT_CHANGED, "key", key_id,
                before={"limits": dict(before.limits)}, after={"limits": dict(after.limits)},
            )
        return JSONResponse(key_row(after, await keyed_usage("key_id", [key_id])))

    @router.delete("/api/keys/{key_id}")
    async def delete_key(key_id: str, request: Request) -> Response:
        keys = stores().keys
        before = keys.get_key(key_id)
        if before is None:
            return not_found("key", key_id)
        keys.delete_key(key_id, actor=request.state.session.username)
        after = keys.get_key(key_id)
        audit(
            request, AuditAction.KEY_DELETED, "key", key_id,
            before=authz.key_dict(before), after=authz.key_dict(after) if after else None,
        )
        return JSONResponse({"deleted": key_id})

    async def _key_action(key_id: str, request: Request, action: AuditAction, apply) -> Response:
        keys = stores().keys
        before = keys.get_key(key_id)
        if before is None:
            return not_found("key", key_id)
        try:
            result = apply(keys, request.state.session.username)
        except (ValueError, KeyError) as exc:
            return _refusal(exc)
        after = keys.get_key(key_id)
        audit(
            request, action, "key", key_id,
            before=authz.key_dict(before), after=authz.key_dict(after),
        )
        return result if isinstance(result, Response) else JSONResponse(
            key_row(after, await keyed_usage("key_id", [key_id]))
        )

    @router.post("/api/keys/{key_id}/disable")
    async def disable_key(key_id: str, request: Request) -> Response:
        body = await json_object_or_empty(request)
        if isinstance(body, Response):
            return body
        reason = body.get("reason")
        if reason is not None and not isinstance(reason, str):
            return bad_request("'reason' must be a string.")
        return await _key_action(
            key_id, request, AuditAction.KEY_DISABLED,
            lambda keys, actor: keys.disable_key(key_id, actor=actor, reason=reason or None),
        )

    @router.post("/api/keys/{key_id}/enable")
    async def enable_key(key_id: str, request: Request) -> Response:
        return await _key_action(
            key_id, request, AuditAction.KEY_ENABLED,
            lambda keys, actor: keys.enable_key(key_id, actor=actor),
        )

    @router.post("/api/keys/{key_id}/rotate")
    async def rotate_key(key_id: str, request: Request) -> Response:
        """A new secret for the same key; the old one fails from the next call.
        ``secret_once`` is in this response and nowhere else, ever. An admin
        key needs ``current_password`` in the body: it is that admin's
        credential, so a stolen session alone must not be able to take it."""
        key = stores().keys.get_key(key_id)
        if key is not None and key.is_admin_key:
            body = await optional_json_object(request)
            if isinstance(body, Response):
                return body
            refused = await reauthenticated(request, stores(), body)
            if refused is not None:
                return refused
        minted: list = []

        def apply(keys, actor):
            minted.append(keys.rotate_key(key_id, actor=actor))

        response = await _key_action(key_id, request, AuditAction.KEY_ROTATED, apply)
        if not minted:
            return response
        row = key_row(minted[0].key, await keyed_usage("key_id", [key_id]))
        return JSONResponse({"key": row, "secret_once": minted[0].secret_once})

    @router.post("/api/keys/{key_id}/raise")
    async def raise_limit(key_id: str, request: Request) -> Response:
        """Lift one limit for the rest of this cycle. Counters are untouched;
        the next cycle reads the configured value again."""
        body = await json_object(request)
        if isinstance(body, Response):
            return body
        key = stores().keys.get_key(key_id)
        if key is None:
            return not_found("key", key_id)
        limit_type, value = body.get("limit_type"), body.get("value")
        if not isinstance(limit_type, str) or isinstance(value, bool) or not isinstance(value, (int, float)):
            return bad_request("'limit_type' (string) and 'value' (number) are required.")
        try:
            state = stores().limits.raise_for_cycle(
                LimitScope.KEY, key_id, limit_type, float(value),
                actor=request.state.session.username,
            )
        except (ValueError, KeyError) as exc:
            return _refusal(exc)
        audit(
            request, AuditAction.LIMIT_RAISED, "key", key_id,
            before={"limit_type": limit_type, "limit": key.limits.get(limit_type)},
            after={"limit_type": limit_type, "effective_limit": state.effective_limit,
                   "until": state.resets_at},
        )
        return JSONResponse(authz.jsonable(state))

    @router.post("/api/keys/{key_id}/reset-cycle")
    async def reset_cycle(key_id: str, request: Request) -> Response:
        """Zero this cycle's counters. The schedule does not move."""
        if stores().keys.get_key(key_id) is None:
            return not_found("key", key_id)
        before = authz.limit_states(stores(), LimitScope.KEY, key_id)
        try:
            stores().limits.reset_cycle(LimitScope.KEY, key_id, actor=request.state.session.username)
        except (ValueError, KeyError) as exc:
            return _refusal(exc)
        after = authz.limit_states(stores(), LimitScope.KEY, key_id)
        audit(
            request, AuditAction.CYCLE_RESET, "key", key_id,
            before={"limit_states": before}, after={"limit_states": after},
        )
        return JSONResponse({"key_id": key_id, "limit_states": after})

    @router.get("/api/keys/{key_id}/requests")
    async def key_requests(
        key_id: str,
        start: str = "",
        end: str = "",
        status: str = "",
        model: str = "",
        error: str = "",
        page: int = 1,
        page_size: int = 100,
        format: str = "json",
    ) -> Response:
        if stores().keys.get_key(key_id) is None:
            return not_found("key", key_id)
        arc = archive()
        if arc is None:
            return _no_archive()
        frm, to = tquery.resolve_window(start or "-30d", end or None)
        result = await asyncio.to_thread(
            tquery.requests_list,
            arc,
            key_id=key_id,
            start=frm,
            end=to,
            status=_int(status),
            model=model or None,
            error=error or None,
            page=page,
            page_size=page_size,
        )
        if format == "csv":
            return _csv_response(tquery.requests_csv(result["rows"]), f"{key_id}-requests.csv")
        return JSONResponse(result)

    @router.get("/api/requests")
    async def requests(
        start: str = "",
        end: str = "",
        request_id: str = "",
        key_id: str = "",
        model: str = "",
        status: str = "",
        error: str = "",
        target_id: str = "",
        provider_id: str = "",
        retried: str = "",
        page: int = 1,
        page_size: int = 100,
        format: str = "json",
    ) -> Response:
        """One row per client request, cluster-wide: the final attempt, with
        ``attempts`` the true count."""
        arc = archive()
        if arc is None:
            return _no_archive()
        frm, to = tquery.resolve_window(start or "-30d", end or None)
        status_arg: int | str | None = _int(status)
        if status_arg is None and status in ("ok", "failed"):
            status_arg = status
        result = await asyncio.to_thread(
            tquery.requests_list,
            arc,
            start=frm,
            end=to,
            request_id=request_id or None,
            key_id=key_id or None,
            model=model or None,
            status=status_arg,
            error=error or None,
            target_id=target_id or None,
            provider_id=provider_id or None,
            retried=retried in ("1", "true"),
            page=page,
            page_size=page_size,
        )
        if format == "csv":
            return _csv_response(tquery.requests_csv(result["rows"]), "requests.csv")
        return JSONResponse(result)

    # -- usage, audit ----------------------------------------------------------

    def _no_archive() -> Response:
        telemetry = getattr(ctx, "telemetry", None)
        reason = getattr(telemetry, "reason", "") or "no archive on this node"
        return error_response(
            503,
            f"No telemetry history is being kept: {reason}.",
            "server_error",
            "history_unavailable",
        )

    @router.get("/api/usage")
    async def usage(
        start: str = "",
        end: str = "",
        bucket: str = "1h",
        group_by: str = "",
        key_id: str = "",
        org_id: str = "",
        model: str = "",
        format: str = "json",
    ) -> Response:
        """Usage per bucket and group. ``group_by``, ``key_id``, ``org_id`` and
        ``model`` take comma-separated lists."""
        arc = archive()
        if arc is None:
            return _no_archive()

        def _list(value: str) -> list[str] | None:
            items = [v.strip() for v in value.split(",") if v.strip()]
            return items or None

        frm, to = tquery.resolve_window(start or "-30d", end or None)
        try:
            result = await asyncio.to_thread(
                tquery.usage,
                arc,
                start=frm,
                end=to,
                bucket=bucket,
                group_by=_list(group_by) or [],
                key_ids=_list(key_id),
                org_ids=_list(org_id),
                models=_list(model),
            )
        except ValueError as exc:
            return bad_request(str(exc))
        if format == "csv":
            return _csv_response(tquery.usage_csv(result), "usage.csv")
        return JSONResponse(result)

    @router.get("/api/audit")
    async def audit_log(
        start: str = "",
        end: str = "",
        actor: str = "",
        action: str = "",
        target: str = "",
        page: int = 1,
        page_size: int = 100,
    ) -> Response:
        def _ts(value: str) -> float | None:
            if not value:
                return None
            frm, _ = tquery.resolve_window(value, None)
            return frm

        entries, total = await asyncio.to_thread(
            stores().auth.audit_page,
            start=_ts(start),
            end=_ts(end),
            actor=actor,
            action=action,
            target=target,
            page=max(1, page),
            page_size=max(1, min(page_size, 1000)),
        )
        return JSONResponse(
            {
                "entries": [authz.jsonable(e) for e in entries],
                "total": total,
                "page": max(1, page),
                "page_size": max(1, min(page_size, 1000)),
            }
        )

    @router.get("/api/audit/integrity")
    async def audit_integrity() -> JSONResponse:
        """The audit hash chain, recomputed: ``ok``, ``rows`` checked,
        ``first_bad_id`` (the first row that no longer hashes or links), and
        ``head_hash`` -- compare it with the remote sink's last heartbeat."""
        report = await asyncio.to_thread(stores().auth.verify_chain)
        return JSONResponse(authz.jsonable(report))

    # -- local pricing -----------------------------------------------------------

    @router.get("/api/pricing")
    async def list_pricing() -> JSONResponse:
        return JSONResponse([authz.jsonable(p) for p in stores().pricing.list()])

    @router.get("/api/pricing/{model:path}")
    async def get_pricing(model: str) -> Response:
        price = stores().pricing.get(model)
        if price is None:
            return not_found("price for model", model)
        return JSONResponse(authz.jsonable(price))

    @router.put("/api/pricing/{model:path}")
    async def set_pricing(model: str, request: Request) -> Response:
        """``{input_usd_per_mtok, output_usd_per_mtok}``; null = unpriced."""
        body = await json_object(request)
        if isinstance(body, Response):
            return body
        rates: list[Any] = []
        for name in ("input_usd_per_mtok", "output_usd_per_mtok"):
            value = body.get(name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                return bad_request(f"'{name}' must be a number or null.")
            if isinstance(value, float) and not math.isfinite(value):
                return bad_request(f"'{name}' must be a finite number, got {value!r}.")
            rates.append(value)
        pricing = stores().pricing
        before = pricing.get(model)
        try:
            price = pricing.set(model, rates[0], rates[1], actor=request.state.session.username)
        except ValueError as exc:
            return bad_request(str(exc))
        audit(
            request, AuditAction.PRICING_CHANGED, "pricing", model,
            before=authz.jsonable(before) if before else None, after=authz.jsonable(price),
        )
        return JSONResponse(authz.jsonable(price))

    @router.delete("/api/pricing/{model:path}")
    async def clear_pricing(model: str, request: Request) -> Response:
        pricing = stores().pricing
        before = pricing.get(model)
        if before is None:
            return not_found("price for model", model)
        pricing.clear(model, actor=request.state.session.username)
        audit(request, AuditAction.PRICING_CHANGED, "pricing", model, before=authz.jsonable(before))
        return JSONResponse({"cleared": model})

    # -- wiring ------------------------------------------------------------------

    def _wiring_redact(text: str) -> str:
        red = getattr(ctx.deps.providers, "redactor", None)
        return red.scrub(text) if red is not None else text

    def _wiring_message(exc: Exception) -> str:
        """A refusal's own sentence. A ValueError is a validation sentence and
        is shown verbatim; anything else carries its class name too."""
        if isinstance(exc, ValueError):
            return _wiring_redact(str(exc))
        return errors.detail(exc, getattr(ctx.deps.providers, "redactor", None))

    def _wiring_parse(op: Any) -> tuple[str, dict]:
        """``(kind, fields)`` of one op, or a WiringRefusal naming what is wrong."""
        if not isinstance(op, dict):
            raise WiringRefusal("each op must be an object", "invalid_request")
        kind = op.get("op")
        if kind == "provider.patch":
            provider_id, patch = op.get("provider_id"), op.get("patch")
            if not isinstance(provider_id, str) or not provider_id:
                raise WiringRefusal("provider.patch needs a provider_id", "invalid_request")
            if not isinstance(patch, dict):
                raise WiringRefusal("provider.patch needs a patch object", "invalid_request")
            return kind, {"provider_id": provider_id, "patch": patch}
        if kind in ("route.set", "route.clear"):
            name = op.get("served_name")
            if not isinstance(name, str) or not name:
                raise WiringRefusal(f"{kind} needs a served_name", "invalid_request")
            if kind == "route.clear":
                return kind, {"served_name": name}
            try:
                policy = RoutingPolicy(op.get("policy"))
            except ValueError:
                raise WiringRefusal(
                    "Unknown policy. Valid values: "
                    + ", ".join(p.value for p in RoutingPolicy) + ".",
                    "invalid_policy",
                ) from None
            limits = op.get("limits")
            if limits is not None and not isinstance(limits, dict):
                raise WiringRefusal(
                    "limits must be an object with rpm and/or tpm.", "invalid_request"
                )
            shares = op.get("shares")
            if shares is not None and not isinstance(shares, dict):
                raise WiringRefusal(
                    "shares must be an object of target id to a whole number.", "invalid_request"
                )
            return kind, {"served_name": name, "policy": policy, "limits": limits, "shares": shares}
        raise WiringRefusal(f"unknown op '{kind}'", "unknown_op")

    def _wiring_validate(kind: str, f: dict) -> None:
        """Every check the op's own endpoint makes, with nothing written."""
        if kind == "provider.patch":
            validate = getattr(ctx.deps.providers, "validate_patch", None)
            if not callable(validate):
                raise WiringRefusal(
                    "This coordinator's provider store cannot check a change "
                    "without making it.", "not_implemented",
                )
            try:
                validate(f["provider_id"], f["patch"])
            except UnknownProviderError:
                raise WiringRefusal(
                    f"No provider '{f['provider_id']}'.", "provider_not_found"
                ) from None
            return
        name = f["served_name"]
        try:
            if kind == "route.set":
                ctx.router.validate_policy(name, f["policy"], f["limits"], f["shares"])
            elif name not in ctx.router.index().targets:
                raise KeyError(name)
        except KeyError:
            raise WiringRefusal(
                f"No model '{name}' is being served.", "model_not_found"
            ) from None

    def _wiring_apply(request: Request, kind: str, f: dict) -> None:
        """One op, written and audited exactly as its own endpoint does it."""
        if kind == "provider.patch":
            providers = ctx.deps.providers
            red = getattr(providers, "redactor", None)
            get = getattr(providers, "get", None)
            before = None
            if callable(get):
                try:
                    before = serialize.provider_audit(get(f["provider_id"]), red)
                except Exception:
                    before = None
            provider = providers.update(f["provider_id"], f["patch"])
            audit(
                request, AuditAction.PROVIDER_UPDATED, "provider", f["provider_id"],
                before=before, after=serialize.provider_audit(provider, red),
            )
            return
        name = f["served_name"]
        previous = ctx.router.config_for(name)
        if kind == "route.set":
            config = ctx.router.set_policy(name, f["policy"], limits=f["limits"], shares=f["shares"])
            before = {"policy": getattr(previous.policy, "value", None) if previous else None}
            after = {"policy": config.policy.value, "override": True}
            if f["limits"] is not None:
                before["limits"] = dict(previous.limits) if previous else {}
                after["limits"] = dict(config.limits)
            if f["shares"] is not None:
                before["shares"] = dict(previous.shares) if previous else {}
                after["shares"] = dict(config.shares)
            audit(request, AuditAction.ROUTING_CHANGED, "routing", name, before=before, after=after)
            return
        if not ctx.router.auto_selected(name):
            ctx.router.clear_policy(name)
            config = ctx.router.config_for(name) or previous
            audit(
                request, AuditAction.ROUTING_CHANGED, "routing", name,
                before={"policy": getattr(previous.policy, "value", None) if previous else None,
                        "override": True},
                after={"policy": getattr(config.policy, "value", None) if config else None,
                       "override": False},
            )

    @router.post("/api/wiring")
    async def wiring(request: Request) -> Response:
        """Validate a batch of provider and routing changes, then apply it.

        Pass 1 checks every op and writes nothing; ``dry_run`` stops there.
        Otherwise one failure refuses the whole batch with 400
        ``wiring_invalid``. Pass 2 applies in body order and stops at the first
        exception: that row carries the server's sentence and every later row
        is ``not_applied``. Launches are not ops -- they go through
        ``POST /api/deployments`` on their own.
        """
        body = await json_object(request)
        if isinstance(body, Response):
            return body
        dry_run = body.get("dry_run", False)
        if not isinstance(dry_run, bool):
            return bad_request("dry_run must be true or false.")
        ops = body.get("ops")
        if not isinstance(ops, list):
            return bad_request("ops must be a list.")
        if len(ops) > WIRING_MAX_OPS:
            return bad_request(f"at most {WIRING_MAX_OPS} ops per request")

        def row(index: int, op: Any, error: dict | None) -> dict:
            kind = op.get("op") if isinstance(op, dict) else None
            return {"index": index, "op": kind, "ok": error is None, "error": error}

        parsed: list[tuple[str, dict]] = []
        results: list[dict] = []
        for index, op in enumerate(ops):
            try:
                kind, fields_ = _wiring_parse(op)
                _wiring_validate(kind, fields_)
            except WiringRefusal as exc:
                results.append(row(index, op, {"message": exc.message, "code": exc.code}))
                continue
            except Exception as exc:
                results.append(
                    row(index, op, {"message": _wiring_message(exc), "code": "invalid_request"})
                )
                continue
            parsed.append((kind, fields_))
            results.append(row(index, op, None))
        failed = sum(1 for r in results if not r["ok"])
        if dry_run:
            return JSONResponse({"dry_run": True, "applied": 0, "results": results})
        if failed:
            return JSONResponse(
                status_code=400,
                content={
                    **errors.error_body(
                        f"Nothing was applied: {failed} change(s) failed validation.",
                        "invalid_request_error", "wiring_invalid",
                    ),
                    "results": results,
                },
            )

        applied = 0
        providers_touched = False
        stopped = False
        for index, (kind, fields_) in enumerate(parsed):
            if stopped:
                results[index] = row(index, ops[index], {
                    "message": "Not attempted: an earlier change failed.",
                    "code": "not_applied",
                })
                continue
            try:
                _wiring_apply(request, kind, fields_)
            except Exception as exc:
                log.exception("wiring op %d (%s) failed", index, kind)
                results[index] = row(
                    index, ops[index], {"message": _wiring_message(exc), "code": "apply_failed"}
                )
                stopped = True
                continue
            applied += 1
            providers_touched = providers_touched or kind == "provider.patch"
        if providers_touched:
            ctx.router.rebuild(force_scores=True)
        return JSONResponse({"dry_run": False, "applied": applied, "results": results})

    return router

