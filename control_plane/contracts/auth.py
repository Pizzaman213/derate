"""Inbound authentication: admins, sessions, orgs, API keys, limits, audit.

The gateway used to answer anyone who could reach it. These are the shapes the
four services that close that agree on -- ``control_plane/auth`` (admins,
sessions, the audit log), ``control_plane/keys`` (orgs and ``derate-`` keys),
``control_plane/limits`` (cycle counters, rate windows, local pricing) -- and
the ``Principal`` the gateway attaches to every request it lets through.

Two rules carried by these types rather than by a comment somewhere else:

* A key's secret exists in exactly one shape, :class:`MintedKey`, returned by
  create and rotate and never stored, logged or listed. Everything else holds
  an :class:`ApiKey`, which has no secret field to leak.
* A limit is named by a string from :data:`LIMIT_TYPES`; the per-model ones are
  ``model:<served_name>:<suffix>`` with a suffix from
  :data:`MODEL_LIMIT_SUFFIXES`. A refusal names it as ``<scope>:<limit_type>``
  (``key:requests_cycle``, ``org:model:gpt-4o:rpm``), which is the
  ``limit_source`` a 429 carries and a telemetry row records.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from .routing import RoutingPolicy
from .telemetry import CostBasis

#: ``derate-<key_id>_<secret>``: the prefix, a 10-char ``[a-z0-9]`` id to look
#: the row up by, and ``secrets.token_urlsafe(KEY_SECRET_BYTES)`` stored as
#: sha256. Keys minted as ``drt_...`` before 2026-11-22 no longer parse.
KEY_PREFIX = "derate-"
KEY_ID_LEN = 10
KEY_SECRET_BYTES = 32
#: The only expiries a key can be created with. ``None`` (never) is reserved
#: for an admin's personal key.
KEY_EXPIRY_DAYS = (1, 10, 20, 30, 50, 100, 365)
#: Usage cycles are 30 days long, anchored at the key's or org's creation.
CYCLE_DAYS = 30

SESSION_COOKIE = "derate_session"
#: The cookie's name when it is set ``Secure``: the ``__Host-`` prefix makes the
#: browser refuse it unless it is Secure, host-only and ``Path=/``, so a sibling
#: subdomain cannot plant or shadow it.
SESSION_COOKIE_SECURE = "__Host-derate_session"
SESSION_IDLE_S = 7 * 86400
SESSION_MAX_S = 30 * 86400

#: Five failures inside the window lock that (username, ip) pair for
#: ``min(LOGIN_LOCK_MAX_S, LOGIN_LOCK_BASE_S * 2**(failures-5))``. Failures
#: for one username spread over addresses that are not themselves locked slow
#: every login for that username by ``min(hold, LOGIN_DELAY_MAX_S)`` where the
#: hold is capped at ``LOGIN_LOCK_USERNAME_MAX_S`` -- a delay, never a refusal:
#: anyone can type a username, so refusing on it is a denial of service on the
#: real admin. Failures from one address across many usernames never refuse;
#: they slow every answer to that address by ``min(2**(failures-5),
#: LOGIN_DELAY_MAX_S)`` seconds instead. ``LOGIN_LOCK_MAX_S`` equals the window:
#: failures age out of it, so no hold can outlast it.
LOGIN_LOCK_THRESHOLD = 5
LOGIN_LOCK_WINDOW_S = 900
LOGIN_LOCK_BASE_S = 30
LOGIN_LOCK_MAX_S = 900
LOGIN_LOCK_USERNAME_MAX_S = 300
LOGIN_DELAY_MAX_S = 8
#: A login with a longer identifier or password is refused before any work.
LOGIN_PASSWORD_MAX_LEN = 1024
#: Password verifications (argon2id, on a worker thread) one address may
#: have in flight at once, and the whole gateway may. Past either, a login
#: or re-authentication answers 429 without hashing, so an anonymous flood
#: cannot fill the thread pool every other ``to_thread`` caller waits on.
LOGIN_INFLIGHT_PER_IP = 2
VERIFY_INFLIGHT_MAX = 4
#: /v1 requests presenting one key id from one client address (an IPv6 /64
#: counts as one) are answered 429 without a key lookup once that key id was
#: refused this many times from there inside the last minute. The budget is
#: the pair's, so a stale key never locks its neighbours on a NAT out.
KEY_REFUSALS_PER_ADDRESS_PER_MIN = 20
#: ...and EVERY key id from that address is, once this many keys of any id
#: were refused from there inside the last minute -- except a key id that
#: authenticated from that address inside the window, which is still checked.
KEY_REFUSALS_PER_ADDRESS_ANY_KEY_PER_MIN = 200
#: The rpm a key is held to when it names none (its org's rpm, if any, is
#: still checked at the org scope). No key is unlimited by omission.
DEFAULT_KEY_RPM = 120
#: The concurrent requests a key is held to when it names none. One key
#: otherwise could hold every connection a backend's pool has (64).
DEFAULT_KEY_MAX_CONCURRENT = 16

#: A limit at or above this fraction of its effective value is "near".
NEAR_LIMIT_FRACTION = 0.8
PASSWORD_MIN_LEN = 12
#: RFC 5321's limit on a forward path, which is the longest address there is.
EMAIL_MAX_LEN = 254

LIMIT_TYPES = (
    "tokens_total_cycle",
    "tokens_input_cycle",
    "tokens_output_cycle",
    "requests_cycle",
    "budget_usd_cycle",
    "local_tokens_cycle",
    "local_usd_cycle",
    "cloud_tokens_cycle",
    "cloud_usd_cycle",
    "rpm",
    "tpm",
    "max_concurrent",
    "max_output_tokens_per_request",
    "max_prompt_tokens_per_request",
    "model:<id>:tokens_cycle",
    "model:<id>:usd_cycle",
    "model:<id>:rpm",
    "model:<id>:tpm",
)
#: Suffixes of the per-model templates: ``model:<served_name>:<suffix>``.
MODEL_LIMIT_SUFFIXES = ("tokens_cycle", "usd_cycle", "rpm", "tpm")


class KeyStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    EXPIRED = "expired"


class LimitScope(str, Enum):
    KEY = "key"
    ORG = "org"
    #: A served name's own rate limit (``RoutingConfig.limits``): rpm/tpm only.
    ROUTE = "route"


class PrincipalVia(str, Enum):
    API_KEY = "api_key"
    #: A UI chat request: the admin's session, attributed to their admin key.
    SESSION = "session"


class AuthErrorCode(str, Enum):
    MISSING_API_KEY = "missing_api_key"
    INVALID_API_KEY = "invalid_api_key"
    API_KEY_DISABLED = "api_key_disabled"
    API_KEY_EXPIRED = "api_key_expired"
    SESSION_REQUIRED = "session_required"
    INVALID_CREDENTIALS = "invalid_credentials"
    LOGIN_LOCKED = "login_locked"
    SETUP_CLOSED = "setup_closed"
    AUTH_UNAVAILABLE = "auth_unavailable"
    LIMIT_EXCEEDED = "limit_exceeded"
    MAX_OUTPUT_TOKENS_EXCEEDED = "max_output_tokens_exceeded"
    MAX_PROMPT_TOKENS_EXCEEDED = "max_prompt_tokens_exceeded"
    LIMIT_EXCEEDS_ORG = "limit_exceeds_org"
    ALLOWLIST_EXCEEDS_ORG = "allowlist_exceeds_org"
    MODEL_NOT_ALLOWED = "model_not_allowed"
    #: ``POST /api/setup/admin`` from anywhere but the coordinator's loopback.
    SETUP_LOCAL_ONLY = "setup_local_only"
    #: Anything but ``/v1/**`` on the edge listener (``DERATE_EDGE_PORT``).
    EDGE_NOT_FOUND = "not_found"
    #: An edge request whose ``DERATE_EDGE_CLIENT_HEADER`` holds no IP address.
    EDGE_CLIENT_UNKNOWN = "edge_client_unknown"
    #: A chat body's media URL names a non-public host.
    MEDIA_URL_REFUSED = "media_url_refused"
    #: A ``Host`` header that is not a host[:port], on either listener.
    INVALID_HOST = "invalid_host"


class AuditAction(str, Enum):
    LOGIN = "login"
    LOGIN_FAILED = "login_failed"
    LOGOUT = "logout"
    PASSWORD_CHANGED = "password_changed"
    ADMIN_ADDED = "admin_added"
    ADMIN_REMOVED = "admin_removed"
    SETUP_ADMIN_CREATED = "setup_admin_created"
    ORG_CREATED = "org_created"
    ORG_UPDATED = "org_updated"
    ORG_DELETED = "org_deleted"
    KEY_CREATED = "key_created"
    KEY_UPDATED = "key_updated"
    KEY_DISABLED = "key_disabled"
    KEY_ENABLED = "key_enabled"
    KEY_ROTATED = "key_rotated"
    KEY_DELETED = "key_deleted"
    LIMIT_CHANGED = "limit_changed"
    LIMIT_RAISED = "limit_raised"
    CYCLE_RESET = "cycle_reset"
    PRICING_CHANGED = "pricing_changed"
    #: The gate's catch-all for an /api write no handler audited explicitly.
    API_WRITE = "api_write"
    SHELL_OPENED = "shell_opened"
    SHELL_CLOSED = "shell_closed"
    PROVIDER_CREATED = "provider_created"
    PROVIDER_UPDATED = "provider_updated"
    PROVIDER_DELETED = "provider_deleted"
    SETTINGS_CHANGED = "settings_changed"
    ENROLLMENT_MINTED = "enrollment_minted"
    ENROLLMENT_REVOKED = "enrollment_revoked"
    DEPLOYMENT_LAUNCHED = "deployment_launched"
    DEPLOYMENT_STOPPED = "deployment_stopped"
    NODE_REMOVED = "node_removed"
    NODE_ADMITTED = "node_admitted"
    NODE_PROCESS_KILLED = "node_process_killed"
    ROUTING_CHANGED = "routing_changed"
    STORAGE_DELETED = "storage_deleted"
    SESSIONS_REVOKED = "sessions_revoked"
    #: A node joined or re-joined; ``before.agent_url`` when it moved.
    NODE_JOINED = "node_joined"
    #: A ChatGPT sign-in finished on the redirect and created or rebound a provider.
    PROVIDER_SIGNED_IN = "provider_signed_in"
    PROFILE_CHANGED = "profile_changed"
    DOWNLOAD_CHANGED = "download_changed"
    BENCHMARK_CHANGED = "benchmark_changed"
    # Security events: refusals written as rows rather than left as silence,
    # at most one per (action, actor, ip) a minute, counted.
    LOGIN_LOCKED = "login_locked"
    KEY_REFUSED = "key_refused"
    SESSION_REFUSED = "session_refused"
    SETUP_REFUSED = "setup_refused"
    JOIN_REJECTED = "join_rejected"
    CROSS_ORIGIN_REFUSED = "cross_origin_refused"
    #: An address past its noise budget kept asking the edge for something it
    #: does not serve; one folded row per address bucket per minute counts it.
    EDGE_REFUSED = "edge_refused"
    #: A live session was used from an address or user agent it was not last seen on.
    SESSION_IP_CHANGED = "session_ip_changed"
    #: The audit hash chain failed verification (actor ``system``).
    AUDIT_CHAIN_BROKEN = "audit_chain_broken"
    #: A node agent refused a token-gated call (journalled agent-side).
    AGENT_REFUSED = "agent_refused"
    #: The cluster join token was replaced (never the token itself in the row).
    CLUSTER_TOKEN_ROTATED = "cluster_token_rotated"
    #: A node runtime's model was loaded or unloaded (the resident set changed).
    NODE_RUNTIME_MODEL_CHANGED = "node_runtime_model_changed"


@dataclass(frozen=True)
class Admin:
    username: str
    #: ``None`` only on an admin created before schema v2; new admins need one.
    email: str | None
    created_at: float
    created_by: str | None


@dataclass(frozen=True)
class Session:
    #: The sha256 of the cookie value -- the cookie itself is never stored.
    session_id: str
    username: str
    created_at: float
    last_seen_at: float
    expires_at: float
    #: Where the session was last seen from (from v4; the login address before).
    ip: str | None
    #: What the browser last said it was; None on sessions from before v3.
    user_agent: str | None = None
    #: Where it signed in. Fixed for the session's life; ``ip`` moves.
    login_ip: str | None = None


@dataclass(frozen=True)
class Org:
    org_id: str
    name: str
    created_at: float
    created_by: str
    #: ``None`` = whatever ``/v1/models`` exposes right now.
    allowed_models: list[str] | None = None
    limits: dict[str, float] = field(default_factory=dict)
    default_key_limits: dict[str, float] = field(default_factory=dict)
    default_key_expiry_days: int | None = None
    routing_policy: RoutingPolicy | None = None
    deleted_at: float | None = None


@dataclass(frozen=True)
class ApiKey:
    key_id: str
    name: str
    org_id: str
    created_by: str
    created_at: float
    expires_at: float | None
    status: KeyStatus
    disabled_at: float | None = None
    disabled_by: str | None = None
    disabled_reason: str | None = None
    last_used_at: float | None = None
    last_used_ip: str | None = None
    notes: str = ""
    is_admin_key: bool = False
    admin_username: str | None = None
    #: ``None`` = inherit the org's list.
    allowed_models: list[str] | None = None
    routing_policy: RoutingPolicy | None = None
    limits: dict[str, float] = field(default_factory=dict)
    #: A LOCAL served name to re-dispatch to when a cloud pool is exhausted.
    cloud_fallback_local_model: str | None = None
    deleted_at: float | None = None


@dataclass(frozen=True)
class MintedKey:
    """The one shape that carries a secret. Returned by create and rotate,
    shown once, never stored, logged or listed."""

    key: ApiKey
    secret_once: str


@dataclass(frozen=True)
class Principal:
    key: ApiKey
    org: Org
    via: PrincipalVia
    ip: str | None
    user_agent: str | None


@dataclass(frozen=True)
class LimitState:
    limit_type: str
    scope: LimitScope
    scope_id: str
    limit: float
    #: The configured limit, or this cycle's raise when there is one.
    effective_limit: float
    used: float
    remaining: float
    resets_at: float | None
    fraction: float
    near: bool
    at: bool


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    status: int = 200
    code: str = ""
    message: str = ""
    limit_source: str | None = None
    used: float | None = None
    limit: float | None = None
    resets_at: float | None = None
    inject_max_tokens: int | None = None
    fallback_model: str | None = None


@dataclass(frozen=True)
class Reservation:
    """Opaque to everything but the limit service that issued it."""

    reservation_id: str
    key_id: str
    org_id: str
    model: str
    started_at: float
    estimated_prompt: int
    estimated_output: int
    #: The in-memory windows this reservation occupies (concurrency slots,
    #: rpm/tpm entries), so settle and release free exactly those.
    slots: tuple[str, ...] = ()


@dataclass(frozen=True)
class UsageActuals:
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None
    cost_basis: CostBasis
    target_kind: str
    model_served: str


@dataclass(frozen=True)
class AuditEntry:
    id: int
    ts: float
    actor: str
    action: AuditAction
    target_kind: str
    target_id: str
    before: dict | None
    after: dict | None
    ip: str | None
    #: The gate's ``r-<12 hex>`` id, also on the response's ``x-request-id``
    #: and on the /v1 ``requests`` row. None on rows written before schema v3.
    request_id: str | None = None
    user_agent: str | None = None
    #: First 12 hex of the session's sha256 -- joins to ``sessions.session_id``.
    session_id: str | None = None
    status: int | None = None


@dataclass(frozen=True)
class LocalPrice:
    model: str
    input_usd_per_mtok: float | None
    output_usd_per_mtok: float | None
    set_by: str
    set_at: float


class AuthError(Exception):
    """A refusal with one of :class:`AuthErrorCode`'s codes."""

    def __init__(
        self, code: AuthErrorCode, message: str = "", retry_after_s: float | None = None
    ) -> None:
        super().__init__(message or code.value)
        self.code = code
        self.message = message or code.value
        self.retry_after_s = retry_after_s


