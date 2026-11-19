"""Who may use the gateway: the session gate on /api, the key gate on /v1.

The gateway used to answer anyone who could reach it. This module is the one
place that decides otherwise, and it decides by path, never by route:

* ``/api/**`` needs a ``derate_session`` cookie naming a live admin session,
  except :data:`OPEN_ROUTES` -- the calls that have to work before there is
  a session, logout included so an expired cookie can always be cleared --
  and ``POST /api/setup/admin``, which is open exactly until the first admin
  exists, and then only to a loopback peer (403 ``setup_local_only``
  otherwise); it answers 410 ``setup_closed`` forever after.
* ``/v1/**`` needs ``Authorization: Bearer drt_...``. Never a query-string
  key: it would land in every access log between the client and here.
* Everything else (the built UI, ``/healthz``, ``/install.sh``) passes.

Every refusal is a security event (``secevents.py``); every gated request
ends in one ``gateway.access`` line and, for an /api write no handler
audited itself, one ``api_write`` audit row (:func:`on_request_done`).

A store that cannot be opened makes every gated path answer 503
``auth_unavailable``: the gate fails closed, never open.

Websockets do not pass through HTTP middleware, so the two socket routes call
:func:`require_session` / :func:`require_key` themselves, before ``accept``;
both refuse a cross-site ``Origin`` the way ``csrf.py`` refuses a write.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import threading
import time
from urllib.parse import urlsplit
from dataclasses import asdict, is_dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from starlette.requests import HTTPConnection, Request
from starlette.responses import Response

from control_plane.contracts.auth import (
    SESSION_COOKIE,
    SESSION_COOKIE_SECURE,
    ApiKey,
    AuthError,
    AuthErrorCode,
    AuthUnavailable,
    LimitScope,
    Org,
    Principal,
    PrincipalVia,
    Session,
)

from control_plane.contracts.auth import AuditAction
from control_plane.keys import format as key_format

from .csrf import is_cross_site_write
from .errors import error_response
from control_plane.redaction import Redactor, printable

log = logging.getLogger("gateway.authz")
access_log = logging.getLogger("gateway.access")
security_log = logging.getLogger("gateway.security")

#: Reachable with no session, by design and forever. ``(method, path)``.
OPEN_ROUTES = frozenset(
    {
        ("GET", "/healthz"),
        ("GET", "/install.sh"),
        # Worker join authenticates with the cluster token in its body.
        ("POST", "/api/nodes/join"),
        # The UI has to learn whether to show setup or login before it has a session.
        ("GET", "/api/auth/state"),
        ("POST", "/api/auth/login"),
        # Idempotent; an expired cookie must still be clearable.
        ("POST", "/api/auth/logout"),
    }
)
#: Open until the first admin exists, and only to a loopback peer;
#: 410 ``setup_closed`` afterwards.
SETUP_ROUTE = ("POST", "/api/setup/admin")

#: Sent with every /v1 401, as RFC 6750 asks.
WWW_AUTHENTICATE = {"WWW-Authenticate": 'Bearer realm="derate"'}

#: Websocket close code for a refused credential (RFC 6455: policy violation).
CLOSE_POLICY = 1008


class AuthStores:
    """The four auth stores, each opened the first time something needs it.

    ``AuthService.open`` and ``KeyService.open`` touch the disk at once, and
    ``create_app`` must not -- the routes generator and the test suite build
    apps that never serve a request. So nothing opens until first use; a
    success is cached, and a failure raises ``AuthUnavailable`` and is tried
    again on the next call rather than remembered.

    The auth service is opened with the audit mirror under
    ``<logs dir>/audit`` (or *audit_dir*) and the ``DERATE_AUDIT_SINK`` sink;
    ``audit_mirror=False`` / ``audit_sink=False`` leave either out.
    """

    def __init__(
        self,
        auth_path: Path | str,
        keys_path: Path | str,
        limits_path: Path | str,
        *,
        audit_dir: Path | str | None = None,
        audit_mirror: bool = True,
        audit_sink: bool = True,
    ):
        self.auth_path = Path(auth_path)
        self.keys_path = Path(keys_path)
        self.limits_path = Path(limits_path)
        self.audit_dir = Path(audit_dir) if audit_dir is not None else None
        self.audit_mirror = audit_mirror
        self.audit_sink = audit_sink
        # Re-entrant: opening limits opens keys and pricing under the same lock.
        self._lock = threading.RLock()
        self._auth = None
        self._keys = None
        self._pricing = None
        self._limits = None
        self._sink = None

    def _open(self, attr: str, build) -> Any:
        value = getattr(self, attr)
        if value is not None:
            return value
        with self._lock:
            value = getattr(self, attr)
            if value is None:
                try:
                    value = build()
                except AuthUnavailable:
                    raise
                except Exception as exc:
                    raise AuthUnavailable(
                        f"the auth store could not be opened: {type(exc).__name__}: {exc}"
                    ) from exc
                setattr(self, attr, value)
        return value

    def _open_auth(self):
        from control_plane import auditlog, paths
        from control_plane.auth import AuthService

        mirror = None
        if self.audit_mirror:
            mirror = auditlog.AuditMirror(self.audit_dir or paths.logs_dir() / "audit")
        sink = auditlog.AuditSink.from_env() if self.audit_sink else None
        service = AuthService.open(self.auth_path, mirror=mirror, sink=sink)
        if sink is not None:
            sink.start(heartbeat=service.head_hash)
            self._sink = sink
        return service

    @property
    def auth(self):
        return self._open("_auth", self._open_auth)

    @property
    def keys(self):
        from control_plane.keys import KeyService

        return self._open("_keys", lambda: KeyService.open(self.keys_path))

    @property
    def pricing(self):
        from control_plane.limits import PricingStore

        return self._open("_pricing", lambda: PricingStore.open(self.limits_path))

    @property
    def limits(self):
        from control_plane.limits import LimitService

        return self._open(
            "_limits",
            lambda: LimitService.open(self.limits_path, keys=self.keys, pricing=self.pricing),
        )

    def close(self) -> None:
        with self._lock:
            for attr in ("_sink", "_limits", "_pricing", "_keys", "_auth"):
                store = getattr(self, attr)
                setattr(self, attr, None)
                if store is None:
                    continue
                try:
                    store.close()
                except Exception:
                    log.debug("closing %s failed", attr, exc_info=True)


# -- errors ------------------------------------------------------------------


#: What a 503 tells the caller. Never the exception text: that names the data
#: directory, and this answer goes to unauthenticated callers too.
UNAVAILABLE_MESSAGE = "The authentication store is unavailable."


def unavailable(exc: AuthUnavailable | None = None) -> Response:
    log.warning("auth store unavailable: %s", exc.message if exc else "")
    return error_response(
        503,
        UNAVAILABLE_MESSAGE,
        "server_error",
        AuthErrorCode.AUTH_UNAVAILABLE.value,
    )


def session_required() -> Response:
    return error_response(
        401,
        "Sign in to use this endpoint: it needs an admin session.",
        "invalid_request_error",
        AuthErrorCode.SESSION_REQUIRED.value,
    )


def key_refused(code: AuthErrorCode, message: str) -> Response:
    return error_response(401, message, "invalid_request_error", code.value, headers=WWW_AUTHENTICATE)


def setup_closed() -> Response:
    return error_response(
        410,
        "Setup is closed: an admin already exists. Sign in instead.",
        "invalid_request_error",
        AuthErrorCode.SETUP_CLOSED.value,
    )


def setup_local_only(port: int) -> Response:
    return error_response(
        403,
        "the first admin can only be created from the coordinator itself: open the "
        "setup page on that machine, or run "
        f"`ssh -L {port}:127.0.0.1:{port} <coordinator>` and use "
        f"http://127.0.0.1:{port}/setup through the tunnel",
        "invalid_request_error",
        AuthErrorCode.SETUP_LOCAL_ONLY.value,
    )


def key_actor(raw: str | None) -> str:
    """``drt_<id>`` for a key-shaped credential, else ``-``. Never the secret."""
    parsed = key_format.parse(raw) if raw else None
    return f"drt_{parsed[0]}" if parsed else "-"


def _note(conn: HTTPConnection, action, *, actor: str, detail: dict) -> None:
    """A security event for this connection. Never raises."""
    try:
        ctx = conn.app.state.ctx
        state = conn.scope.get("state", {})
        ctx.secevents.note(
            action,
            actor=actor,
            ip=client_ip(conn),
            user_agent=state.get("user_agent") or None,
            request_id=state.get("request_id"),
            detail=detail,
        )
    except Exception:
        log.error("could not note %s", getattr(action, "value", action), exc_info=True)


# -- request helpers ---------------------------------------------------------


def client_ip(conn: HTTPConnection) -> str | None:
    """The client address, as :class:`AuthGuard` resolved it for this
    request (``state["client_ip"]``): the socket peer, or -- only when that
    peer is inside ``DERATE_TRUSTED_PROXIES`` -- the first untrusted hop of
    ``X-Forwarded-For`` read right to left (:func:`forwarded_client`).
    Nothing a client sends chooses it."""
    state = conn.scope.get("state") or {}
    if "client_ip" in state:
        return state["client_ip"]
    return _resolve_client(conn)[0]


def peer_ip(conn: HTTPConnection) -> str | None:
    """The socket's own peer, never rewritten by any header."""
    client = conn.scope.get("client")
    return client[0] if client else None


