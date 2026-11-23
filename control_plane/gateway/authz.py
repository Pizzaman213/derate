"""Who may use the gateway: the session gate on /api, the key gate on /v1.

The gateway used to answer anyone who could reach it. This module is the one
place that decides otherwise, and it decides by path, never by route:

* ``/api/**`` needs a ``derate_session`` cookie naming a live admin session,
  except :data:`OPEN_ROUTES` -- the calls that have to work before there is
  a session, logout included so an expired cookie can always be cleared --
  and ``POST /api/setup/admin``, which is open to any client exactly until
  the first admin exists and answers 410 ``setup_closed`` forever after.
  First-run setup used to be loopback-only; it is not any more, because
  "loopback" is a fact about the network path, not about the operator --
  under Docker Desktop the operator's own browser arrives from the VM's
  gateway (192.168.65.1) and was refused on their own machine. Whoever
  reaches a fresh coordinator first can claim it, so set it up before
  exposing it.
* ``/v1/**`` needs ``Authorization: Bearer derate-...``. Never a query-string
  key: it would land in every access log between the client and here. An
  address that has had :data:`KEY_REFUSALS_PER_ADDRESS_PER_MIN` keys refused
  inside a minute is answered 429 without a lookup until the window drains.
* Everything else (the built UI, ``/healthz``, ``/install.sh``) passes.

A connection accepted on the EDGE socket (``DERATE_EDGE_PORT``, the one
cloudflared is pointed at) is narrower: ``/v1/**`` only, 404 for every other
path, the client address read from ``DERATE_EDGE_CLIENT_HEADER`` and never
from the socket or ``X-Forwarded-For``, and the scheme forced to https
because the edge terminated TLS. :func:`is_edge` decides from
``scope["server"]``, which uvicorn fills from the accepted listener.

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
import collections
import ipaddress
import logging
import re
import threading
import time
from dataclasses import asdict, is_dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from starlette.requests import HTTPConnection, Request
from starlette.responses import Response

from control_plane.contracts.auth import (
    KEY_PREFIX,
    KEY_REFUSALS_PER_ADDRESS_PER_MIN,
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

from .csrf import is_cross_site_write, trusted_origins
from .errors import error_response
from .secevents import address_bucket
from control_plane.redaction import Redactor, printable
from control_plane.telemetry.records import AccessRecord

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
#: Open to any client until the first admin exists; 410 ``setup_closed``
#: afterwards.
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


def edge_not_found() -> Response:
    """Anything but ``/v1/**`` on the edge socket. No security event: the
    access row records it, and 404 noise would bury the real refusals."""
    return error_response(
        404, "Not found.", "invalid_request_error", AuthErrorCode.EDGE_NOT_FOUND.value
    )


def edge_client_unknown(header: str) -> Response:
    return error_response(
        400,
        f"This edge expects the client address in the {header} header and none "
        "was sent; see DERATE_EDGE_CLIENT_HEADER.",
        "invalid_request_error",
        AuthErrorCode.EDGE_CLIENT_UNKNOWN.value,
    )


def invalid_host() -> Response:
    """A ``Host`` that is not a host[:port]. Refused on both listeners before
    anything reads ``request.url``, which Starlette builds from it."""
    return error_response(
        400,
        "The Host header must be a host name or address, with an optional port.",
        "invalid_request_error",
        AuthErrorCode.INVALID_HOST.value,
    )


def key_refusals_exceeded() -> Response:
    return error_response(
        429,
        "Too many refused API keys from this address; wait a minute.",
        "rate_limit_error",
        "rate_limited",
        headers={"Retry-After": "60"},
        retry_after_s=60,
    )


def key_actor(raw: str | None) -> str:
    """``derate-<id>`` for a key-shaped credential, else ``-``. Never the secret."""
    parsed = key_format.parse(raw) if raw else None
    return f"{KEY_PREFIX}{parsed[0]}" if parsed else "-"


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


def _settings_of(conn: HTTPConnection):
    try:
        return conn.app.state.ctx.settings
    except AttributeError:
        return None


def is_edge(scope, settings) -> bool:
    """A connection accepted on the edge socket: ``scope["server"]`` is the
    listener's own ``(host, port)``, set by uvicorn from the accepted socket,
    so the port alone says which listener answered."""
    port = getattr(settings, "edge_port", None)
    server = scope.get("server")
    return port is not None and bool(server) and int(server[1]) == port


def _edge_client(conn: HTTPConnection, header: str) -> str | None:
    """The address in the edge's client header, or None when it cannot be
    the edge's own answer. The edge writes exactly one value and overwrites
    whatever the caller sent, so anything else means the header did not come
    from the edge: absent, repeated (the first copy would be the caller's),
    not a bare IP address (a list, a port, a scope id), or an address the edge
    can never report -- loopback or unspecified, which would let a request
    pass for this machine's own shell in every audit row.

    An IPv4-mapped IPv6 address is reported as the IPv4 address, so the two
    spellings of one caller share one throttle bucket and one audit identity."""
    values = conn.headers.getlist(header)
    if len(values) != 1:
        return None
    try:
        address = ipaddress.ip_address(values[0].strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.scope_id is not None:
            return None  # a zone id names an interface on the sender's box
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
    if address.is_loopback or address.is_unspecified:
        return None
    return str(address)


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
    """Record the socket peer and the resolved client on ``scope["state"]``.

    On an edge connection the client is whatever the edge's own header
    names -- ``X-Forwarded-For`` and :func:`forwarded_client` are not
    consulted, since every WAN caller arrives from the tunnel's loopback --
    ``state["edge"]`` is set, a missing or malformed header leaves
    ``client_ip`` None with ``edge_client_missing`` set for the gate to
    refuse, and the scheme is https: the edge terminated TLS, which is what
    makes HSTS and ``request.url.scheme`` truthful."""
    state = scope.setdefault("state", {})
    client = scope.get("client")
    state["peer_ip"] = client[0] if client else None
    state["peer_port"] = client[1] if client else None
    state["client_port"] = state["peer_port"]
    settings = _settings_of(conn)
    if is_edge(scope, settings):
        state["edge"] = True
        state["client_ip"] = _edge_client(conn, settings.edge_client_header)
        if state["client_ip"] is None:
            state["edge_client_missing"] = True
        scope["scheme"] = "wss" if scope["type"] == "websocket" else "https"
        return
    networks = _trusted_networks(conn)
    state["client_ip"] = forwarded_client(
        state["peer_ip"], conn.headers.get("x-forwarded-for"), networks
    )
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
    return f"key:{KEY_PREFIX}{key_id}" if key_id else "-"


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
    # rules (derate- keys, sk-..., bearer ...); only remembered values are lost.
    try:
        query = (redactor or _PATTERN_REDACTOR).scrub(query)
    except Exception:
        log.error("could not scrub a query string", exc_info=True)
        query = "<unscrubbed>"
    return query[:200]


def _emit_access(scope, state: dict, *, method: str, path: str, query: str,
                 status: int | None, duration_ms: float) -> None:
    """The durable twin of the ``gateway.access`` line: an ``access`` row on
    the telemetry sink, which ``DERATE_TELEMETRY=0`` does not switch off.
    Never raises."""
    try:
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


#: An access line or row keeps this much of a path. A URL can be ~16 KB through
#: Cloudflare; a few thousand of those would rotate node.log's history away.
_LOGGED_PATH_MAX = 512
#: Edge 404s one address gets written down per minute; past it they are still
#: answered, but quietly (see ``on_request_done``).
EDGE_MISSES_PER_ADDRESS_PER_MIN = 60


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
    # A flood answered past its address's budget (a throttled 429, an edge
    # 404 beyond EDGE_MISSES_PER_ADDRESS_PER_MIN) writes no line and no row
    # of its own: the folded security-event row counts it, and one access
    # entry per flooded request is what would rotate real history out.
    if not state.get("quiet"):
        logged = path[:_LOGGED_PATH_MAX]
        _log_access(state, method=method, path=logged, query=query, status=status,
                    duration_ms=duration_ms)
        _emit_access(scope, state, method=method, path=logged, query=query, status=status,
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
            "No API key was sent. Pass one as 'Authorization: Bearer derate-...'; "
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


def _edge_path_refused(path: str) -> bool:
    """Whether the edge refuses *path*: anything outside ``/v1``, and any
    path with a ``.`` or ``..`` segment. uvicorn decodes ``%2F``, so
    ``/v1/..%2Fcluster`` arrives as ``/v1/../cluster`` -- under /v1 by
    prefix, and resolved to the admin UI by the static mount behind it."""
    if not _gated(path, "/v1"):
        return True
    segments = path.split("/")
    return "." in segments or ".." in segments


#: A Host header a client could legitimately send: a name or IPv4 address, or
#: a bracketed IPv6 address, each with an optional port. Anything else -- "/",
#: "?", "#", "@", whitespace, a lone "[" -- is refused before any URL is built
#: from it, because Starlette builds ``request.url`` from exactly this value.
_HOST_RE = re.compile(r"(?:[A-Za-z0-9._-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?")


def host_is_valid(conn: HTTPConnection) -> bool:
    """At most one ``Host``, and that one a plain host[:port]. None at all is
    HTTP/1.0 and fine: the URL then falls back to the socket's own address."""
    values = conn.headers.getlist("host")
    if not values:
        return True
    return len(values) == 1 and _HOST_RE.fullmatch(values[0]) is not None


class RefusalWindow:
    """Client address -> monotonic times of its refused keys in the last
    minute, the shape ``internal_api``'s join window has. :meth:`over` only
    reads; :meth:`note` appends. Its own instance, never the join window: a
    node's joins and a caller's bad keys are different budgets. ``create_app``
    builds one and hands it to :class:`AuthGuard` and ``app.state`` so the
    HTTP gate and :func:`require_key` share it.

    :meth:`over` runs on every /v1 request, valid ones included, on the event
    loop, so it must not cost more as the number of refused addresses grows:
    buckets are kept in last-refused order and expired ones are dropped from
    the front only (amortised O(1)), never by scanning the whole map."""

    #: Buckets kept at once. Past it the least recently refused is forgotten:
    #: a caller spread over more addresses than this gets more than 20 a
    #: minute in total, but no one address does, and memory stays flat.
    MAX_BUCKETS = 100_000

    def __init__(self, per_minute: int, clock=time.monotonic) -> None:
        self._limit = per_minute
        self._clock = clock
        self._window: collections.OrderedDict[str, collections.deque[float]] = collections.OrderedDict()

    #: The unit one caller is counted as (an IPv6 /64), shared with the
    #: security-event fold so the two cannot disagree about who one caller is.
    _bucket = staticmethod(address_bucket)

    def _prune(self, cutoff: float) -> None:
        """Drop buckets whose newest refusal is older than *cutoff*. They are
        in last-refused order, so the expired ones are all at the front."""
        window = self._window
        while window:
            newest = next(iter(window.values()))
            if newest and newest[-1] >= cutoff:
                return
            window.popitem(last=False)

    def over(self, ip: str) -> bool:
        cutoff = self._clock() - 60.0
        self._prune(cutoff)
        recent = self._window.get(self._bucket(ip))
        if not recent:
            return False
        while recent and recent[0] < cutoff:
            recent.popleft()
        return len(recent) >= self._limit

    def note(self, ip: str) -> None:
        now = self._clock()
        self._prune(now - 60.0)
        bucket = self._bucket(ip)
        recent = self._window.get(bucket)
        if recent is None:
            recent = self._window[bucket] = collections.deque()
        else:
            self._window.move_to_end(bucket)
        while recent and recent[0] < now - 60.0:
            recent.popleft()
        recent.append(now)
        while len(self._window) > self.MAX_BUCKETS:
            self._window.popitem(last=False)

    def reset(self) -> None:
        self._window.clear()


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

    def __init__(self, app, key_refusals: RefusalWindow | None = None) -> None:
        self.app = app
        # Peers already warned about an ignored X-Forwarded-For, so the
        # warning is once per peer per process rather than once per request.
        self._xff_warned: set[str] = set()
        # Client address -> refused keys in the last minute, read before the
        # key store so a flood of bad keys costs no lookups.
        self.key_refusals = key_refusals or RefusalWindow(KEY_REFUSALS_PER_ADDRESS_PER_MIN)
        # Client address -> edge 404s in the last minute. Never refuses: past
        # the budget the 404s still go out, they just stop being written down.
        self.edge_misses = RefusalWindow(EDGE_MISSES_PER_ADDRESS_PER_MIN)

    def _warn_untrusted_proxy(self, request: Request, peer: str | None) -> None:
        """F8: a proxy in front with ``DERATE_TRUSTED_PROXIES`` unset records
        the proxy as every client. Say so, once per peer. Not on the edge:
        Cloudflare always sends one and it is ignored there by design."""
        if request.scope.get("state", {}).get("edge"):
            return
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

        if not host_is_valid(conn) or (
            state.get("edge")
            and (_edge_path_refused(scope.get("path", "")) or state.get("edge_client_missing"))
        ):
            # A malformed Host anywhere, and on the edge anything but /v1 or a
            # caller without the client header: closed rather than answered,
            # and the finally below records it.
            status = CLOSE_POLICY
            try:
                await send({"type": "websocket.close", "code": CLOSE_POLICY})
            finally:
                try:
                    await on_request_done(
                        {**scope, "method": "WS"}, status, (time.monotonic() - began) * 1000.0
                    )
                except Exception:
                    log.exception("socket-done hook failed for %s", state.get("request_id"))
            return

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
            # Host first: everything after it may build a URL from it. Then
            # the edge's own refusals, so the edge never admits that /api
            # exists -- not even as a cross-site 403. Then the cross-site
            # write check, so a cross-site write is refused before anything
            # looks at its cookie, inside the guard so its row carries the
            # request id.
            refusal = (
                self._front_refusal(request)
                or _cross_site_refusal(request)
                or self._refusal(request)
            )
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

    def _front_refusal(self, request: Request) -> Response | None:
        """A malformed Host on either listener; on the edge, anything but a
        plain /v1 path, and a /v1 call without the client header."""
        if not host_is_valid(request):
            return invalid_host()
        state = request.scope.get("state") or {}
        if not state.get("edge"):
            return None
        path = request.scope["path"]
        # Before the CORS-preflight bypass and the cross-site check: a
        # preflight or a cross-site write to /api on the edge is still a 404.
        if _edge_path_refused(path):
            caller = state.get("client_ip") or "-"
            if self.edge_misses.over(caller):
                state["quiet"] = True
            else:
                self.edge_misses.note(caller)
            return edge_not_found()
        if state.get("edge_client_missing"):
            _note(
                request,
                AuditAction.KEY_REFUSED,
                actor="-",
                detail={"code": AuthErrorCode.EDGE_CLIENT_UNKNOWN.value, "path": path},
            )
            return edge_client_unknown(request.app.state.ctx.settings.edge_client_header)
        return None

    def _refusal(self, request: Request) -> Response | None:
        """The response that refuses this request, or None to let it through.

        Decides on ``scope["path"]``, the field the router dispatches on --
        never ``request.url.path``, which Starlette rebuilds from the
        caller's ``Host`` header and re-parses: ``Host: x#`` made it empty
        and walked an anonymous caller past the session gate into /api."""
        path = request.scope["path"]
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
                    # Open to any client until the first admin exists (see the
                    # module docstring), then closed for good.
                    if auth.admin_exists():
                        return setup_closed()
                    return None
                session = current_session(stores, request)
                if session is None:
                    _note(request, AuditAction.SESSION_REFUSED, actor="-", detail={"path": path})
                    return session_required()
                request.state.session = session
            else:
                # An address past its refusal budget is answered before the
                # store is read: no lookup, no hash compare, a valid key
                # included -- the budget is on the address, not the key.
                ip = client_ip(request) or "-"
                if self.key_refusals.over(ip):
                    _note(
                        request,
                        AuditAction.KEY_REFUSED,
                        actor=key_actor(bearer_token(request)),
                        detail={"code": "rate_limited", "path": path},
                    )
                    request.scope["state"]["quiet"] = True
                    return key_refusals_exceeded()
                # Opened first, so a broken store is 503 even for a request that
                # brought no key: the gate fails closed, never "just" 401.
                stores.keys
                try:
                    request.state.principal = authenticate_key(stores, request)
                except AuthUnavailable:
                    raise
                except AuthError as exc:
                    self.key_refusals.note(ip)
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
        path=request.scope["path"],
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
        detail={"origin": origin, "path": request.scope["path"]},
    )
    return error_response(
        403,
        f"Cross-origin write refused: {origin!r} does not match this "
        "gateway. Add it to DERATE_ALLOWED_ORIGINS if it should be "
        "allowed to change cluster state.",
        "invalid_request_error",
        "cross_origin_write_refused",
    )


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
        path=websocket.scope["path"],
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
            detail={"origin": origin, "path": websocket.scope["path"]},
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
        _note(websocket, AuditAction.SESSION_REFUSED, actor="-", detail={"path": websocket.scope["path"]})
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
    # The same per-address budget the HTTP gate keeps (app.state.key_refusals,
    # set beside the guard in create_app); absent in a bare test app.
    window = getattr(websocket.app.state, "key_refusals", None)
    ip = client_ip(websocket) or "-"
    if window is not None and window.over(ip):
        _note(
            websocket,
            AuditAction.KEY_REFUSED,
            actor=key_actor(bearer_token(websocket)),
            detail={"code": "rate_limited", "path": websocket.scope["path"]},
        )
        await websocket.close(code=CLOSE_POLICY, reason="rate_limited")
        return None
    try:
        principal = authenticate_key(stores, websocket)
        # Left on the scope so the socket's access row (AuthGuard._socket)
        # names the key it was opened with.
        websocket.scope.setdefault("state", {})["principal"] = principal
        return principal
    except AuthError as exc:
        if window is not None:
            window.note(ip)
        _note(
            websocket,
            AuditAction.KEY_REFUSED,
            actor=key_actor(bearer_token(websocket)),
            detail={"code": exc.code.value, "path": websocket.scope["path"]},
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