class InvalidCredentials(AuthError):
    def __init__(self, message: str = "invalid username or password") -> None:
        super().__init__(AuthErrorCode.INVALID_CREDENTIALS, message)


class LoginLocked(AuthError):
    def __init__(self, retry_after_s: float) -> None:
        super().__init__(
            AuthErrorCode.LOGIN_LOCKED,
            f"too many failed logins; try again in {int(retry_after_s + 0.999)}s",
            retry_after_s,
        )


class LimitExceedsOrg(AuthError):
    def __init__(self, field_name: str, message: str = "") -> None:
        super().__init__(
            AuthErrorCode.LIMIT_EXCEEDS_ORG,
            message or f"{field_name} exceeds the org's {field_name}",
        )
        self.field = field_name


class AllowlistExceedsOrg(AuthError):
    def __init__(self, models: list[str]) -> None:
        super().__init__(
            AuthErrorCode.ALLOWLIST_EXCEEDS_ORG,
            "allowed_models names models the org does not allow: " + ", ".join(models),
        )
        self.models = models


class AuthUnavailable(AuthError):
    def __init__(self, message: str = "the auth store could not be opened") -> None:
        super().__init__(AuthErrorCode.AUTH_UNAVAILABLE, message)


@dataclass(frozen=True)
class ChainReport:
    """``AuthService.verify_chain``: whether every hash-chained audit row
    (schema v3 onwards) still hashes to what was written, and links to the
    row before it. ``first_bad_id`` is the first row that does not; rows
    written before v3 carry no hash and are not counted.

    ``anchor`` says what the newest row was checked against: ``"head-file"``
    (``<logs dir>/audit/HEAD``, which catches deleting the newest rows) or
    ``"none"`` (only the chain itself, which cannot)."""

    ok: bool
    rows: int
    first_bad_id: int | None
    head_hash: str | None
    anchor: str | None = None


