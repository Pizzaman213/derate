"""Admins and their sessions: login, logout, password, the admin list.

Every route here except ``GET /api/auth/state``, ``POST /api/auth/login`` and
``POST /api/auth/logout`` sits behind ``authz.AuthGuard``'s session check, so a handler that reads
``request.state.session`` can rely on it. Password hashing is argon2id and
deliberately slow, so every call that hashes runs on a worker thread.

**Registration must stay ABOVE the ``StaticFiles`` mount in ``app.py``**, for
the reason every router here gives: below it, ``/api/auth/state`` -- the first
call the UI makes -- would answer ``index.html``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import math
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, Response

from control_plane.contracts.auth import (
    LOGIN_INFLIGHT_PER_IP,
    SESSION_COOKIE,
    SESSION_COOKIE_SECURE,
    SESSION_MAX_S,
    VERIFY_INFLIGHT_MAX,
    AuditAction,
    AuthErrorCode,
    InvalidCredentials,
    LoginLocked,
)

from . import authz
from . import body as _body
from .body import BodyTooLarge, payload_too_large, read_json_object
from .deps import GatewayContext
from .errors import error_response

log = logging.getLogger("gateway.auth")


def session_cookie_name(request: Request) -> str:
    """``__Host-derate_session`` when the cookie is Secure, else ``derate_session``."""
    return SESSION_COOKIE_SECURE if _secure(request) else SESSION_COOKIE


def set_session_cookie(response: Response, request: Request, cookie: str) -> None:
    """HttpOnly, SameSite=Strict, Path=/; Secure and ``__Host-``-prefixed on https."""
    response.set_cookie(
        session_cookie_name(request),
        cookie,
        max_age=SESSION_MAX_S,
        path="/",
        httponly=True,
        samesite="strict",
        secure=_secure(request),
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        session_cookie_name(request),
        path="/",
        httponly=True,
        samesite="strict",
        secure=_secure(request),
    )


def _secure(request: Request) -> bool:
    """Secure when the origin browsers use is https. ``DERATE_PUBLIC_ORIGIN``
    says so behind a TLS terminator the gateway cannot see; without it, the
    request's own scheme decides."""
    public = request.app.state.ctx.settings.public_origin
    if public:
        return urlsplit(public).scheme == "https"
    return request.url.scheme == "https"


class _VerifySlots:
    """How many password verifications are in flight, per address and in
    total (GA-2). Each is an argon2id hash on a worker thread; without a cap
    a flood of fresh ``(username, ip)`` pairs -- which lockout never sees --
    fills the thread pool that audit writes and every other ``to_thread``
    caller share. Counters, not semaphores: a request over the cap is
    refused, never queued."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total = 0
        self._per_ip: dict[str, int] = {}

    def take(self, ip: str) -> bool:
        with self._lock:
            held = self._per_ip.get(ip, 0)
            if held >= LOGIN_INFLIGHT_PER_IP or self._total >= VERIFY_INFLIGHT_MAX:
                return False
            self._per_ip[ip] = held + 1
            self._total += 1
            return True

    def give(self, ip: str) -> None:
        with self._lock:
            self._total -= 1
            held = self._per_ip.get(ip, 0) - 1
            if held <= 0:
                self._per_ip.pop(ip, None)
            else:
                self._per_ip[ip] = held


_verify_slots = _VerifySlots()


def _too_many_in_flight(request: Request) -> Response:
    authz._note(
        request, AuditAction.LOGIN_LOCKED, actor="-", detail={"scope": "inflight"}
    )
    return error_response(
        429,
        "Too many sign-in attempts in flight from this address.",
        "rate_limit_error",
        AuthErrorCode.LOGIN_LOCKED.value,
        headers={"Retry-After": "1"},
        retry_after_s=1,
    )


@contextlib.contextmanager
def _verifying(request: Request):
    """Holds a verification slot for the block, or yields the 429 to answer
    with instead of verifying."""
    ip = authz.client_ip(request) or "-"
    if not _verify_slots.take(ip):
        yield _too_many_in_flight(request)
        return
    try:
        yield None
    finally:
        _verify_slots.give(ip)


async def reauthenticated(request: Request, stores: authz.AuthStores, body: dict) -> Response | None:
    """``None`` when ``body["current_password"]`` is the signed-in admin's
    password, else the 401/429 to answer with. Admin-level changes ask for it
    so a stolen cookie alone cannot mint an admin or take the admin key; a
    wrong or missing one is a ``login_failed`` row under the caller's name."""
    session = request.state.session
    password = body.get("current_password")
    with _verifying(request) as refused:
        if refused is not None:
            return refused
        try:
            await asyncio.to_thread(
                stores.auth.reauthenticate,
                session.username,
                password if isinstance(password, str) else "",
                **authz.request_context(request),
            )
        except LoginLocked as exc:
            retry = max(1, math.ceil(exc.retry_after_s or 0))
            return error_response(
                429, exc.message, "rate_limit_error", AuthErrorCode.LOGIN_LOCKED.value,
                headers={"Retry-After": str(retry)}, retry_after_s=retry,
            )
        except InvalidCredentials:
            request.state.audited = True  # the service wrote login_failed
            return error_response(
                401,
                "'current_password' is required and must be your password.",
                "invalid_request_error",
                AuthErrorCode.INVALID_CREDENTIALS.value,
            )
    return None