def _trusted_networks(conn: HTTPConnection) -> tuple:
    try:
        return conn.app.state.ctx.settings.trusted_networks
    except AttributeError:
        return ()


def _in_networks(ip: str, networks) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(address in network for network in networks)


def forwarded_client(peer: str | None, xff: str | None, trusted) -> str | None:
    """The client address for a request from socket peer *peer* carrying
    ``X-Forwarded-For: xff``.

    The header counts only when *peer* is inside a *trusted* network, and
    then it is read right to left: each trusted hop is skipped and the first
    address that is not trusted is the client -- the one hop no trusted proxy
    would have let a client write. When every element is trusted, the header
    is empty, or any element is not an IP address, the answer is *peer*:
    never the leftmost element, which is the one the client wrote itself.

    A loopback hop from a peer that is not itself loopback is not believed
    either: no other machine can have been reached from this one's loopback,
    and naming it is exactly how a LAN client inside a trusted CIDR would
    pass for the operator's own shell (GA-1)."""
    if not peer or not trusted or not xff or not _in_networks(peer, trusted):
        return peer
    hops = [part.strip() for part in xff.split(",")]
    for hop in hops:
        try:
            ipaddress.ip_address(hop)
        except ValueError:
            return peer
    for hop in reversed(hops):
        if not _in_networks(hop, trusted):
            if is_loopback(hop) and not is_loopback(peer):
                return peer
            return hop
    return peer