@runtime_checkable
class AuthPort(Protocol):
    """Every mutating method also takes the request context an audit row
    carries -- ``request_id``, ``user_agent``, ``session_id`` (all default
    None) -- beside ``ip``; ``gateway/authz.request_context`` builds all four."""

    def admin_exists(self) -> bool: ...
    def create_admin(
        self,
        username: str,
        password: str,
        *,
        email: str | None,
        created_by: str | None,
        ip: str | None,
        request_id: str | None = None,
        user_agent: str | None = None,
        session_id: str | None = None,
    ) -> Admin: ...
    def create_first_admin(
        self,
        username: str,
        password: str,
        *,
        email: str | None,
        ip: str | None,
        request_id: str | None = None,
        user_agent: str | None = None,
        session_id: str | None = None,
    ) -> Admin:
        """``create_admin`` with ``created_by=None`` that raises a
        ``ValueError`` (``AdminExists``) when any admin exists, decided in
        the same write transaction as the insert."""
        ...
    def list_admins(self) -> list[Admin]: ...
    def remove_admin(
        self,
        username: str,
        *,
        actor: str,
        ip: str | None,
        request_id: str | None = None,
        user_agent: str | None = None,
        session_id: str | None = None,
    ) -> None: ...
    def change_password(
        self,
        username: str,
        old_password: str,
        new_password: str,
        *,
        actor: str,
        ip: str | None,
        request_id: str | None = None,
        user_agent: str | None = None,
        session_id: str | None = None,
    ) -> None: ...
    def login(
        self,
        username: str,
        password: str,
        *,
        ip: str | None,
        request_id: str | None = None,
        user_agent: str | None = None,
        session_id: str | None = None,
    ) -> tuple[Session, str]: ...
    def login_delay_s(self, ip: str | None, username: str | None = None) -> float: ...
    def reauthenticate(
        self,
        username: str,
        password: str,
        *,
        ip: str | None,
        request_id: str | None = None,
        user_agent: str | None = None,
        session_id: str | None = None,
    ) -> None: ...
    def session(self, cookie_value: str, *, now: float | None = None) -> Session | None: ...
    def logout(
        self,
        cookie_value: str,
        *,
        ip: str | None,
        request_id: str | None = None,
        user_agent: str | None = None,
        session_id: str | None = None,
    ) -> None: ...
    def revoke_all(
        self,
        username: str,
        *,
        actor: str,
        ip: str | None,
        request_id: str | None = None,
        user_agent: str | None = None,
        session_id: str | None = None,
    ) -> int: ...
    def verify_chain(self) -> ChainReport: ...
    def head_hash(self) -> tuple[str | None, int]: ...
    def audit(
        self,
        actor: str,
        action: AuditAction,
        target_kind: str,
        target_id: str,
        *,
        before: dict | None = None,
        after: dict | None = None,
        ip: str | None = None,
        request_id: str | None = None,
        user_agent: str | None = None,
        session_id: str | None = None,
        status: int | None = None,
    ) -> None: ...
    def audit_page(
        self,
        *,
        start: float | None = None,
        end: float | None = None,
        actor: str = "",
        action: str = "",
        target: str = "",
        page: int = 1,
        page_size: int = 100,
    ) -> tuple[list[AuditEntry], int]: ...