def bad_request(message: str, code: str = "invalid_request") -> Response:
    return error_response(400, message, "invalid_request_error", code)


async def json_object(request: Request) -> dict | Response:
    """The body as a dict, or the 400/413 that says why it is not one."""
    try:
        return await read_json_object(request)
    except BodyTooLarge:
        return payload_too_large()
    except Exception:
        return bad_request("Request body must be a JSON object.", "invalid_json")


async def json_object_or_empty(request: Request) -> dict | Response:
    """``json_object`` for a route whose body is optional: an empty body is
    ``{}``, a present one must be a JSON object (400 otherwise, 413 past the
    ceiling)."""
    try:
        return await _body.optional_json_object(request)
    except BodyTooLarge:
        return payload_too_large()
    except Exception:
        return bad_request("Request body must be a JSON object.", "invalid_json")


async def optional_json_object(request: Request) -> dict | Response:
    """Like ``json_object`` for a route whose body is optional (a DELETE, a
    rotate): an absent or unparseable body is ``{}``; only an oversize one
    is refused."""
    try:
        return await read_json_object(request)
    except BodyTooLarge:
        return payload_too_large()
    except Exception:
        return {}


def string_fields(body: dict, *names: str) -> tuple[str, ...] | Response:
    values = []
    for name in names:
        value = body.get(name)
        if not isinstance(value, str) or not value:
            return bad_request(f"'{name}' is required and must be a string.", "missing_field")
        values.append(value)
    return tuple(values)


def admin_dict(admin) -> dict:
    return {
        "username": admin.username,
        "email": admin.email,
        "created_at": admin.created_at,
        "created_by": admin.created_by,
    }


def session_dict(session, *, current: bool) -> dict:
    """A session as ``GET /api/auth/sessions`` lists it: the 12-hex id the
    audit rows carry, never the full hash (that is the lookup key)."""
    return {
        "username": session.username,
        "session_id": session.session_id[:12],
        "login_ip": session.login_ip,
        "ip": session.ip,
        "user_agent": session.user_agent,
        "created_at": session.created_at,
        "last_seen_at": session.last_seen_at,
        "expires_at": session.expires_at,
        "current": current,
    }