def _resolve_client(conn: HTTPConnection) -> tuple[str | None, str | None]:
    """``(client_ip, peer_ip)`` for *conn*, from the socket and the header."""
    peer = peer_ip(conn)
    return forwarded_client(peer, conn.headers.get("x-forwarded-for"), _trusted_networks(conn)), peer


_FORWARDED_SCHEMES = frozenset({"http", "https", "ws", "wss"})


def _apply_forwarded_proto(scope, conn: HTTPConnection, networks) -> None:
    """``X-Forwarded-Proto`` from a trusted proxy sets the request's scheme,
    as uvicorn did before derate took the resolution over. Ignored from
    anyone else."""
    peer = peer_ip(conn)
    if not peer or not networks or not _in_networks(peer, networks):
        return
    proto = (conn.headers.get("x-forwarded-proto") or "").split(",")[-1].strip().lower()
    if proto not in _FORWARDED_SCHEMES:
        return
    if scope["type"] == "websocket":
        proto = proto.replace("http", "ws")
    scope["scheme"] = proto


def _stamp_address(scope, conn: HTTPConnection) -> None:
    """Record the socket peer and the resolved client on ``scope["state"]``."""
    state = scope.setdefault("state", {})
    client = scope.get("client")
    networks = _trusted_networks(conn)
    state["peer_ip"] = client[0] if client else None
    state["peer_port"] = client[1] if client else None
    state["client_ip"] = forwarded_client(
        state["peer_ip"], conn.headers.get("x-forwarded-for"), networks
    )
    state["client_port"] = state["peer_port"]
    _apply_forwarded_proto(scope, conn, networks)


def is_loopback(ip: str | None) -> bool:
    """Whether *ip* is a loopback address. False for None or garbage."""
    if not ip:
        return False
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def new_request_id() -> str:
    """``r-<12 hex>``: the id an /api or /v1 request carries everywhere --
    the ``x-request-id`` header, the ``requests`` row, the audit row."""
    return f"r-{uuid4().hex[:12]}"


_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
#: POSTs that change nothing: a dry-run plan, and the UI's chat, whose every
#: message is already a ``requests`` row under the same request id. Auditing
#: them would bury the admin's real changes under one row per question asked.
_NOT_MUTATIONS = frozenset(
    {
        ("POST", "/api/plan"),
        ("POST", "/api/chat/completions"),
        ("POST", "/api/chat/speech"),
        ("POST", "/api/chat/transcriptions"),
    }
)


def _who(state: dict) -> str:
    session = state.get("session")
    if session is not None:
        return f"session:{session.username}"
    principal = state.get("principal")
    key = getattr(principal, "key", None) if principal is not None else None
    key_id = getattr(key, "key_id", None)
    return f"key:drt_{key_id}" if key_id else "-"


_PATTERN_REDACTOR = Redactor()


def _query_of(scope) -> str:
    """The first 200 characters of the query string, through the shared
    provider Redactor (a key pasted into ``?api_key=`` must not reach the
    log or the archive). Never raises."""
    query = (scope.get("query_string") or b"").decode("latin-1")
    if not query:
        return ""
    try:
        redactor = getattr(scope["app"].state.ctx.deps.providers, "redactor", None)
    except Exception:
        redactor = None
    # Without the shared one, a fresh Redactor still applies the pattern
    # rules (drt_ keys, sk-..., bearer ...); only remembered values are lost.
    try:
        query = (redactor or _PATTERN_REDACTOR).scrub(query)
    except Exception:
        log.error("could not scrub a query string", exc_info=True)
        query = "<unscrubbed>"
    return query[:200]