@runtime_checkable
class KeyPort(Protocol):
    def create_org(self, name: str, *, actor: str, **fields) -> Org: ...
    def get_org(self, org_id: str) -> Org | None: ...
    def list_orgs(self, *, include_deleted: bool = False) -> list[Org]: ...
    def update_org(self, org_id: str, *, actor: str, **fields) -> Org: ...
    def delete_org(self, org_id: str, *, actor: str) -> None: ...
    def default_org_id(self) -> str: ...
    def create_key(
        self,
        *,
        name: str,
        org_id: str,
        created_by: str,
        expiry_days: int | None,
        limits: dict[str, float] | None = None,
        allowed_models: list[str] | None = None,
        routing_policy: RoutingPolicy | None = None,
        notes: str = "",
        is_admin_key: bool = False,
        admin_username: str | None = None,
        cloud_fallback_local_model: str | None = None,
    ) -> MintedKey: ...
    def get_key(self, key_id: str) -> ApiKey | None: ...
    def list_keys(
        self, *, org_id: str | None = None, include_deleted: bool = False
    ) -> list[ApiKey]: ...
    def authenticate(
        self, raw_key: str, *, ip: str | None, now: float | None = None
    ) -> ApiKey: ...
    def update_key(self, key_id: str, *, actor: str, **fields) -> ApiKey: ...
    def disable_key(self, key_id: str, *, actor: str, reason: str | None = None) -> ApiKey: ...
    def enable_key(self, key_id: str, *, actor: str) -> ApiKey: ...
    def rotate_key(self, key_id: str, *, actor: str) -> MintedKey: ...
    def delete_key(self, key_id: str, *, actor: str) -> None: ...
    def sweep_expired(self, *, now: float | None = None) -> list[str]: ...
    def effective_allowlist(self, key: ApiKey, org: Org, exposed: list[str]) -> list[str]: ...
    def validate_against_org(
        self,
        *,
        org: Org,
        limits: dict[str, float],
        allowed_models: list[str] | None,
        exposed: list[str],
    ) -> None: ...