def create_router(ctx: GatewayContext) -> APIRouter:
    router = APIRouter()

    def stores() -> authz.AuthStores:
        return ctx.auth_stores

    @router.get("/api/auth/state")
    async def auth_state(request: Request) -> JSONResponse:
        """Open: what the UI needs to pick setup, login, or the app."""
        auth = stores().auth
        session = authz.current_session(stores(), request)
        exists = auth.admin_exists()
        return JSONResponse(
            {
                "admin_exists": exists,
                "authenticated": session is not None,
                "username": session.username if session else None,
                # Always false: setup is open to any client until the first
                # admin exists. Kept so a UI that still reads it shows the form.
                "setup_local_only": False,
            }
        )

    @router.post("/api/auth/login")
    async def login(request: Request) -> Response:
        """``username`` is the admin's username or their email."""
        body = await json_object(request)
        if isinstance(body, Response):
            return body
        fields = string_fields(body, "username", "password")
        if isinstance(fields, Response):
            return fields
        username, password = fields
        auth = stores().auth
        context = authz.request_context(request)
        # Failures from one address over any usernames slow it down rather
        # than lock anybody out: behind NAT one address is many people. The
        # same goes for failures spread over addresses for one username.
        delay = await asyncio.to_thread(auth.login_delay_s, context["ip"], username)
        if delay > 0:
            await asyncio.sleep(delay)
        request.state.audited = True  # every outcome below is the service's own row
        with _verifying(request) as refused:
            if refused is not None:
                return refused
            try:
                session, cookie = await asyncio.to_thread(auth.login, username, password, **context)
            except LoginLocked as exc:
                retry = max(1, math.ceil(exc.retry_after_s or 0))
                return error_response(
                    429,
                    exc.message,
                    "rate_limit_error",
                    AuthErrorCode.LOGIN_LOCKED.value,
                    headers={"Retry-After": str(retry)},
                    retry_after_s=retry,
                )
            except InvalidCredentials as exc:
                return error_response(
                    401, exc.message, "invalid_request_error", AuthErrorCode.INVALID_CREDENTIALS.value
                )
        response = JSONResponse(
            {
                "username": session.username,
                "created_at": session.created_at,
                "expires_at": session.expires_at,
            }
        )
        set_session_cookie(response, request, cookie)
        return response

    @router.post("/api/auth/logout")
    async def logout(request: Request) -> Response:
        cookie = authz.session_cookie(request)
        if cookie:
            await asyncio.to_thread(
                stores().auth.logout, cookie, **authz.request_context(request)
            )
            request.state.audited = True
        response = JSONResponse({"logged_out": True})
        clear_session_cookie(response, request)
        return response

    @router.get("/api/auth/me")
    async def me(request: Request) -> JSONResponse:
        session = request.state.session
        key = authz.admin_key(stores(), session.username)
        return JSONResponse(
            {
                "username": session.username,
                "session": {
                    "created_at": session.created_at,
                    "last_seen_at": session.last_seen_at,
                    "expires_at": session.expires_at,
                    "ip": session.ip,
                    "user_agent": session.user_agent,
                },
                "admin_key_id": key.key_id,
            }
        )

    @router.delete("/api/auth/sessions")
    async def revoke_sessions(request: Request) -> Response:
        """Sign out everywhere: every session of yours ends, this one
        included. Audited as ``sessions_revoked`` with how many there were."""
        session = request.state.session
        count = await asyncio.to_thread(
            stores().auth.revoke_all,
            session.username,
            actor=session.username,
            **authz.request_context(request),
        )
        request.state.audited = True
        response = JSONResponse({"revoked": count})
        clear_session_cookie(response, request)
        return response

    @router.get("/api/auth/sessions")
    async def list_sessions(request: Request) -> JSONResponse:
        """Every live session of every admin: where it signed in (``login_ip``),
        where it was last seen from (``ip``, ``user_agent``) and when. The id
        is the 12-hex prefix the audit log carries, never the cookie."""
        mine = request.state.session.session_id
        sessions = await asyncio.to_thread(stores().auth.list_sessions)
        return JSONResponse([session_dict(s, current=s.session_id == mine) for s in sessions])

    @router.delete("/api/admins/{username}/sessions")
    async def revoke_admin_sessions(username: str, request: Request) -> Response:
        """Sign *username* out everywhere. Any admin may do this to any admin:
        it is the containment step for a cookie seen somewhere it should not
        be. Audited as ``sessions_revoked``, actor the caller, target
        *username*."""
        session = request.state.session
        auth = stores().auth
        if not any(a.username == username for a in auth.list_admins()):
            return error_response(
                404, f"no admin named {username!r}", "invalid_request_error", "not_found"
            )
        count = await asyncio.to_thread(
            auth.revoke_all,
            username,
            actor=session.username,
            **authz.request_context(request),
        )
        request.state.audited = True
        response = JSONResponse({"username": username, "revoked": count})
        if username == session.username:
            clear_session_cookie(response, request)
        return response

    @router.post("/api/auth/password")
    async def change_password(request: Request) -> Response:
        """Change your own password. Every session of yours ends, this one
        included: the UI signs in again with the new one."""
        session = request.state.session
        body = await json_object(request)
        if isinstance(body, Response):
            return body
        fields = string_fields(body, "old", "new")
        if isinstance(fields, Response):
            return fields
        old, new = fields
        try:
            await asyncio.to_thread(
                stores().auth.change_password,
                session.username,
                old,
                new,
                actor=session.username,
                **authz.request_context(request),
            )
            request.state.audited = True
        except InvalidCredentials as exc:
            return error_response(
                400, exc.message, "invalid_request_error", AuthErrorCode.INVALID_CREDENTIALS.value
            )
        except ValueError as exc:
            return bad_request(str(exc))
        response = JSONResponse({"changed": True, "reauthenticate": True})
        clear_session_cookie(response, request)
        return response

    @router.get("/api/admins")
    async def list_admins() -> JSONResponse:
        return JSONResponse([admin_dict(a) for a in stores().auth.list_admins()])

    @router.post("/api/admins")
    async def add_admin(request: Request) -> Response:
        """A new admin, and the personal admin key their UI chat runs as."""
        session = request.state.session
        body = await json_object(request)
        if isinstance(body, Response):
            return body
        refused = await reauthenticated(request, stores(), body)
        if refused is not None:
            return refused
        fields = string_fields(body, "username", "email", "password")
        if isinstance(fields, Response):
            return fields
        username, email, password = fields
        try:
            admin = await asyncio.to_thread(
                stores().auth.create_admin,
                username,
                password,
                email=email,
                created_by=session.username,
                **authz.request_context(request),
            )
        except ValueError as exc:
            return bad_request(str(exc))
        key = authz.mint_admin_key(stores(), admin.username)
        authz.audit_request(
            request, AuditAction.KEY_CREATED, "key", key.key_id, after=authz.key_dict(key)
        )
        return JSONResponse({"admin": admin_dict(admin), "admin_key_id": key.key_id})

    @router.delete("/api/admins/{username}")
    async def remove_admin(username: str, request: Request) -> Response:
        """Remove an admin: their sessions end and their admin key is disabled."""
        session = request.state.session
        body = await optional_json_object(request)
        if isinstance(body, Response):
            return body
        refused = await reauthenticated(request, stores(), body)
        if refused is not None:
            return refused
        try:
            stores().auth.remove_admin(
                username, actor=session.username, **authz.request_context(request)
            )
            request.state.audited = True
        except ValueError as exc:
            message = str(exc)
            status = 404 if message.startswith("no admin named") else 400
            return error_response(
                status, message, "invalid_request_error",
                "not_found" if status == 404 else "invalid_request",
            )
        keys = stores().keys
        for key in keys.list_keys():
            if key.is_admin_key and key.admin_username == username and key.status.value == "active":
                before = authz.key_dict(key)
                after = keys.disable_key(key.key_id, actor=session.username, reason="admin removed")
                authz.audit_request(
                    request,
                    AuditAction.KEY_DISABLED,
                    "key",
                    key.key_id,
                    before=before,
                    after=authz.key_dict(after),
                )
        return JSONResponse({"removed": username})

    return router