def trusted_origins(settings) -> tuple[str, ...]:
    """``DERATE_ALLOWED_ORIGINS`` plus ``DERATE_PUBLIC_ORIGIN`` when set."""
    public = getattr(settings, "public_origin", "")
    return tuple(settings.allowed_origins) + ((public,) if public else ())


def _emit_access(scope, state: dict, *, method: str, path: str, query: str,
                 status: int | None, duration_ms: float) -> None:
    """The durable twin of the ``gateway.access`` line: an ``access`` row on
    the telemetry sink, which ``DERATE_TELEMETRY=0`` does not switch off.
    Never raises."""
    try:
        # Imported here: AccessRecord ships with the telemetry branch, and
        # without it the access row is skipped rather than the gateway failing.
        from control_plane.telemetry.records import AccessRecord
        ctx = scope["app"].state.ctx
        sink = getattr(ctx, "sink", None)
        if sink is None or not hasattr(sink, "access"):
            return
        sink.access(
            AccessRecord(
                request_id=state.get("request_id") or "-",
                ts=time.time(),
                client_ip=state.get("client_ip"),
                client_port=state.get("client_port"),
                peer_ip=state.get("peer_ip"),
                who=_who(state),
                method=method,
                path=path,
                query=query,
                status=status,
                duration_ms=round(duration_ms, 3),
                user_agent=state.get("user_agent") or None,
                node_id=getattr(ctx.settings, "coordinator_node_id", "") or "",
            )
        )
    except Exception:
        log.error("could not record access for %s %s", method, path, exc_info=True)


def _log_access(state: dict, *, method: str, path: str, query: str,
                status: int | None, duration_ms: float) -> None:
    # The path is printable()-escaped: Starlette decodes %0A into it, and a
    # raw newline would let a URL write a whole forged line into node.log.
    access_log.info(
        "rid=%s ip=%s port=%d peer=%s who=%s %s %s q=%r %d %dms ua=%r",
        state.get("request_id", "-"),
        state.get("client_ip") or "-",
        state.get("client_port") or 0,
        state.get("peer_ip") or "-",
        _who(state),
        method,
        printable(path),
        query,
        status or 0,
        int(duration_ms),
        state.get("user_agent", ""),
    )


async def on_request_done(scope, status: int | None, duration_ms: float) -> None:
    """Called once per HTTP request -- every path, not only /api and /v1 --
    after its response has been sent, or after it failed (*status* None, or
    500 when the handler raised). ``scope["state"]`` holds ``request_id``,
    ``client_ip``, ``client_port``, ``peer_ip``, ``peer_port``, ``user_agent``, ``started`` and whatever
    the gate and the handler left there (``session``, ``principal``,
    ``audited``). Writes the ``gateway.access`` line, the matching ``access``
    row, and, for an /api write with a session that no handler audited, the
    generic ``api_write`` row. Must never raise."""
    state = scope.get("state", {})
    method = scope.get("method", "")
    path = scope.get("path", "")
    query = _query_of(scope)
    _log_access(state, method=method, path=path, query=query, status=status,
                duration_ms=duration_ms)
    _emit_access(scope, state, method=method, path=path, query=query, status=status,
                 duration_ms=duration_ms)
    session = state.get("session")
    if (
        session is None
        or state.get("audited")
        or method not in _WRITE_METHODS
        or not _gated(path, "/api")
        or (method, path) in _NOT_MUTATIONS
    ):
        return
    try:
        auth = scope["app"].state.ctx.auth_stores.auth
        await asyncio.to_thread(
            auth.audit,
            session.username,
            AuditAction.API_WRITE,
            "route",
            f"{method} {path}",
            after={"status": status, "query": query},
            ip=state.get("client_ip"),
            request_id=state.get("request_id"),
            user_agent=state.get("user_agent") or None,
            session_id=session.session_id[:12],
            status=status,
        )
    except Exception:
        log.error("could not write the api_write row for %s %s", method, path, exc_info=True)


def bearer_token(conn: HTTPConnection) -> str | None:
    """The credential in ``Authorization``, or None when there is none.

    ``Bearer <token>`` yields the token; any other value is returned whole, so
    a malformed header is ``invalid_api_key`` rather than ``missing_api_key``.
    """
    raw = conn.headers.get("authorization")
    if raw is None or not raw.strip():
        return None
    scheme, _, rest = raw.strip().partition(" ")
    if scheme.lower() == "bearer":
        return rest.strip() or None
    return raw.strip()


def stores_of(conn: HTTPConnection) -> AuthStores:
    return conn.app.state.ctx.auth_stores