@runtime_checkable
class PricingPort(Protocol):
    def get(self, model: str) -> LocalPrice | None: ...
    def list(self) -> list[LocalPrice]: ...
    def set(
        self,
        model: str,
        input_usd_per_mtok: float | None,
        output_usd_per_mtok: float | None,
        *,
        actor: str,
    ) -> LocalPrice: ...
    def clear(self, model: str, *, actor: str) -> None: ...
    def cost(
        self,
        *,
        model: str,
        target_kind: str,
        prompt_tokens: int,
        completion_tokens: int,
        metered_usd: float | None,
        provider_in_per_mtok: float | None,
        provider_out_per_mtok: float | None,
    ) -> tuple[float | None, CostBasis]: ...


@runtime_checkable
class LimitPort(Protocol):
    def check_and_reserve(
        self,
        principal: Principal,
        *,
        model: str,
        target_kind: str | None,
        body: dict,
        now: float | None = None,
        route_limits: dict | None = None,
        remote_rates: tuple[float, float] | None = None,
    ) -> tuple[LimitDecision, Reservation | None]: ...
    def settle(
        self, reservation: Reservation, actuals: UsageActuals | None, *, now: float | None = None
    ) -> None: ...
    def mark_started(self, reservation: Reservation) -> None: ...
    def check_target_kind(
        self, reservation: Reservation, target_kind: str, *, now: float | None = None
    ) -> LimitDecision: ...
    def states(
        self, scope: LimitScope, scope_id: str, *, now: float | None = None
    ) -> list[LimitState]: ...
    def raise_for_cycle(
        self,
        scope: LimitScope,
        scope_id: str,
        limit_type: str,
        value: float,
        *,
        actor: str,
        now: float | None = None,
    ) -> LimitState: ...
    def reset_cycle(
        self, scope: LimitScope, scope_id: str, *, actor: str, now: float | None = None
    ) -> None: ...
    def cycle_bounds(self, anchor: float, now: float) -> tuple[float, float]: ...