def session_id_of(conn: HTTPConnection) -> str | None:
    """The first 12 hex of the request's session hash -- the prefix of
    ``sessions.sid_hash`` an investigator joins an audit row to. Never the
    cookie."""
    session = conn.scope.get("state", {}).get("session")
    return session.session_id[:12] if session is not None else None


def request_context(conn: HTTPConnection) -> dict:
    """Who/where/which-request for an audit row: ``ip``, ``request_id``,
    ``user_agent``, ``session_id`` -- the kwargs ``AuthPort.audit`` takes."""
    state = conn.scope.get("state", {})
    return {
        "ip": client_ip(conn),
        "request_id": state.get("request_id"),
        "user_agent": state.get("user_agent") or conn.headers.get("user-agent", "")[:512] or None,
        "session_id": session_id_of(conn),
    }


def audit_request(
    conn: HTTPConnection,
    action,
    target_kind: str,
    target_id: str,
    *,
    before: dict | None = None,
    after: dict | None = None,
    actor: str | None = None,
    status: int | None = None,
) -> None:
    """Write the audit row for what this request did, attributed to its
    session, and mark the request audited so the gate's generic
    ``api_write`` row is not written as well. Synchronous, like every
    ``AuthService`` call a handler makes."""
    state = conn.scope.setdefault("state", {})
    session = state.get("session")
    stores_of(conn).auth.audit(
        actor or (session.username if session is not None else "-"),
        action,
        target_kind,
        target_id,
        before=before,
        after=after,
        status=status,
        **request_context(conn),
    )
    state["audited"] = True


def authenticate_key(stores: AuthStores, conn: HTTPConnection) -> Principal:
    """The principal behind this connection's key. Raises ``AuthError``."""
    token = bearer_token(conn)
    if token is None:
        raise AuthError(
            AuthErrorCode.MISSING_API_KEY,
            "No API key was sent. Pass one as 'Authorization: Bearer drt_...'; "
            "an admin creates keys on the Keys screen.",
        )
    ip = client_ip(conn)
    keys = stores.keys
    key = keys.authenticate(token, ip=ip)
    org = keys.get_org(key.org_id)
    if org is None:  # pragma: no cover - authenticate already refuses a deleted org
        raise AuthError(AuthErrorCode.INVALID_API_KEY, "the API key is not valid")
    return Principal(
        key=key,
        org=org,
        via=PrincipalVia.API_KEY,
        ip=ip,
        user_agent=conn.headers.get("user-agent"),
    )


def model_allowed(stores: AuthStores, principal: Principal, model: str) -> bool:
    """Whether *principal*'s key may name *model* at all (K-3).

    Checked before the catalogue is consulted, so a restricted key gets the
    same 403 for a model that exists and one that does not, and cannot map
    the catalogue by the difference."""
    return model in stores.keys.effective_allowlist(principal.key, principal.org, [model])



def session_cookie(conn: HTTPConnection) -> str | None:
    """The session cookie's value: ``__Host-derate_session`` when present
    (set on https), else ``derate_session``."""
    return conn.cookies.get(SESSION_COOKIE_SECURE) or conn.cookies.get(SESSION_COOKIE)


def current_session(stores: AuthStores, conn: HTTPConnection) -> Session | None:
    cookie = session_cookie(conn)
    if not cookie:
        return None
    session = stores.auth.session(cookie)
    if session is None:
        return None
    return _seen_from(stores, conn, session)


def _seen_from(stores: AuthStores, conn: HTTPConnection, session: Session) -> Session:
    """A session used from another address or browser than it was last seen
    from: a ``session_ip_changed`` security event (folded per user and address
    per window, like every other one) and ``sessions.ip``/``user_agent``
    moved to the new values, so the next request from there is quiet.
    ``login_ip`` keeps where it signed in. A stolen cookie replayed from
    elsewhere is exactly this."""
    ip = client_ip(conn)
    ua = conn.headers.get("user-agent", "")[:512] or None
    if session.ip == ip and session.user_agent == ua:
        return session
    _note(
        conn,
        AuditAction.SESSION_IP_CHANGED,
        actor=session.username,
        detail={
            "session": session.session_id[:12],
            "login_ip": session.login_ip,
            "previous_ip": session.ip,
            "user_agent": ua,
            "previous_user_agent": session.user_agent,
        },
    )
    stores.auth.touch_session(session.session_id, ip=ip, user_agent=ua)
    return replace(session, ip=ip, user_agent=ua)


def admin_key(stores: AuthStores, username: str) -> ApiKey:
    """The admin's personal key, minted when it is missing.

    Chat from the UI runs as this key, so a session's requests carry a real
    ``key_id`` in telemetry and on the Keys screen without any secret ever
    reaching the browser. An admin created before keys existed, or whose key
    was deleted, gets a fresh one here.
    """
    keys = stores.keys
    mine = [
        k
        for k in keys.list_keys()
        if k.is_admin_key and k.admin_username == username
    ]
    if mine:
        live = [k for k in mine if k.status.value == "active"]
        return (live or mine)[-1]
    return mint_admin_key(stores, username)


def mint_admin_key(stores: AuthStores, username: str) -> ApiKey:
    """A new personal admin key. Its secret is discarded: the admin rotates
    the key to obtain a usable one, through the normal one-time reveal."""
    keys = stores.keys
    minted = keys.create_key(
        name=username,
        org_id=keys.default_org_id(),
        created_by=username,
        expiry_days=None,
        is_admin_key=True,
        admin_username=username,
    )
    return minted.key


def session_principal(stores: AuthStores, request: Request, session: Session) -> Principal:
    key = admin_key(stores, session.username)
    if key.status.value == "disabled":
        raise AuthError(
            AuthErrorCode.API_KEY_DISABLED,
            f"your admin key {key.key_id} has been disabled; enable it on the Keys screen",
        )
    org = stores.keys.get_org(key.org_id)
    if org is None:
        raise AuthError(AuthErrorCode.INVALID_API_KEY, "your admin key's org no longer exists")
    return Principal(
        key=key,
        org=org,
        via=PrincipalVia.SESSION,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )


# -- the middleware ----------------------------------------------------------


def _gated(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


class AuthGuard:
    """Every HTTP request's credential check. See the module docstring.

    Plain ASGI rather than ``@app.middleware("http")``: Starlette's
    ``BaseHTTPMiddleware`` re-wraps every response body in a memory stream
    and a task group, and on a gateway whose product is a streamed completion
    that cost showed up as a doubled p99 under ``tests/load`` at 100 rps. This
    only reads the scope and, when it refuses, sends the refusal itself; a
    request it lets through goes to the app untouched. What it learns is
    left on ``scope["state"]`` -- the same dict every ``Request.state`` on
    this request reads -- as ``session`` (an /api request) or ``principal``
    (a /v1 request).
    """

    def __init__(self, app) -> None:
        self.app = app
        # Peers already warned about an ignored X-Forwarded-For, so the
        # warning is once per peer per process rather than once per request.
        self._xff_warned: set[str] = set()

    def _warn_untrusted_proxy(self, request: Request, peer: str | None) -> None:
        """F8: a proxy in front with ``DERATE_TRUSTED_PROXIES`` unset records
        the proxy as every client. Say so, once per peer."""
        if "x-forwarded-for" not in request.headers:
            return
        try:
            if request.app.state.ctx.settings.trusted_proxies:
                return
        except AttributeError:
            return
        key = peer or "-"
        if key in self._xff_warned:
            return
        self._xff_warned.add(key)
        security_log.warning(
            "X-Forwarded-For from %s ignored: set DERATE_TRUSTED_PROXIES if a proxy is in front",
            key,
        )

    async def _socket(self, scope, receive, send) -> None:
        """A socket's access row, written when it closes.

        Sockets skip the HTTP credential gate -- ``require_session`` /
        ``require_key`` decide inside the handler -- so this only gives the
        socket its id and address up front and, once the handler returns,
        records it: status 101 when it was accepted, else the close code it
        was refused with (1008 for a policy refusal), with the duration it
        stayed open and whatever principal the handler's gate left in
        ``scope["state"]``."""
        state = scope.setdefault("state", {})
        began = time.monotonic()
        state["request_id"] = new_request_id()
        state["started"] = began
        conn = HTTPConnection(scope)
        _stamp_address(scope, conn)
        state["user_agent"] = conn.headers.get("user-agent", "")[:512]
        status: int | None = None

        async def tracking_send(message) -> None:
            nonlocal status
            kind = message.get("type")
            if kind == "websocket.accept" and status is None:
                status = 101
            elif kind == "websocket.close" and status is None:
                status = int(message.get("code") or 1000)
            elif kind == "websocket.http.response.start" and status is None:
                status = int(message.get("status") or 0) or None
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        finally:
            try:
                await on_request_done(
                    {**scope, "method": "WS"}, status, (time.monotonic() - began) * 1000.0
                )
            except Exception:
                log.exception("socket-done hook failed for %s", state.get("request_id"))

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "websocket":
            await self._socket(scope, receive, send)
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Every HTTP request gets an id, an address and an access row -- the
        # open paths (/healthz, /install.sh, /auth/callback, the static UI)
        # included. Only the credential check below is prefix-scoped.
        request = Request(scope, receive)
        state = scope.setdefault("state", {})
        rid = new_request_id()
        began = time.monotonic()
        state["request_id"] = rid
        state["started"] = began
        _stamp_address(scope, request)
        state["user_agent"] = request.headers.get("user-agent", "")[:512]
        self._warn_untrusted_proxy(request, state["peer_ip"])

        # A handler past the gate -- the open routes included, which read
        # the stores themselves -- can still find one it cannot open. That is
        # the same fail-closed 503 as at the gate, as long as nothing has been
        # sent yet; after the first byte it is the server error it is.
        started = False
        status: int | None = None

        async def tracking_send(message) -> None:
            nonlocal started, status
            if message["type"] == "http.response.start":
                started = True
                status = message["status"]
                headers = list(message.get("headers") or [])
                if not any(k.lower() == b"x-request-id" for k, _ in headers):
                    headers.append((b"x-request-id", rid.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            # The cross-site write check runs first, so a cross-site write is
            # refused before anything looks at its cookie -- and inside the
            # guard, so its cross_origin_refused row carries the request id.
            refusal = _cross_site_refusal(request) or _refusal(request)
            if refusal is not None:
                await refusal(scope, receive, tracking_send)
                return
            try:
                await self.app(scope, receive, tracking_send)
            except AuthUnavailable as exc:
                if started:
                    raise
                await unavailable(exc)(scope, receive, tracking_send)
        except Exception:
            # The server error middleware answers 500 above us; the record
            # says so rather than 0. A response already started keeps the
            # status the client was actually sent.
            if not started:
                status = 500
            raise
        finally:
            try:
                await on_request_done(scope, status, (time.monotonic() - began) * 1000.0)
            except Exception:
                log.exception("request-done hook failed for %s", rid)


def _cross_site_refusal(request: Request) -> Response | None:
    """The 403 for a cross-site write (csrf.py), or None.

    Unconditional, unlike CORS: this is what protects the default,
    same-origin deployment that never sets DERATE_ALLOWED_ORIGINS. The
    session cookie is SameSite=Strict, so a browser does not attach it to a
    cross-site request anyway; this refuses the write outright."""
    try:
        settings = request.app.state.ctx.settings
    except AttributeError:
        return None
    origin = request.headers.get("origin")
    if not is_cross_site_write(
        method=request.method,
        path=request.url.path,
        origin=origin,
        request_scheme=request.url.scheme,
        request_netloc=request.url.netloc,
        allowed_origins=trusted_origins(settings),
    ):
        return None
    _note(
        request,
        AuditAction.CROSS_ORIGIN_REFUSED,
        actor="-",
        detail={"origin": origin, "path": request.url.path},
    )
    return error_response(
        403,
        f"Cross-origin write refused: {origin!r} does not match this "
        "gateway. Add it to DERATE_ALLOWED_ORIGINS if it should be "
        "allowed to change cluster state.",
        "invalid_request_error",
        "cross_origin_write_refused",
    )


def setup_host_allowed(host: str, public_origin: str | None) -> bool:
    """Whether first-admin setup may answer a request that named *host*:
    a loopback literal, ``localhost``, or ``DERATE_PUBLIC_ORIGIN``'s host."""
    host = host.lower()
    if host == "localhost" or is_loopback(host):
        return True
    return bool(public_origin) and host == (urlsplit(public_origin).hostname or "").lower()


def _is_cors_preflight(request: Request) -> bool:
    """A real CORS preflight, which carries no credential by specification:
    ``OPTIONS`` with ``Access-Control-Request-Method``, and only when
    ``DERATE_ALLOWED_ORIGINS`` names an origin (otherwise no CORS middleware
    exists to answer it). Any other ``OPTIONS`` goes through the gate like
    every method, so it is not an anonymous route-existence oracle."""
    if request.method != "OPTIONS" or "access-control-request-method" not in request.headers:
        return False
    try:
        return bool(request.app.state.ctx.settings.allowed_origins)
    except AttributeError:
        return False


def _refusal(request: Request) -> Response | None:
    """The response that refuses this request, or None to let it through."""
    path = request.url.path
    is_api = _gated(path, "/api")
    is_v1 = _gated(path, "/v1")
    if not (is_api or is_v1) or _is_cors_preflight(request):
        return None

    method = "GET" if request.method == "HEAD" else request.method
    stores = stores_of(request)
    try:
        if is_api:
            route = (method, path)
            if route in OPEN_ROUTES:
                return None
            # Fail closed: a store that cannot open answers 503 here, before a
            # missing cookie could be reported as the problem.
            auth = stores.auth
            if route == SETUP_ROUTE:
                if auth.admin_exists():
                    return setup_closed()
                # Both the socket peer and the resolved client: a trusted
                # proxy on another machine forwarding "127.0.0.1" is the
                # network, not the operator's own shell.
                state = request.scope.get("state") or {}
                peer = state.get("peer_ip", peer_ip(request))
                if not (is_loopback(peer) and is_loopback(client_ip(request))):
                    _note(request, AuditAction.SETUP_REFUSED, actor="-", detail={"path": path})
                    return setup_local_only(request.app.state.ctx.settings.port)
                host = request.url.hostname or ""
                if not setup_host_allowed(host, request.app.state.ctx.settings.public_origin):
                    # A loopback peer naming a foreign host is a DNS-rebinding
                    # page in a browser on this machine, not the operator.
                    _note(
                        request, AuditAction.SETUP_REFUSED, actor="-",
                        detail={"path": path, "host": host},
                    )
                    return setup_local_only(request.app.state.ctx.settings.port)
                return None
            session = current_session(stores, request)
            if session is None:
                _note(request, AuditAction.SESSION_REFUSED, actor="-", detail={"path": path})
                return session_required()
            request.state.session = session
        else:
            # Opened first, so a broken store is 503 even for a request that
            # brought no key: the gate fails closed, never "just" 401.
            stores.keys
            try:
                request.state.principal = authenticate_key(stores, request)
            except AuthUnavailable:
                raise
            except AuthError as exc:
                _note(
                    request,
                    AuditAction.KEY_REFUSED,
                    actor=key_actor(bearer_token(request)),
                    detail={"code": exc.code.value, "path": path},
                )
                return key_refused(exc.code, exc.message)
        return None
    except AuthUnavailable as exc:
        return unavailable(exc)


def _socket_request_id(websocket) -> str:
    """Sockets skip the HTTP gate, so they get their id here."""
    state = websocket.scope.setdefault("state", {})
    rid = state.get("request_id")
    if not rid:
        rid = state["request_id"] = new_request_id()
        _stamp_address(websocket.scope, websocket)
        state["user_agent"] = websocket.headers.get("user-agent", "")[:512]
    return rid


def cross_site_socket(websocket) -> bool:
    """True when a browser on another origin opened this socket. Treated as
    a write: a socket is not a safe method."""
    origin = websocket.headers.get("origin")
    try:
        settings = websocket.app.state.ctx.settings
    except AttributeError:
        return False
    refused = is_cross_site_write(
        method="POST",
        path=websocket.url.path,
        origin=origin,
        request_scheme={"ws": "http", "wss": "https"}.get(websocket.url.scheme, websocket.url.scheme),
        request_netloc=websocket.url.netloc,
        allowed_origins=trusted_origins(settings),
    )
    if refused:
        _note(
            websocket,
            AuditAction.CROSS_ORIGIN_REFUSED,
            actor="-",
            detail={"origin": origin, "path": websocket.url.path},
        )
    return refused


async def require_session(websocket, stores: AuthStores) -> Session | None:
    """The socket's session, or None after closing it 1008 before accept."""
    _socket_request_id(websocket)
    if cross_site_socket(websocket):
        await websocket.close(code=CLOSE_POLICY, reason="cross_origin_refused")
        return None
    try:
        session = current_session(stores, websocket)
    except AuthUnavailable:
        session = None
    if session is None:
        _note(websocket, AuditAction.SESSION_REFUSED, actor="-", detail={"path": websocket.url.path})
        await websocket.close(code=CLOSE_POLICY, reason=AuthErrorCode.SESSION_REQUIRED.value)
        return None
    websocket.scope["state"]["session"] = session
    return session


async def require_key(websocket, stores: AuthStores) -> Principal | None:
    """The socket's principal, or None after closing it 1008 before accept.

    The close reason is the refusal code (``missing_api_key``,
    ``invalid_api_key``, ...), the same word a /v1 401 carries.
    """
    _socket_request_id(websocket)
    if cross_site_socket(websocket):
        await websocket.close(code=CLOSE_POLICY, reason="cross_origin_refused")
        return None
    try:
        principal = authenticate_key(stores, websocket)
        # Left on the scope so the socket's access row (AuthGuard._socket)
        # names the key it was opened with.
        websocket.scope.setdefault("state", {})["principal"] = principal
        return principal
    except AuthError as exc:
        _note(
            websocket,
            AuditAction.KEY_REFUSED,
            actor=key_actor(bearer_token(websocket)),
            detail={"code": exc.code.value, "path": websocket.url.path},
        )
        await websocket.close(code=CLOSE_POLICY, reason=exc.code.value)
        return None


# -- serialisation -----------------------------------------------------------


def jsonable(value: Any) -> Any:
    """A contracts dataclass as plain JSON: enums by value, nothing else changed."""
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def key_dict(key: ApiKey) -> dict:
    """An ``ApiKey`` for a response or an audit row. ``ApiKey`` has no secret
    or hash field, and this adds none."""
    return jsonable(key)


def org_dict(org: Org) -> dict:
    return jsonable(org)


def limit_states(stores: AuthStores, scope: LimitScope, scope_id: str) -> list[dict]:
    return [jsonable(s) for s in stores.limits.states(scope, scope_id)]
