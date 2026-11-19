"""The provider service: registry, discovery, forwarding, health, spend.

Implements :class:`~control_plane.contracts.ports.ProviderPort` and the three
extras the gateway routes on: :meth:`ProviderService.forward`,
:meth:`ProviderService.route_targets`, :meth:`ProviderService.spend_today`.

The shape of the thing: the cluster is the default and a paid API is the
overflow valve. Everything here exists so that a remote upstream can sit in a
``RoutingConfig`` next to a local deployment and be chosen or skipped on the
same terms.
"""

from __future__ import annotations

import asyncio
import copy
import json
import hmac
import ipaddress
import logging
import math
import re
import time
import threading
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ..contracts.providers import Provider, ProviderKind, ProviderModel
from ..netliteral import is_numeric_host_literal
from ..contracts.routing import RouteTarget, TargetKind
from . import chatgpt_sign_in as siwc
from . import claude_sign_in as claude
from .config import (
    CHATGPT_DISCOVERY_URL,
    CHATGPT_PLAN_SCOPE,
    CHATGPT_REDIRECT_URI,
    CHATGPT_RESOURCE,
    CHATGPT_TOKEN_URL,
    CLAUDE_CLIENT_ID,
    CLAUDE_REDIRECT_URI,
    CLAUDE_TOKEN_URL,
    CLAUDE_PROFILE_URL,
    CLAUDE_SCOPES,
    COST_BLEND_INPUT_WEIGHT,
    COST_BLEND_OUTPUT_WEIGHT,
    CONNECT_TIMEOUT_S,
    DISCOVERY_TIMEOUT_S,
    ERROR_BODY_LIMIT_BYTES,
    OAUTH_FILE,
    PROVIDER_REFRESH_S,
    READ_TIMEOUT_S,
    REAUTH_ERROR_CODES,
    SERVER_ERROR_RETRIES,
    SIGN_IN_ATTEMPT_TTL_S,
    SPEND_PERSIST_INTERVAL_S,
    STREAM_READ_TIMEOUT_S,
    TOKEN_REFRESH_SKEW_S,
    SIGN_OUT_RETRIES,
    WRITE_TIMEOUT_S,
    data_dir,
)
from .discovery import parse_endpoints, parse_models, recognized_envelope
from .errors import (
    PullRefusedError,
    PullUnsupportedError,
    AdapterUnsupportedError,
    MissingKeyError,
    ProviderError,
    ProviderNotAdmittingError,
    SignInError,
    UnknownProviderError,
    UpstreamError,
)
from .kinds import ChatWire, KindSpec, auth_headers, join_url, native_base, spec_for
from .oauth_store import OAuthStore, SignInRecord
from .responses_adapter import ResponsesToChat, chat_to_responses
from .anthropic_adapter import MessagesToChat, chat_to_messages
from .runtime import ProviderRuntime, jittered_delay, parse_retry_after, utc_day
from .secrets import (
    MINTED_REF_RE,
    OPERATOR_REF_RE,
    Redactor,
    SecretRedactingFilter,
    SecretStore,
    has_known_key_shape,
    looks_like_secret,
)
from .serialization import assert_no_key_material, provider_public_dict
from .store import ProviderStore
from .usage import Usage, UsageSniffer

log = logging.getLogger(__name__)

#: What a sign-in credential is called wherever a key's reference would be named.
_SIGN_IN_REF = "ChatGPT sign-in"

#: The one endpoint a Responses-wire kind serves; everything else needs a raw key.
_RESPONSES_ONLY = "ChatGPT plan providers serve /v1/chat/completions only"
_CLAUDE_ONLY = "Claude subscription providers serve /v1/chat/completions only"

#: Client body fields that choose a different model or backend upstream
#: (OpenRouter's fallback list and provider routing). Never forwarded (K-2).
_CLIENT_ROUTING_FIELDS = frozenset({"models", "route", "fallbacks", "provider"})

#: Kinds that name a box the operator chose -- a LAN Ollama, any
#: OpenAI-compatible server. Plain http and loopback/RFC1918 hosts are theirs
#: to use; every other kind has one home and speaks https to it.
_LAN_KINDS = frozenset({ProviderKind.OLLAMA, ProviderKind.CUSTOM})

#: IPv4 link-local, where every cloud metadata service answers.
_LINK_LOCAL_V4 = ipaddress.ip_network("169.254.0.0/16")


def validate_base_url(kind: ProviderKind, url: str) -> str:
    """Refuse a ``base_url`` the provider's stored credential must not go to.

    The provider's key is sent as a bearer to whatever host this names, and a
    failed answer's body is echoed into ``last_error`` -- so an unchecked URL
    is both an SSRF and a way to walk off with the key (finding K3). The
    rules, each named in the refusal:

    * ``https`` unless the kind is OLLAMA or CUSTOM;
    * an IP-literal host is never link-local (``169.254.0.0/16``,
      ``fe80::/10``), multicast or unspecified -- no DNS lookup is made;
    * a kind with a fixed home (``KindSpec.base_url``, every kind but
      OLLAMA/CUSTOM) must keep that home's hostname.

    Returns ``url`` unchanged; raises ``ValueError``.
    """
    kind = ProviderKind(kind)
    if kind is ProviderKind.CLAUDE_CODE and url != spec_for(kind).base_url:
        raise ValueError("claude_code base_url must be https://api.anthropic.com/v1")
    lan = kind in _LAN_KINDS
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    allowed = ("https", "http") if lan else ("https",)
    if scheme not in allowed:
        raise ValueError(
            f"{kind.value} base_url must use {' or '.join(allowed)}, not {scheme or 'no scheme'!r}"
        )
    try:
        host = parts.hostname
    except ValueError:
        host = None
    if not host:
        raise ValueError("base_url names no host")
    if is_numeric_host_literal(host):
        raise ValueError("base_url host is not a dotted-quad address or a hostname")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        mapped = getattr(address, "ipv4_mapped", None)
        if mapped is not None:
            address = mapped
        if (
            address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or (address.version == 4 and address in _LINK_LOCAL_V4)
        ):
            raise ValueError(
                f"base_url host {host} is a link-local, multicast or unspecified address"
            )
    home = spec_for(kind).base_url
    if home and not lan:
        home_host = (urlsplit(home).hostname or "").lower()
        if host.lower() != home_host:
            raise ValueError(f"{kind.value} base_url must be on {home_host}, not {host}")
    return url


def _screen_free_text(value: str, field: str) -> None:
    """Refuse key-shaped material in a field that is echoed and persisted.

    display_name and aliases appear verbatim in every provider listing and
    in providers.json; the reference/URL fields already get this screen and
    these must not be the two that dodge it.
    """
    if looks_like_secret(value):
        raise ValueError(
            f"{field} looks like it contains key material; provider names and "
            "aliases are displayed and persisted verbatim, so paste the key's "
            "REFERENCE (an env var or secrets.json name), never the key"
        )

def _screen_passthrough(value: str, field: str) -> None:
    """Refuse key-shaped material in a field that is forwarded, not stored.

    Narrower than :func:`_screen_free_text` on purpose. A model name is not a
    credential slot: it is handed to the upstream and never persisted or
    echoed in a listing, so the entropy fallback buys nothing here and costs
    every GGUF repository whose name happens to carry a 32-character run --
    all 96 variants of Mistral-Small-24B-Instruct-2501, none of Qwen2.5's.
    A pasted key is still caught by its vendor prefix.
    """
    if has_known_key_shape(value):
        raise ValueError(
            f"{field} looks like it contains key material; it is sent on to "
            "the provider, so pass the model's name and keep credentials in "
            "the provider's api_key_ref"
        )


_ID_SAFE = re.compile(r"[^a-z0-9._-]+")

#: The shape of a reference derate mints for itself when a key is pasted rather
#: than named. The prefix marks the secrets.json entry as ours, which is the
#: only reason removing a provider can safely delete it: an operator's own name
#: may be shared with something else and is never touched.
MINTED_REF_PREFIX = "DERATE_"
MINTED_REF_SUFFIX = "_API_KEY"
#: The UI reads a reference as a *name* only up to this length and renders
#: anything longer as key material (``ui/src/api/redact.ts``). A minted name
#: that overflowed would display as ``***`` -- derate's own reference, hidden
#: from the operator as though it had leaked.
_MAX_REF_LEN = 64
_REF_UNSAFE = re.compile(r"[^A-Za-z0-9]+")

#: The refusal for a key pasted into the field that takes a name. One string
#: rather than three copies kept word-for-word by hand: add, update and the
#: day-0 stub all screen the same field for the same mistake, and a refusal
#: that reads differently in each reads as three different rules.
KEY_IN_REF_FIELD = (
    "api_key_ref must be the NAME of an environment variable or "
    "secrets.json key, not the key itself -- send the key as "
    '"api_key" and it will be stored in secrets.json under a '
    "reference for you"
)


def minted_ref(provider_id: str) -> str:
    """The secrets.json name a pasted key is stored under for this provider.

    Uppercase throughout, which is also what keeps it clear of
    :func:`looks_like_secret`: that predicate wants a long run containing both
    cases and a digit, and a name shaped like this has no lowercase at all.
    """
    stem = _REF_UNSAFE.sub("_", provider_id).strip("_").upper() or "PROVIDER"
    budget = _MAX_REF_LEN - len(MINTED_REF_PREFIX) - len(MINTED_REF_SUFFIX)
    return f"{MINTED_REF_PREFIX}{stem[:budget]}{MINTED_REF_SUFFIX}"


#: The one field ``POST /api/wiring`` will not carry: a pasted key is written
#: to secrets.json, which neither a dry run nor a batch may do.
WIRING_NO_API_KEY = "api_key cannot be set through /api/wiring; use PATCH /api/providers/{id}."

REF_NOT_OURS = (
    "api_key_ref must be a derate-minted reference or DERATE_SECRET_*: "
    'send the key as "api_key" and derate stores it under this provider\'s '
    "own reference, or export it as DERATE_SECRET_<NAME> and name that"
)


def ref_allowed(ref: str, provider_id: str) -> bool:
    """Whether *provider_id* may hold the reference *ref*.

    Exactly its own minted name, or an operator's ``DERATE_SECRET_*``. Any
    other environment variable -- the cluster token, the HF token, another
    provider's minted key -- would be sent as a bearer to this provider's
    ``base_url``, which the same admin chose.
    """
    if ref == minted_ref(provider_id):
        return True
    return bool(OPERATOR_REF_RE.fullmatch(ref)) and not MINTED_REF_RE.fullmatch(ref)


def parse_daily_budget(value) -> float | None:
    """A daily budget, or None for unlimited. NaN would disable it silently."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"daily_budget_usd must be a number or null, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"daily_budget_usd must be a number or null, got {value!r}") from None
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"daily_budget_usd must be a finite number >= 0, got {value!r}")
    return number


#: Endpoints whose request body has a `stream` field. Everything else -- today
#: that means /v1/audio/speech and /v1/audio/transcriptions -- must not have one
#: added, because a strict upstream rejects an unknown field outright.
_STREAMING_ENDPOINTS = ("chat/completions", "completions")


def _accepts_stream(endpoint: str) -> bool:
    tail = endpoint.strip("/")
    return any(tail.endswith(name) for name in _STREAMING_ENDPOINTS)


def _json_object(raw: bytes) -> dict:
    """A response body as a JSON object, or {} when it is not one."""
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _oauth_error_code(payload: dict) -> str:
    """RFC 6749's ``error`` string, or the ``code`` of an object-shaped one."""
    error = payload.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        return str(error.get("code") or error.get("type") or "")
    return ""


def estimate_prompt_tokens(body: dict) -> int:
    """Character-based prompt estimate (4 chars/token, 4 tokens/message).

    Stand-in for ``limits.service.estimate_prompt_tokens`` so this branch does
    not depend on the auth-keys-limits branch; the usage fallback only needs a
    rough figure when an upstream sent no usage block.
    """
    chars = 0
    overhead = 0
    messages = body.get("messages")
    if isinstance(messages, list):
        for message in messages:
            overhead += 4
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str):
                chars += len(content)
            elif isinstance(content, list):
                chars += sum(
                    len(part["text"])
                    for part in content
                    if isinstance(part, dict) and isinstance(part.get("text"), str)
                )
            elif content is not None:
                chars += len(str(content))
    for key in ("prompt", "input"):
        value = body.get(key)
        if isinstance(value, str):
            chars += len(value)
        elif isinstance(value, list):
            chars += sum(len(i) for i in value if isinstance(i, str))
    return int(chars / 4.0) + overhead


@dataclass
class UpstreamResponse:
    """A live upstream response, headers known, body not yet read."""

    status_code: int
    headers: dict[str, str]
    media_type: str | None
    body: AsyncIterator[bytes]
    # Logical failure discovered while translating a stream whose HTTP headers
    # have already been sent. Set before the corresponding error frame yields.
    error: UpstreamError | None = None


#: The only upstream response headers a client is ever shown (finding K6).
#: An allowlist, not a denylist: a provider's ``openai-organization``,
#: ``openai-project`` and account-wide ``x-ratelimit-*`` describe derate's
#: account with the provider, never the key holder's. ``x-request-id`` is not
#: here because it is replaced by derate's own trace id, never relayed.
RESPONSE_HEADER_ALLOWLIST = frozenset({"content-type", "cache-control", "retry-after"})


@dataclass
class _Entry:
    provider: Provider
    runtime: ProviderRuntime
    spec: KindSpec
    # Set when we disabled the provider ourselves for an unresolvable key, so
    # we know it is ours to re-enable once the reference resolves.
    auto_disabled: bool = False
    models_by_upstream: dict[str, ProviderModel] = field(default_factory=dict)
    # upstream_id -> (input, output) as the catalogue publishes it, before a
    # backend pin's host price replaces it on the model. What a cleared pin
    # restores, and what is persisted: the file never holds a host's price
    # in the catalogue's place.
    catalogue_prices: dict[str, tuple[float | None, float | None]] = field(
        default_factory=dict
    )

    def reindex(self) -> None:
        self.models_by_upstream = {m.upstream_id: m for m in self.provider.models}

    def capture_catalogue_prices(self) -> None:
        """Record the models' prices as the catalogue's own. Call only on a
        model list fresh from the upstream or from disk, never after pinning."""
        self.catalogue_prices = {
            m.upstream_id: (m.input_cost_per_mtok, m.output_cost_per_mtok)
            for m in self.provider.models
        }


def existing_provider_for(providers: list[Provider], base_url: str) -> Provider | None:
    """A provider already registered against *base_url*, or None.

    Compared on the URL rather than on the node, because that is what makes
    two entries a duplicate as far as routing is concerned. Trailing slashes
    are normalised; nothing else is, since a different port or scheme really
    is a different upstream. Shared by the manual ``POST /api/nodes/{id}/runtime``
    route and the background auto-adopt loop so the two cannot drift apart.
    """
    want = (base_url or "").rstrip("/")
    for p in providers or []:
        if str(getattr(p, "base_url", "") or "").rstrip("/") == want:
            return p
    return None


class ProviderService:
    """Remote OpenAI-compatible upstreams as first-class route targets."""

    def __init__(
        self,
        *,
        data_path: Path | None = None,
        store: ProviderStore | None = None,
        secrets: SecretStore | None = None,
        oauth: OAuthStore | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        now: Callable[[], float] = time.time,
        refresh_interval_s: float = PROVIDER_REFRESH_S,
        events: Any = None,
    ) -> None:
        root = Path(data_path) if data_path is not None else data_dir()
        # Optional, and degrades: a service constructed without one accounts
        # exactly as before and simply reports no budget crossings.
        self._events = events
        #: provider_id -> the budget sentence last reported, so a CROSSING is
        #: emitted once and a standing condition is not re-emitted per request.
        self._budget_state: dict[str, str | None] = {}
        self.redactor = Redactor()
        self.secrets = secrets or SecretStore(root / "secrets.json", redactor=self.redactor)
        # Share one redactor so a value resolved anywhere is scrubbed everywhere.
        self.redactor = self.secrets.redactor
        self.store = store or ProviderStore(root / "providers.json", redactor=self.redactor)
        self.store.redactor = self.redactor
        self.oauth = oauth or OAuthStore(root / OAUTH_FILE, redactor=self.redactor)
        self.oauth.redactor = self.redactor
        # Per provider, so two requests finding the same expired token refresh
        # it once: the refresh token rotates, and the loser would present a
        # spent one and be told to sign in again.
        self._token_locks: dict[str, asyncio.Lock] = {}
        self._sign_ins: dict[str, siwc.SignInAttempt] = {}
        # Finished attempts, kept for the UI's poll; and the ones in flight past
        # their state check, so a refusal there is recorded rather than lost.
        self._sign_in_outcomes: dict[str, dict] = {}
        self._consumed: set[str] = set()
        self._siwc_discovery: dict | None = None
        self._claude_sign_ins: dict[str, claude.ClaudeSignInAttempt] = {}
        self._claude_outcomes: dict[str, dict] = {}
        self._claude_credential_lock = threading.RLock()
        self._now = now
        self._refresh_interval_s = refresh_interval_s
        self._client_factory = client_factory or self._default_client_factory
        self._clients: dict[int, httpx.AsyncClient] = {}
        self._entries: dict[str, _Entry] = {}
        self._refresh_task: asyncio.Task | None = None
        self._spend_dirty = False
        self._spend_persisted_at = 0.0
        self._install_redacting_filter()
        self._load()

    # -- construction helpers ---------------------------------------------

    def _install_redacting_filter(self) -> None:
        """Attach the redaction backstop to every logger in this package.

        A :class:`logging.Filter` attached to a ``Logger`` only runs for
        records logged directly through *that* logger object — it is never
        consulted for children in the hierarchy, only for handlers walked
        during propagation. Every module here logs via
        ``logging.getLogger(__name__)``, i.e. a *child* of the package
        logger, so attaching the filter to ``logging.getLogger(__package__)``
        alone leaves it inert for every one of those records. Fix: walk the
        logger manager's registry for every logger already created under
        this package's prefix (all submodules are imported by the time a
        service is constructed) and attach directly to each.
        Idempotent — replaces rather than stacks, so constructing several
        services does not accumulate filters — and safe to call again later
        if a submodule logger were somehow created after the fact.
        """
        package_prefix = __package__ or ""
        redacting_filter = SecretRedactingFilter(self.redactor)
        loggers: dict[str, logging.Logger] = {package_prefix: logging.getLogger(package_prefix)}
        # Snapshot: another thread's getLogger() mutates this dict mid-walk.
        for name, candidate in list(logging.Logger.manager.loggerDict.items()):
            if not isinstance(candidate, logging.Logger):
                continue  # a logging.PlaceHolder, not a real logger
            if name == package_prefix or name.startswith(package_prefix + "."):
                loggers[name] = candidate
        for logger_obj in loggers.values():
            for existing in list(logger_obj.filters):
                if isinstance(existing, SecretRedactingFilter):
                    logger_obj.removeFilter(existing)
            logger_obj.addFilter(redacting_filter)

    @staticmethod
    def _default_client_factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=CONNECT_TIMEOUT_S,
                read=READ_TIMEOUT_S,
                write=WRITE_TIMEOUT_S,
                pool=CONNECT_TIMEOUT_S,
            ),
            # A redirect would carry the bearer to wherever the upstream points.
            follow_redirects=False,
        )

    def _client(self) -> httpx.AsyncClient:
        """One client per event loop, so a sync call on a worker thread is safe."""
        loop_key = id(asyncio.get_running_loop())
        client = self._clients.get(loop_key)
        if client is None or client.is_closed:
            client = self._client_factory()
            self._clients[loop_key] = client
        return client

    def _load(self) -> None:
        for provider, runtime in self.store.load():
            entry = _Entry(provider=provider, runtime=runtime, spec=spec_for(provider.kind))
            entry.capture_catalogue_prices()
            self._apply_pin_prices(entry)
            entry.reindex()
            self._entries[provider.provider_id] = entry
            ref = provider.api_key_ref
            if ref and not ref_allowed(ref, provider.provider_id):
                log.warning(
                    "provider %s: api_key_ref %s is not derate-minted or DERATE_SECRET_*; "
                    "it will not be resolved until the provider is given a key again",
                    provider.provider_id,
                    ref,
                )
        # A missing key disables a provider. It never fails startup.
        for entry in self._entries.values():
            self._check_key(entry)
        if self._entries:
            log.info("loaded %d provider(s) from disk", len(self._entries))

    @staticmethod
    def _usable_ref(entry: _Entry) -> str:
        """The reference to resolve, or "" when the record holds one it may not."""
        ref = entry.provider.api_key_ref
        return ref if ref and ref_allowed(ref, entry.provider.provider_id) else ""

    def _sync(self, entry: _Entry) -> _Entry:
        """Mirror live runtime state onto the contract record before it is read.

        This is also where a failed provider goes half-open. `healthy` is what
        routing selects on (`gateway/targets.py`), so leaving it False forever
        made the block prevent its own cure: an unhealthy provider was never
        offered a request, and a successful request was the only thing that
        could mark it healthy again. One transient timeout took a box off the
        air for six hours -- until the next model-list refresh -- while it
        answered pings in 11 ms.

        Once the backoff expires we stop *asserting* it is down and let one
        request find out, exactly as the gateway's circuit breaker does at
        half-open. `last_error` is deliberately left in place, so a screen can
        still say what went wrong while the probe is out.
        """
        entry.runtime.half_open(self._now())
        entry.provider.healthy = entry.runtime.healthy
        entry.provider.last_error = entry.runtime.last_error
        entry.provider.last_refreshed = entry.runtime.last_refreshed
        return entry

    def _persist(self) -> None:
        self._spend_dirty = False
        self._spend_persisted_at = self._now()
        entries = [self._sync(e) for e in self._entries.values()]
        self.store.save([(self._persisted_view(e), e.runtime) for e in entries])

    @staticmethod
    def _persisted_view(entry: _Entry) -> Provider:
        """The record with the catalogue's prices, never a pinned host's.

        The pinned price is runtime state (``backend_pin_prices``) re-applied
        on load; writing it over the catalogue price would lose the price a
        cleared pin has to restore.
        """
        if not entry.runtime.backend_pin_prices:
            return entry.provider
        models = []
        for m in entry.provider.models:
            cost_in, cost_out = entry.catalogue_prices.get(
                m.upstream_id, (m.input_cost_per_mtok, m.output_cost_per_mtok)
            )
            models.append(replace(m, input_cost_per_mtok=cost_in, output_cost_per_mtok=cost_out))
        return replace(entry.provider, models=models)

    # -- keys --------------------------------------------------------------

    def _credential_label(self, entry: _Entry) -> str:
        """The name the runtime's messages give this provider's credential."""
        if entry.spec.sign_in:
            return "Claude subscription sign-in" if entry.spec.sign_in == "claude" else _SIGN_IN_REF
        return entry.provider.api_key_ref

    def _credential_present(self, entry: _Entry) -> bool:
        if entry.spec.sign_in:
            record = self.oauth.get(entry.provider.provider_id)
            return record is not None and bool(record.refresh_token) and not record.needs_reauth
        ref = self._usable_ref(entry)
        return bool(ref) and self.secrets.has(ref)

    def _check_key(self, entry: _Entry) -> bool:
        """Resolve the credential. Disable on a miss, re-enable on a return.

        For a sign-in kind the credential is the stored sign-in, present while
        it holds a refresh token that has not been refused.
        """
        if not entry.spec.sign_in and not entry.spec.requires_key and not entry.provider.api_key_ref:
            entry.runtime.clear_missing_key()
            return True
        label = self._credential_label(entry)
        if self._credential_present(entry):
            if entry.runtime.missing_key_ref is not None:
                entry.runtime.clear_missing_key()
                entry.runtime.healthy = True
                entry.runtime.last_error = None
                if entry.auto_disabled:
                    entry.provider.enabled = True
                    entry.auto_disabled = False
                    log.info(
                        "provider %s re-enabled; %s now resolves",
                        entry.provider.provider_id,
                        label,
                    )
            return True
        if entry.provider.enabled:
            entry.auto_disabled = True
        entry.provider.enabled = False
        entry.runtime.note_missing_key(label, sign_in=bool(entry.spec.sign_in))
        entry.provider.healthy = False
        entry.provider.last_error = entry.runtime.last_error
        log.warning(
            "provider %s disabled: %s does not resolve",
            entry.provider.provider_id,
            label,
        )
        return False

    def _store_key(self, ref: str, value: str) -> None:
        """Write a pasted key to secrets.json under ``ref``.

        The one thing this has to get right beyond the write itself:
        :meth:`SecretStore.get` reads the environment *before* the file, so an
        environment variable sharing this name would silently win over what was
        just stored and the provider would authenticate with a key nobody here
        chose. Check first and refuse, naming the variable and never its value.
        Checking before the write rather than after also means the refused call
        leaves no inert entry in the file.
        """
        shadow = self.secrets.env_ref(ref)
        if shadow is not None and shadow != value:
            raise ValueError(
                f"an environment variable named {ref} already resolves to a "
                "different value and takes precedence over secrets.json, so "
                "the key would be stored and then ignored; choose another "
                "reference name or unset it"
            )
        self.secrets.put(ref, value)

    def resolve_key(self, provider_id: str) -> str:
        """Request time only. The one place a value is ever produced."""
        entry = self._entry(provider_id)
        if entry.spec.sign_in:
            # The raw-key paths (multipart, realtime) would forward to an
            # endpoint the plan does not serve, with a token that expires
            # within the hour and is refreshed only by open_upstream.
            reason = _CLAUDE_ONLY if entry.spec.sign_in == "claude" else _RESPONSES_ONLY
            raise AdapterUnsupportedError(f"provider {provider_id!r}: {reason}")
        if not entry.spec.requires_key and not entry.provider.api_key_ref:
            return ""
        ref = self._usable_ref(entry)
        value = self.secrets.get(ref) if ref else None
        if value is None:
            raise MissingKeyError(provider_id, entry.provider.api_key_ref)
        return value

    def key_status(self, provider_id: str) -> dict:
        """Whether this provider's credential resolves, and from where.

        The honest answer to "is the key set?" in a product that has no reveal
        control and is not getting one. A reference is a name, and a name plus
        which of the two places answered to it is the whole of what can be said
        without producing key material: no value, no length, no prefix.

        The provenance is worth naming rather than collapsing into a boolean,
        because the two are not equally ours. A key in secrets.json is one this
        coordinator wrote and can replace. A key in the environment belongs to
        whatever started the process: pasting a replacement does not overwrite
        it -- :meth:`update` mints a reference and moves the provider onto it,
        and the variable stops being what authenticates. That is worth knowing
        before the paste, not after.
        """
        entry = self._entry(provider_id)
        if entry.spec.sign_in:
            record = self.oauth.get(provider_id)
            if record is None:
                return {"key_state": "missing", "key_source": None}
            if record.needs_reauth:
                return {"key_state": "reauth", "key_source": "oauth"}
            return {"key_state": "set", "key_source": "oauth"}
        ref = entry.provider.api_key_ref
        if ref and not self._usable_ref(entry):
            return {"key_state": "missing", "key_source": None}
        if not entry.spec.requires_key and not ref:
            return {"key_state": "not_needed", "key_source": None}
        if not ref:
            return {"key_state": "missing", "key_source": None}
        # Environment first, because that is the order resolution uses.
        if self.secrets.env_ref(ref) is not None:
            return {"key_state": "set", "key_source": "environment"}
        if self.secrets.has(ref):
            return {"key_state": "set", "key_source": "secrets.json"}
        return {"key_state": "missing", "key_source": None}

    def sign_in_account(self, provider_id: str) -> str | None:
        """The account a sign-in provider is signed in as, or None.

        An email address, which the operator needs to see to know whose plan
        a request is billed to, and which is not a credential.
        """
        entry = self._entry(provider_id)
        if not entry.spec.sign_in:
            return None
        record = self.oauth.get(provider_id)
        return record.email if record is not None else None

    async def _access_token(
        self, entry: _Entry, *, force: bool = False, client: httpx.AsyncClient | None = None,
        rejected_token: str | None = None,
    ) -> str:
        """A live access token for a sign-in provider, refreshed when near expiry.

        Serialised per provider: the refresh token rotates on every use, so two
        requests refreshing at once would leave the slower one holding a spent
        token -- which OpenAI answers as ``refresh_token_reused``, and that
        means signing in again. Inside the lock the record is re-read, so the
        second caller finds the first one's fresh token and does not refresh.
        """
        if entry.spec.sign_in == "claude":
            return await self._claude_access_token(
                entry, force=force, client=client, rejected_token=rejected_token
            )
        provider_id = entry.provider.provider_id
        lock = self._token_locks.setdefault(provider_id, asyncio.Lock())
        async with lock:
            record = self.oauth.get(provider_id)
            if record is None or not record.refresh_token or record.needs_reauth:
                self._check_key(entry)
                self._persist()
                raise self._upstream_error(
                    provider_id, 401, "ChatGPT sign-in is missing or expired; sign in "
                    "again from Settings → Providers"
                )
            now = self._now()
            if not force and record.expires_at - now > TOKEN_REFRESH_SKEW_S:
                return record.access_token

            client = client or self._client()
            try:
                response = await client.post(
                    CHATGPT_TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "client_id": record.client_id,
                        "refresh_token": record.refresh_token,
                        "resource": CHATGPT_RESOURCE,
                    },
                    headers={"Accept": "application/json"},
                    timeout=DISCOVERY_TIMEOUT_S,
                )
            except httpx.HTTPError as exc:
                raise self._upstream_error(
                    provider_id, 502,
                    f"could not refresh ChatGPT sign-in: {type(exc).__name__}",
                ) from None

            payload = _json_object(response.content)
            if response.status_code == 200 and isinstance(payload.get("access_token"), str):
                record.access_token = payload["access_token"]
                record.refresh_token = str(payload.get("refresh_token") or record.refresh_token)
                record.expires_at = now + float(payload.get("expires_in") or 3600)
                if isinstance(payload.get("scope"), str):
                    record.scopes = payload["scope"].split()
                record.saved_at = now
                self.oauth.put(record)
                return record.access_token

            if _oauth_error_code(payload) in REAUTH_ERROR_CODES:
                record.needs_reauth = True
                record.access_token = ""
                record.refresh_token = ""
                record.saved_at = now
                self.oauth.put(record)
                message = "ChatGPT sign-in expired; sign in again from Settings → Providers"
                entry.runtime.note_auth_failure(now, 401, _SIGN_IN_REF, message, sign_in=True)
                self._check_key(entry)
                self._persist()
                log.warning("provider %s: ChatGPT sign-in refused at refresh", provider_id)
                raise self._upstream_error(provider_id, 401, message)

            detail = self._error_message(response.status_code, response.content)
            raise self._upstream_error(
                provider_id, 502, f"could not refresh ChatGPT sign-in: {detail}"
            )

    @staticmethod
    def _claude_generation(record: SignInRecord | None) -> tuple | None:
        return None if record is None else (
            record.access_token, record.refresh_token, record.saved_at
        )

    def _claude_current(self, entry: _Entry, generation: tuple | None) -> bool:
        pid = entry.provider.provider_id
        return (
            self._entries.get(pid) is entry
            and self._claude_generation(self.oauth.get(pid)) == generation
        )

    @staticmethod
    def _claude_token_fields(payload: dict, *, refresh: bool = False) -> tuple[str, str, float]:
        access, rotating = payload.get("access_token"), payload.get("refresh_token")
        expiry = payload.get("expires_in")
        if (
            not isinstance(access, str) or not access.strip()
            or (not refresh and (not isinstance(rotating, str) or not rotating.strip()))
            or (refresh and rotating is not None and (
                not isinstance(rotating, str) or not rotating.strip()
            ))
            or isinstance(expiry, bool) or not isinstance(expiry, (int, float))
            or not math.isfinite(expiry) or expiry <= 0
        ):
            raise ValueError("Claude returned malformed access_token, refresh_token or expires_in")
        if "scope" in payload and (
            not isinstance(payload["scope"], str)
            or "user:inference" not in payload["scope"].split()
        ):
            raise ValueError("Claude did not grant user:inference; consent was not broadened")
        return access, rotating or "", float(expiry)

    async def _claude_access_token(
        self, entry: _Entry, *, force: bool, client: httpx.AsyncClient | None,
        rejected_token: str | None,
    ) -> str:
        pid = entry.provider.provider_id
        lock = self._token_locks.setdefault(pid, asyncio.Lock())
        async with lock:
            with self._claude_credential_lock:
                record = self.oauth.get(pid)
                if (
                    self._entries.get(pid) is not entry or record is None
                    or record.needs_reauth or not record.refresh_token
                ):
                    raise self._upstream_error(pid, 401, "Claude subscription sign-in is missing or expired; sign in again from Settings → Providers")
                now = self._now()
                if record.expires_at - now > TOKEN_REFRESH_SKEW_S and (
                    not force or (rejected_token is not None and record.access_token != rejected_token)
                ):
                    return record.access_token
                generation = self._claude_generation(record)
            try:
                response = await (client or self._client()).post(
                    CLAUDE_TOKEN_URL,
                    json={"grant_type": "refresh_token", "refresh_token": record.refresh_token,
                          "client_id": CLAUDE_CLIENT_ID},
                    headers={"Accept": "application/json"}, timeout=DISCOVERY_TIMEOUT_S,
                )
            except httpx.HTTPError as exc:
                raise self._upstream_error(pid, 502, f"could not refresh Claude subscription sign-in: {type(exc).__name__}") from None
            payload = _json_object(response.content)
            for field in ("access_token", "refresh_token"):
                self.redactor.remember(str(payload.get(field) or ""))
            with self._claude_credential_lock:
                if not self._claude_current(entry, generation):
                    newer = self.oauth.get(pid)
                    if (
                        self._entries.get(pid) is entry and newer is not None
                        and not newer.needs_reauth and newer.access_token
                        and newer.expires_at > self._now()
                    ):
                        return newer.access_token
                    raise self._upstream_error(pid, 401, "Claude subscription credential was removed or invalidated during refresh")
                if response.status_code == 200:
                    try:
                        access, rotating, expiry = self._claude_token_fields(payload, refresh=True)
                    except ValueError as exc:
                        raise self._upstream_error(pid, 502, str(exc)) from None
                    updated = replace(
                        record, access_token=access, refresh_token=rotating or record.refresh_token,
                        expires_at=self._now() + expiry, saved_at=self._now(),
                        scopes=payload["scope"].split() if "scope" in payload else record.scopes,
                    )
                    self.oauth.put(updated)
                    return access
                if response.status_code in (401, 403) or _oauth_error_code(payload) in {
                    "invalid_grant", "invalid_token",
                }:
                    self.oauth.put(replace(record, needs_reauth=True, access_token="",
                                           refresh_token="", saved_at=self._now()))
                    message = "Claude subscription sign-in expired; sign in again from Settings → Providers"
                    entry.runtime.note_auth_failure(self._now(), 401, self._credential_label(entry), message, sign_in=True)
                    self._check_key(entry)
                    self._persist()
                    raise self._upstream_error(pid, 401, message)
                detail = self._error_message(response.status_code, response.content)
                raise self._upstream_error(pid, 502, f"could not refresh Claude subscription sign-in: {detail}")

    def _prune_claude_sign_ins(self) -> None:
        now = self._now()
        for key, attempt in list(self._claude_sign_ins.items()):
            if now - attempt.created >= SIGN_IN_ATTEMPT_TTL_S:
                del self._claude_sign_ins[key]
        for key, outcome in list(self._claude_outcomes.items()):
            if now - outcome["at"] >= SIGN_IN_ATTEMPT_TTL_S:
                del self._claude_outcomes[key]

    def start_claude_sign_in(self, provider_id: str | None) -> dict:
        with self._claude_credential_lock:
            self._prune_claude_sign_ins()
            if provider_id is not None:
                entry = self._entry(provider_id)
                if entry.spec.sign_in != "claude":
                    raise ValueError(f"provider {provider_id!r} is not a Claude subscription provider")
                if self.oauth.get(provider_id) is None:
                    raise ValueError("This provider has no Claude sign-in to renew; remove it and sign in again")
            attempt = claude.new_attempt(provider_id=provider_id, now=self._now())
            self._claude_sign_ins[attempt.attempt_id] = attempt
            return {
                "attempt_id": attempt.attempt_id, "authorize_url": claude.authorize_url(attempt),
                "redirect_uri": CLAUDE_REDIRECT_URI,
                "expires_at": attempt.created + SIGN_IN_ATTEMPT_TTL_S,
            }

    def claude_sign_in_status(self, attempt_id: str) -> dict:
        with self._claude_credential_lock:
            self._prune_claude_sign_ins()
            if attempt_id in self._claude_sign_ins:
                return {"status": "pending"}
            outcome = self._claude_outcomes.get(attempt_id)
            if outcome is None:
                raise SignInError("sign_in_attempt_unknown", claude.ATTEMPT_UNKNOWN)
            return {k: v for k, v in outcome.items() if k != "at"}

    async def complete_claude_sign_in(self, attempt_id: str, callback_url: str) -> Provider:
        with self._claude_credential_lock:
            self._prune_claude_sign_ins()
            attempt = self._claude_sign_ins.get(attempt_id)
            if attempt is None:
                raise SignInError("sign_in_attempt_unknown", claude.ATTEMPT_UNKNOWN)
            params = claude.parse_callback(callback_url)
            self.redactor.remember(params["code"])
            if not hmac.compare_digest(params["state"].encode(), attempt.state.encode()):
                raise SignInError("sign_in_state_mismatch", "That code belongs to a different Claude sign-in. Use this attempt's code, or start again.")
            previous_entry = self._entry(attempt.provider_id) if attempt.provider_id else None
            previous = self.oauth.get(attempt.provider_id) if attempt.provider_id else None
            generation = self._claude_generation(previous)
            # Consume before the first await, preventing duplicate exchanges.
            del self._claude_sign_ins[attempt_id]
            self._claude_outcomes[attempt_id] = {"status": "pending", "at": self._now()}
        try:
            provider = await self._exchange_claude_sign_in(attempt, params, previous_entry, generation)
        except Exception as exc:
            self._claude_outcomes[attempt_id] = {
                "status": "failed", "code": getattr(exc, "code", None) or "sign_in_failed",
                "message": self.redactor.scrub(getattr(exc, "message", None) or str(exc)),
                "at": self._now(),
            }
            raise
        self._claude_outcomes[attempt_id] = {
            "status": "done", "provider_id": provider.provider_id, "at": self._now(),
        }
        return provider

    async def _exchange_claude_sign_in(
        self, attempt: claude.ClaudeSignInAttempt, params: dict,
        previous_entry: _Entry | None, generation: tuple | None,
    ) -> Provider:
        client = self._client()
        try:
            response = await client.post(
                CLAUDE_TOKEN_URL,
                json={"grant_type": "authorization_code", "client_id": CLAUDE_CLIENT_ID,
                      "code": params["code"], "state": params["state"],
                      "redirect_uri": CLAUDE_REDIRECT_URI, "code_verifier": attempt.verifier},
                headers={"Accept": "application/json"}, timeout=DISCOVERY_TIMEOUT_S,
            )
            payload = _json_object(response.content)
            for field in ("access_token", "refresh_token"):
                self.redactor.remember(str(payload.get(field) or ""))
            if response.status_code != 200:
                raise SignInError("sign_in_exchange_failed", f"Claude refused the sign-in code: {self._error_message(response.status_code, response.content)}")
            try:
                access, rotating, expiry = self._claude_token_fields(payload)
            except ValueError as exc:
                raise SignInError("sign_in_exchange_failed", str(exc)) from None
            profile_response = await client.get(
                CLAUDE_PROFILE_URL, headers=claude.oauth_headers(access), timeout=DISCOVERY_TIMEOUT_S,
            )
        except httpx.HTTPError as exc:
            raise self._upstream_error("claude_code", 502, f"could not reach Claude sign-in service: {type(exc).__name__}") from None
        profile = _json_object(profile_response.content)
        account, org = profile.get("account"), profile.get("organization")
        if (
            profile_response.status_code != 200 or not isinstance(account, dict)
            or not isinstance(org, dict)
            or not all(isinstance(v, str) and v.strip() for v in (
                account.get("uuid"), org.get("uuid"),
            ))
        ):
            raise SignInError("sign_in_exchange_failed", "Claude profile check failed: " + (
                self._error_message(profile_response.status_code, profile_response.content)
                if profile_response.status_code != 200 else "account.uuid and organization.uuid are required"
            ))
        subject = org["uuid"] + ":" + account["uuid"]
        with self._claude_credential_lock:
            if previous_entry is not None:
                if not self._claude_current(previous_entry, generation):
                    raise SignInError("sign_in_attempt_unknown", "The Claude provider or credential changed during sign-in. Start again.")
                previous = self.oauth.get(previous_entry.provider.provider_id)
                if previous is None or previous.subject != subject:
                    raise SignInError("sign_in_wrong_account", "That is a different Claude account or organization from this provider's stored account.")
            pid = attempt.provider_id or self._mint_id(ProviderKind.CLAUDE_CODE)
            record = SignInRecord(
                provider_id=pid, client_id=CLAUDE_CLIENT_ID, subject=subject,
                email=account.get("email") if isinstance(account.get("email"), str) else "",
                id_token="", access_token=access, refresh_token=rotating,
                expires_at=self._now() + expiry, saved_at=self._now(),
                scopes=payload.get("scope", CLAUDE_SCOPES).split(),
            )
            self.oauth.put(record)
            if previous_entry is None:
                try:
                    self._register({"kind": "claude_code", "provider_id": pid}, signed_in=True)
                except BaseException:
                    self.oauth.delete(pid)
                    raise
            else:
                previous_entry.runtime.note_success(self._now())
                self._check_key(previous_entry)
                self._persist()
        await self.refresh_async(pid)
        return self.get(pid)

    def _upstream_error(
        self, provider_id: str, status: int, message: str, *, code: str | None = None,
        retry_after_s: float | None = None,
    ) -> UpstreamError:
        """An UpstreamError carrying an OpenAI-shaped body.

        The gateway hands ``body`` to the client verbatim, so an error derate
        writes itself -- rather than relays -- has to bring one, or the client
        receives the status and nothing to read.
        """
        message = self.redactor.scrub(message)
        exc = UpstreamError(provider_id, status, message, error_code=code,
                            retry_after_s=retry_after_s)
        exc.body = json.dumps(exc.to_openai_error())
        return exc

    # -- Sign in with ChatGPT ----------------------------------------------

    def _prune_sign_ins(self, now: float) -> None:
        for attempt_id, attempt in list(self._sign_ins.items()):
            if now - attempt.created > SIGN_IN_ATTEMPT_TTL_S:
                del self._sign_ins[attempt_id]
        for attempt_id, outcome in list(self._sign_in_outcomes.items()):
            if now - outcome["at"] > SIGN_IN_ATTEMPT_TTL_S:
                del self._sign_in_outcomes[attempt_id]

    def chatgpt_sign_in_status(self, attempt_id: str) -> dict:
        """Where an attempt stands: ``pending``, ``done`` or ``failed``.

        What the UI polls while the operator is in the ChatGPT tab, so a
        redirect that reached ``GET /auth/callback`` finishes the screen too
        without anybody pasting anything.
        """
        self._prune_sign_ins(self._now())
        if attempt_id in self._sign_ins:
            return {"status": "pending"}
        outcome = self._sign_in_outcomes.get(attempt_id)
        if outcome is None:
            raise SignInError("sign_in_attempt_unknown", siwc.ATTEMPT_UNKNOWN)
        return {k: v for k, v in outcome.items() if k != "at"}

    async def complete_chatgpt_callback(self, query: str) -> tuple[str, Provider]:
        """Finish whichever attempt the redirect's ``state`` belongs to.

        ``GET /auth/callback`` has no session and no attempt id -- it is a
        browser following OpenAI's redirect -- so the state, a 256-bit secret
        only this attempt's authorize URL carried, is what identifies it.
        """
        self._prune_sign_ins(self._now())
        state = siwc.parse_callback(query).get("state", "").encode()
        for attempt_id, attempt in list(self._sign_ins.items()):
            if state and hmac.compare_digest(state, attempt.state.encode()):
                return attempt_id, await self.complete_chatgpt_sign_in(attempt_id, query)
        raise SignInError("sign_in_attempt_unknown", siwc.ATTEMPT_UNKNOWN)

    def start_chatgpt_sign_in(self, provider_id: str | None) -> dict:
        """Begin a sign-in: the authorize URL for the operator's browser.

        With ``provider_id``, a sign-in-again for that provider: the client id
        OpenAI issued it, and its account as the login hint. Without one, a
        first registration under the dynamic client id.
        """
        now = self._now()
        self._prune_sign_ins(now)
        if provider_id:
            entry = self._entry(provider_id)
            if entry.spec.sign_in != "chatgpt":
                raise ValueError(f"provider {provider_id!r} is not a ChatGPT plan provider")
            record = self.oauth.get(provider_id)
            if record is None:
                raise ValueError(
                    f"provider {provider_id!r} has no ChatGPT sign-in to renew; "
                    "remove it and sign in again"
                )
            attempt = siwc.new_attempt(
                client_id=record.client_id,
                provider_id=provider_id,
                email_hint=record.email or None,
                now=now,
            )
        else:
            attempt = siwc.new_attempt(
                client_id=siwc.DYNAMIC_CLIENT_ID, provider_id=None, email_hint=None, now=now
            )
        self._sign_ins[attempt.attempt_id] = attempt
        return {
            "attempt_id": attempt.attempt_id,
            "authorize_url": siwc.authorize_url(attempt, host_id=self.oauth.host_id()),
            "redirect_uri": CHATGPT_REDIRECT_URI,
            "expires_at": now + SIGN_IN_ATTEMPT_TTL_S,
        }

    async def complete_chatgpt_sign_in(self, attempt_id: str, callback_url: str) -> Provider:
        """Finish a sign-in from the callback address, pasted or redirected.

        Every refusal is a :class:`SignInError` naming what to do, and none of
        them stores anything: the record is written only once the ID token has
        verified, plan usage was granted, and (on a sign-in-again) the account
        is the one already on file. Once the attempt is consumed its outcome is
        kept for :meth:`chatgpt_sign_in_status`.
        """
        try:
            provider = await self._complete_chatgpt_sign_in(attempt_id, callback_url)
        except Exception as exc:
            if attempt_id not in self._sign_ins and attempt_id in self._consumed:
                self._consumed.discard(attempt_id)
                self._sign_in_outcomes[attempt_id] = {
                    "status": "failed",
                    "code": getattr(exc, "code", None) or "sign_in_failed",
                    "message": self.redactor.scrub(getattr(exc, "message", None) or str(exc)),
                    "at": self._now(),
                }
            raise
        self._consumed.discard(attempt_id)
        self._sign_in_outcomes[attempt_id] = {
            "status": "done",
            "provider_id": provider.provider_id,
            "at": self._now(),
        }
        return provider

    async def _complete_chatgpt_sign_in(self, attempt_id: str, callback_url: str) -> Provider:
        now = self._now()
        self._prune_sign_ins(now)
        attempt = self._sign_ins.get(attempt_id)
        if attempt is None:
            raise SignInError("sign_in_attempt_unknown", siwc.ATTEMPT_UNKNOWN)
        params = siwc.parse_callback(callback_url)
        if "code" not in params and "error" not in params:
            raise SignInError("sign_in_bad_callback", siwc.BAD_CALLBACK)
        if not hmac.compare_digest(params.get("state", "").encode(), attempt.state.encode()):
            raise SignInError(
                "sign_in_state_mismatch",
                "That address belongs to a different sign-in. Paste the address "
                "from the tab this sign-in opened, or start again.",
            )
        # Past the state check the code is this attempt's, and it is single
        # use: whatever happens next, the attempt cannot be finished twice.
        del self._sign_ins[attempt_id]
        self._consumed.add(attempt_id)
        if "error" in params:
            raise SignInError(
                "sign_in_declined", params.get("error_description") or params["error"]
            )
        issued = params.get("client_id", "")
        if attempt.provider_id is None:
            if not issued or issued == siwc.DYNAMIC_CLIENT_ID:
                raise SignInError(
                    "sign_in_incomplete",
                    "OpenAI did not issue derate a client id for this sign-in. Start again.",
                )
            client_id = issued
        else:
            if issued and issued != attempt.client_id:
                raise SignInError(
                    "sign_in_client_mismatch",
                    "OpenAI answered for a different registration of derate than "
                    "the one this provider uses. Start again.",
                )
            client_id = attempt.client_id

        client = self._client()
        try:
            response = await client.post(
                CHATGPT_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "code": params["code"],
                    "code_verifier": attempt.verifier,
                    "redirect_uri": CHATGPT_REDIRECT_URI,
                    "resource": CHATGPT_RESOURCE,
                },
                headers={"Accept": "application/json"},
                timeout=DISCOVERY_TIMEOUT_S,
            )
        except httpx.HTTPError as exc:
            raise self._upstream_error(
                "chatgpt", 502, f"could not reach {CHATGPT_TOKEN_URL}: {type(exc).__name__}"
            ) from None
        tokens = _json_object(response.content)
        # Before anything below can format an error: the redactor scrubs only
        # what it already holds.
        for name in ("id_token", "access_token", "refresh_token"):
            self.redactor.remember(str(tokens.get(name) or ""))
        if response.status_code != 200 or not all(
            isinstance(tokens.get(name), str) and tokens[name]
            for name in ("id_token", "access_token", "refresh_token")
        ):
            detail = self._error_message(response.status_code, response.content)
            raise SignInError(
                "sign_in_exchange_failed", f"OpenAI refused the sign-in code: {detail}"
            )

        discovery = await self._chatgpt_discovery()
        jwks = await self._get_json(str(discovery.get("jwks_uri") or ""))
        claims = siwc.verify_id_token(
            tokens["id_token"], jwks, client_id=client_id, nonce=attempt.nonce
        )
        granted = str(tokens.get("scope") or params.get("scope") or "").split()
        if CHATGPT_PLAN_SCOPE not in granted:
            raise SignInError("sign_in_no_plan_usage", siwc.NO_PLAN_USAGE)
        subject = str(claims.get("sub") or "")
        if attempt.provider_id is not None:
            previous = self.oauth.get(attempt.provider_id)
            if previous is None or previous.subject != subject:
                raise SignInError("sign_in_wrong_account", siwc.WRONG_ACCOUNT)

        provider_id = attempt.provider_id or self._mint_id(ProviderKind.CHATGPT)
        self.oauth.put(
            SignInRecord(
                provider_id=provider_id,
                client_id=client_id,
                subject=subject,
                email=str(claims.get("email") or ""),
                id_token=tokens["id_token"],
                access_token=tokens["access_token"],
                refresh_token=tokens["refresh_token"],
                expires_at=now + float(tokens.get("expires_in") or 3600),
                scopes=granted,
                saved_at=now,
            )
        )
        if attempt.provider_id is None:
            self._register({"kind": ProviderKind.CHATGPT.value, "provider_id": provider_id},
                           signed_in=True)
        else:
            entry = self._entry(provider_id)
            entry.runtime.note_success(now)
            # Re-enables a provider that was switched off for the lapsed sign-in.
            self._check_key(entry)
            self._persist()
        log.info("provider %s signed in to ChatGPT", provider_id)
        await self.refresh_async(provider_id)
        return self.get(provider_id)

    async def _chatgpt_discovery(self) -> dict:
        """OpenAI's OpenID configuration, fetched once per process."""
        if self._siwc_discovery is None:
            self._siwc_discovery = await self._get_json(CHATGPT_DISCOVERY_URL)
        return self._siwc_discovery

    async def _get_json(self, url: str) -> dict:
        if not url:
            raise self._upstream_error("chatgpt", 502, "OpenAI's sign-in configuration names no URL")
        try:
            response = await self._client().get(
                url, headers={"Accept": "application/json"}, timeout=DISCOVERY_TIMEOUT_S
            )
        except httpx.HTTPError as exc:
            raise self._upstream_error(
                "chatgpt", 502, f"could not reach {url}: {type(exc).__name__}"
            ) from None
        payload = _json_object(response.content)
        if response.status_code != 200 or not payload:
            raise self._upstream_error(
                "chatgpt", 502,
                f"{url} answered {response.status_code}: "
                f"{self._error_message(response.status_code, response.content)}",
            )
        return payload

    async def sign_out_async(self, provider_id: str) -> bool | None:
        """Revoke a sign-in provider's refresh token at OpenAI. Never raises.

        ``None`` when there is nothing to revoke (not a sign-in kind, or no
        token held); otherwise whether OpenAI confirmed it. A ``False`` is not
        a reason to keep the provider -- removing it still deletes the tokens
        here -- only a reason to tell the operator to disconnect derate from
        the ChatGPT side as well.
        """
        try:
            entry = self._entry(provider_id)
            if not entry.spec.sign_in:
                return None
            if entry.spec.sign_in == "claude":
                # No remote revocation endpoint has been verified.
                return False
            record = self.oauth.get(provider_id)
            if record is None or not record.refresh_token:
                return None
            endpoint = str((await self._chatgpt_discovery()).get("revocation_endpoint") or "")
            if not endpoint:
                return False
            client = self._client()
            for attempt in range(SIGN_OUT_RETRIES + 1):
                try:
                    response = await client.post(
                        endpoint,
                        data={
                            "token": record.refresh_token,
                            "token_type_hint": "refresh_token",
                            "client_id": record.client_id,
                        },
                        timeout=DISCOVERY_TIMEOUT_S,
                    )
                except httpx.HTTPError:
                    response = None
                if response is not None and response.status_code == 200:
                    return True
                if response is not None and response.status_code < 500:
                    break
                if attempt < SIGN_OUT_RETRIES:
                    await asyncio.sleep(jittered_delay())
        except Exception as exc:  # a failed revocation must never block a removal
            log.warning("provider %s: revocation failed (%s)", provider_id, type(exc).__name__)
            return False
        log.warning("provider %s: OpenAI did not confirm revocation", provider_id)
        return False

    # -- registry ----------------------------------------------------------

    def _entry(self, provider_id: str) -> _Entry:
        try:
            return self._entries[provider_id]
        except KeyError:
            raise UnknownProviderError(provider_id) from None

    def list(self) -> list[Provider]:
        """Every provider. Records hold a key *reference*; there is no key to redact."""
        return [copy.deepcopy(self._sync(e).provider) for e in self._sorted()]

    def get(self, provider_id: str) -> Provider:
        return copy.deepcopy(self._sync(self._entry(provider_id)).provider)

    def _sorted(self) -> list[_Entry]:
        return sorted(
            self._entries.values(),
            key=lambda e: (e.provider.priority, e.provider.provider_id),
        )

    def add(self, spec: dict) -> Provider:
        """Register a provider and pull its model list.

        Synchronous, to satisfy the port. Async callers should prefer
        :meth:`add_async`, which does not need a worker thread.
        """
        provider_id = self._register(spec)
        try:
            self._run_sync(lambda: self._refresh_standalone(provider_id))
        except ProviderError as exc:
            log.warning("initial model refresh for %s failed: %s", provider_id, exc)
        return self.get(provider_id)

    async def add_async(self, spec: dict) -> Provider:
        provider_id = self._register(spec)
        await self.refresh_async(provider_id)
        return self.get(provider_id)

    def _register(self, spec: dict, *, signed_in: bool = False) -> str:
        """Validate *spec* and add the provider. ``signed_in`` is the sign-in flow only.

        A sign-in kind's credential is created by the sign-in completing, so
        the add route -- which takes a key or a reference -- can never create
        one: there is nothing it could store that would authenticate.
        """
        kind = ProviderKind(spec.get("kind", ProviderKind.CUSTOM))
        kind_spec = spec_for(kind)
        if kind_spec.sign_in and not signed_in:
            raise ValueError(
                f"{kind.value} providers are added by signing in: POST /api/sign-in/{kind_spec.sign_in}"
            )

        base_url = str(spec.get("base_url") or kind_spec.base_url).strip()
        if not base_url:
            raise ValueError(f"{kind.value} needs a base_url")
        if looks_like_secret(base_url):
            raise ValueError(
                "base_url looks like it contains key material; put the key in an "
                "environment variable or secrets.json and reference it by name"
            )

        # Read a pasted key before anything below can raise. The route handler
        # logs the exception and formats its text into the response, and the
        # redactor can only scrub a value it already holds.
        api_key = str(spec.get("api_key") or "").strip()
        self.redactor.remember(api_key)

        api_key_ref = str(spec.get("api_key_ref") or "").strip()
        if kind_spec.sign_in and (api_key or api_key_ref):
            raise ValueError(f"{kind.value} credentials must come from sign-in, not api_key or api_key_ref")
        if looks_like_secret(api_key_ref):
            # The whole design rests on this field being a name. A key here
            # would be displayed everywhere a reference safely can be. There
            # is somewhere to put the key now, so the refusal says where.
            raise ValueError(KEY_IN_REF_FIELD)
        if kind_spec.requires_key and not signed_in and not api_key_ref and not api_key:
            raise ValueError(f"{kind.value} needs an api_key or an api_key_ref")

        provider_id = str(spec.get("provider_id") or "").strip() or self._mint_id(kind)
        if provider_id in self._entries:
            raise ValueError(f"provider {provider_id!r} already exists")
        if api_key_ref and not ref_allowed(api_key_ref, provider_id):
            raise ValueError(REF_NOT_OURS)
        # After the reference screens: a spec wrong on both counts is told
        # about the key first, which is the costlier mistake.
        validate_base_url(kind, base_url)
        budget = parse_daily_budget(spec.get("daily_budget_usd"))

        display_name = str(spec.get("display_name") or kind_spec.display_name)
        aliases = {str(k): str(v) for k, v in (spec.get("aliases") or {}).items()}
        # Same screen the ref and URL get: these two fields are echoed in
        # every provider listing and persisted verbatim, so a pasted key
        # here would leak everywhere a reference safely displays (WF-5 /
        # audit follow-up L1 — the screen existed, these fields dodged it).
        _screen_free_text(display_name, "display_name")
        for alias_key, alias_value in aliases.items():
            _screen_free_text(alias_key, "aliases key")
            _screen_free_text(alias_value, f"aliases[{alias_key!r}]")
        # Last, so a spec refused by any screen above leaves no secret behind:
        # an explicit reference names where to put the key, otherwise mint one.
        if api_key:
            api_key_ref = api_key_ref or minted_ref(provider_id)
            self._store_key(api_key_ref, api_key)

        provider = Provider(
            provider_id=provider_id,
            kind=kind,
            display_name=display_name,
            base_url=base_url,
            api_key_ref=api_key_ref,
            enabled=bool(spec.get("enabled", True)),
            priority=int(spec.get("priority", 100)),
            models=[],
            healthy=True,
            last_error=None,
            last_refreshed=0.0,
        )
        runtime = ProviderRuntime(
            provider_id=provider_id,
            daily_budget_usd=budget,
            aliases=aliases,
            # A new provider serves nothing until somebody says otherwise.
            # Deliberately not settable from the spec: the catalogue is empty
            # until the initial refresh returns, so there would be nothing to
            # check a requested id against and no way to refuse a wrong one.
            enabled_models=frozenset(),
        )
        entry = _Entry(provider=provider, runtime=runtime, spec=kind_spec)
        self._entries[provider_id] = entry
        self._check_key(entry)
        self._persist()
        log.info("added provider %s (%s)", provider_id, kind.value)
        return provider_id

    async def pull(
        self,
        provider_id: str,
        model: str,
        *,
        on_size: Callable[[int], None] | None = None,
        on_progress: Callable[[dict], None] | None = None,
    ) -> dict:
        """Tell a self-hosted provider to fetch *model*, then re-read its catalogue.

        Streamed rather than awaited whole, for one reason that matters: the
        response's first frames carry the download total, and that is the only
        moment before several minutes of transfer at which anything can decide
        the weights are too big for the machine. ``on_size`` is called once with
        that total; raising from it aborts the transfer, which is how the
        caller's memory gate refuses without first filling somebody's SD card.

        ``on_progress`` is called with every frame, and is how a caller watches
        a transfer rather than only judging it. The frames carry ``completed``
        alongside ``total`` the whole way down, so the progress was always
        arriving here -- until this existed the loop read it and dropped it on
        the floor, and a pull was a single number reported once followed by
        minutes of silence. It is deliberately handed the raw frame: ``status``
        is the upstream's own sentence ("pulling manifest", "verifying sha256
        digest") and paraphrasing it here would put a second, worse vocabulary
        in front of the one the server actually used. Unlike ``on_size`` it is
        not a gate -- it must not raise, and anything it raises will abort the
        transfer exactly as ``on_size`` does.

        The native API, not the OpenAI-compatible one -- pulling is not in that
        spec. ``pull_path`` names it per kind so nothing here assumes Ollama.
        """
        entry = self._entry(provider_id)
        if not entry.spec.pull_path:
            raise PullUnsupportedError(
                f"{entry.spec.display_name} does not host its own weights, so "
                f"there is nothing to pull onto it."
            )
        model = model.strip()
        if not model:
            raise ValueError("pull needs a model name")
        _screen_passthrough(model, "model")

        url = join_url(native_base(entry.provider.base_url), entry.spec.pull_path)
        client = self._client()
        seen_size = False
        digest = ""
        async with client.stream(
            "POST",
            url,
            json={"model": model, "stream": True},
            timeout=httpx.Timeout(connect=CONNECT_TIMEOUT_S, read=None, write=WRITE_TIMEOUT_S, pool=CONNECT_TIMEOUT_S),
        ) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", "replace")[:400]
                raise UpstreamError(provider_id, response.status_code, body)
            async for line in response.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                try:
                    frame = json.loads(line)
                except ValueError:
                    continue
                # The upstream reports its own failures in-band, at 200.
                if frame.get("error"):
                    raise UpstreamError(provider_id, 502, str(frame["error"])[:400])
                total = frame.get("total")
                if not seen_size and isinstance(total, (int, float)) and total > 0:
                    seen_size = True
                    if on_size is not None:
                        # Raising here leaves the `async with` to close the
                        # connection, which is what stops the transfer.
                        on_size(int(total))
                if frame.get("digest"):
                    digest = str(frame["digest"])
                # After the gate, never before it: a refused pull downloaded
                # nothing, and reporting a frame from it would put a bar on
                # screen for a transfer that was aborted at its first byte.
                if on_progress is not None:
                    on_progress(frame)

        # The catalogue is what makes the model routable; without this the pull
        # succeeds and nothing can address it until the refresh loop comes round.
        await self._refresh_standalone(provider_id)
        return {"provider_id": provider_id, "model": model, "digest": digest}

    def _mint_id(self, kind: ProviderKind) -> str:
        base = _ID_SAFE.sub("-", kind.value.lower()).strip("-") or "provider"
        if base not in self._entries:
            return base
        n = 2
        while f"{base}-{n}" in self._entries:
            n += 1
        return f"{base}-{n}"

    def update(self, provider_id: str, patch: dict) -> Provider:
        """Enable, disable, reprioritize, rename, set a budget, set aliases."""
        entry = self._entry(provider_id)
        held = entry.provider.api_key_ref
        self._apply_patch(entry, patch)
        if "base_url" in patch and held == minted_ref(provider_id) and not entry.provider.api_key_ref:
            # _apply_patch let go of the key because base_url moved: the key
            # was pasted for the old host and is never sent to the new one.
            self.secrets.delete(held)
        # auto_disabled too, not just enabled: a provider switched off *because*
        # its reference did not resolve is exactly the one a new key should
        # bring back, and it is disabled by definition. An operator who turned
        # it off by hand stays off.
        if entry.provider.enabled or entry.auto_disabled:
            self._check_key(entry)
        self._persist()
        return self.get(provider_id)

    def validate_patch(self, provider_id: str, patch: dict) -> None:
        """Every check ``update`` makes, against a copy, with nothing written.

        ``POST /api/wiring`` validates a whole batch before applying any of it,
        so this has to raise exactly what ``update`` would and change nothing.
        A key is the one field it refuses outright: pasting one stores it in
        secrets.json, which no dry run may do.
        """
        if not isinstance(patch, dict):
            raise ValueError("patch must be an object")
        if "api_key" in patch:
            raise ValueError(WIRING_NO_API_KEY)
        self._apply_patch(copy.deepcopy(self._entry(provider_id)), patch)

    def _apply_patch(self, entry: _Entry, patch: dict) -> None:
        provider_id = entry.provider.provider_id
        if entry.spec.sign_in and ("api_key" in patch or "api_key_ref" in patch):
            self.redactor.remember(str(patch.get("api_key") or ""))
            raise ValueError(f"{entry.provider.kind.value} credentials must come from sign-in, not api_key or api_key_ref")
        if "enabled" in patch:
            entry.provider.enabled = bool(patch["enabled"])
            entry.auto_disabled = False
        if "priority" in patch:
            entry.provider.priority = int(patch["priority"])
        if "display_name" in patch:
            display_name = str(patch["display_name"])
            _screen_free_text(display_name, "display_name")
            entry.provider.display_name = display_name
        if "daily_budget_usd" in patch:
            entry.runtime.daily_budget_usd = parse_daily_budget(patch["daily_budget_usd"])
            # The other crossing direction: the spend did not move, the cap did.
            self._check_budget(entry)
        if "base_url" in patch:
            base_url = str(patch["base_url"]).strip()
            if looks_like_secret(base_url):
                raise ValueError("base_url looks like it contains key material")
            validate_base_url(entry.provider.kind, base_url)
            if (
                base_url != entry.provider.base_url
                and entry.provider.api_key_ref == minted_ref(provider_id)
                and "api_key" not in patch
                and "api_key_ref" not in patch
            ):
                # A key derate stored was pasted for the old host. Moving the
                # URL must not carry it to the new one (finding K3): the
                # provider needs its key again.
                entry.provider.api_key_ref = ""
            entry.provider.base_url = base_url
        if "api_key_ref" in patch:
            ref = str(patch["api_key_ref"]).strip()
            if looks_like_secret(ref):
                # Word for word what the add path says. The same mistake got a
                # shorter, different sentence here, which read as a different
                # rule rather than the same one.
                raise ValueError(KEY_IN_REF_FIELD)
            if ref and not ref_allowed(ref, provider_id):
                raise ValueError(REF_NOT_OURS)
            entry.provider.api_key_ref = ref
        if "api_key" in patch:
            # Rotating a key in place. Reuse the reference the provider already
            # has when derate minted it, so everything pointing at that name
            # keeps working; mint one when the provider was configured against
            # an operator's own reference, which is not ours to overwrite. A
            # reference named in this same patch wins over both.
            api_key = str(patch["api_key"] or "").strip()
            self.redactor.remember(api_key)
            if not api_key:
                raise ValueError("api_key must not be empty")
            ref = entry.provider.api_key_ref
            if "api_key_ref" not in patch and ref != minted_ref(provider_id):
                ref = minted_ref(provider_id)
            self._store_key(ref, api_key)
            entry.provider.api_key_ref = ref
        if "aliases" in patch:
            aliases = {str(k): str(v) for k, v in (patch["aliases"] or {}).items()}
            for alias_key, alias_value in aliases.items():
                _screen_free_text(alias_key, "aliases key")
                _screen_free_text(alias_value, f"aliases[{alias_key!r}]")
            entry.runtime.aliases = aliases
            self._apply_aliases(entry)
        if "enabled_models" in patch:
            entry.runtime.enabled_models = self._screen_allowlist(entry, patch["enabled_models"])
        if "backend_pins" in patch:
            pins, prices, fallbacks = self._screen_backend_pins(entry, patch["backend_pins"])
            entry.runtime.backend_pins = pins
            entry.runtime.backend_pin_prices = prices
            entry.runtime.backend_fallbacks = fallbacks
            self._apply_pin_prices(entry)
        if "backend_fallbacks" in patch:
            entry.runtime.backend_fallbacks = self._screen_backend_fallbacks(
                entry, patch["backend_fallbacks"]
            )
        if "audio_prices" in patch:
            entry.runtime.audio_prices = self._screen_audio_prices(entry, patch["audio_prices"])
            self._apply_pin_prices(entry)

    def remove(self, provider_id: str) -> None:
        with self._claude_credential_lock:
            entry = self._entry(provider_id)
            ref = entry.provider.api_key_ref
            del self._entries[provider_id]
            # Only delete keys minted by derate, not operator-owned references.
            if ref and ref == minted_ref(provider_id):
                self.secrets.delete(ref)
            self.oauth.delete(provider_id)
            for attempt_id, attempt in list(self._claude_sign_ins.items()):
                if attempt.provider_id == provider_id:
                    del self._claude_sign_ins[attempt_id]
            self._persist()
            log.info("removed provider %s", provider_id)

    def _apply_aliases(self, entry: _Entry) -> None:
        for model in entry.provider.models:
            model.served_name = entry.runtime.aliases.get(model.upstream_id, model.upstream_id)
        entry.provider.models.sort(key=lambda m: m.served_name)
        self._apply_pin_prices(entry)
        entry.reindex()

    # -- the allowlist -----------------------------------------------------

    @staticmethod
    def _screen_allowlist(entry: _Entry, value: object) -> frozenset[str]:
        """Validate a requested allowlist against what the provider publishes.

        Membership in the live catalogue is the whole check, and it is a
        stronger guard than the key screens the other free-text fields get: an
        id has to have been published by the upstream before it can be stored,
        and no API key ever will be. That matters because the obvious screen is
        the wrong one here -- ``_screen_free_text``'s entropy fallback is what
        refused all 96 variants of Mistral-Small-24B-Instruct-2501, and a
        model id is exactly the shape it gets wrong.

        Refusing names the ids rather than the count, because the fix is to
        stop asking for that model and you cannot do that without knowing which.
        """
        wanted = {str(item) for item in (value or [])}
        unknown = sorted(wanted - {m.upstream_id for m in entry.provider.models})
        if unknown:
            shown = ", ".join(unknown[:3])
            more = f" (and {len(unknown) - 3} more)" if len(unknown) > 3 else ""
            raise ValueError(
                f"{entry.provider.provider_id} does not publish {shown}{more}; "
                "enable only models from its own catalogue"
            )
        return frozenset(wanted)

    @staticmethod
    def _screen_backend_models(entry: _Entry, value: object, key: str) -> dict:
        """The per-model update map for ``backend_pins``/``backend_fallbacks``,
        refused for a kind with no backend hosts or a model off its catalogue."""
        if not entry.spec.supports_backend_routing:
            raise ValueError(
                f"{entry.provider.provider_id} does not aggregate backend hosts per model; "
                f"{key} does not apply"
            )
        if value is not None and not isinstance(value, dict):
            raise ValueError(f"{key} must be an object keyed by upstream model id")
        updates = {str(k): v for k, v in (value or {}).items()}
        unknown = sorted(set(updates) - {m.upstream_id for m in entry.provider.models})
        if unknown:
            shown = ", ".join(unknown[:3])
            more = f" (and {len(unknown) - 3} more)" if len(unknown) > 3 else ""
            raise ValueError(
                f"{entry.provider.provider_id} does not publish {shown}{more}; "
                "pin a backend only for a model from its own catalogue"
            )
        return updates

    def _screen_backend_pins(
        self, entry: _Entry, value: object
    ) -> tuple[
        dict[str, str], dict[str, tuple[float | None, float | None]], dict[str, bool]
    ]:
        """Merge a requested backend-pin update into what is already pinned.

        One model at a time, unlike ``aliases`` and ``enabled_models``: the
        control that writes this is per-model, on the Models tab, and must
        not have to know every other model's pin on the same provider just to
        avoid erasing it by omission. An empty string clears that model's pin
        -- "" can never be a real backend tag -- rather than needing a
        separate delete form.

        Membership is checked against the model list, the same guard
        ``_screen_allowlist`` uses and for the same reason. A new tag is then
        checked against the model's live endpoints call (``list_backends``):
        a tag OpenRouter does not list is refused here rather than accepted
        and turned into a 404 on every request, and the listed row's prices
        are captured as the price the model is billed and routed at from now
        on. A tag equal to the one already pinned is not re-fetched.

        Returns ``(pins, pin_prices, fallbacks)`` and writes nothing: the
        caller assigns them, so a dry run (``validate_patch``) on a copy
        changes nothing either. Clearing a pin drops its price and its
        fallback flag.
        """
        provider_id = entry.provider.provider_id
        updates = {
            k: str(v or "") for k, v in self._screen_backend_models(
                entry, value, "backend_pins"
            ).items()
        }
        pins = dict(entry.runtime.backend_pins)
        prices = dict(entry.runtime.backend_pin_prices)
        fallbacks = dict(entry.runtime.backend_fallbacks)
        for upstream_id, tag in updates.items():
            if not tag:
                pins.pop(upstream_id, None)
                prices.pop(upstream_id, None)
                fallbacks.pop(upstream_id, None)
                continue
            if pins.get(upstream_id) == tag and upstream_id in prices:
                continue
            try:
                rows = self.list_backends(provider_id, upstream_id)
            except (UpstreamError, AdapterUnsupportedError, MissingKeyError) as exc:
                raise ValueError(str(exc)) from None
            row = next((r for r in rows if r.get("tag") == tag), None)
            if row is None:
                raise ValueError(
                    f"{provider_id} lists no backend '{tag}' for {upstream_id}; "
                    "pick one from its backend list"
                )
            pins[upstream_id] = tag
            prices[upstream_id] = (row.get("input_cost_per_mtok"), row.get("output_cost_per_mtok"))
        return pins, prices, fallbacks

    def _screen_backend_fallbacks(self, entry: _Entry, value: object) -> dict[str, bool]:
        """Merge a requested fallback-flag update, a key at a time like pins.

        ``True`` lets OpenRouter try other hosts when the pinned one is down;
        ``False`` (stored as absence) keeps the pin strict. Booleans only: a
        string "false" is truthy and would silently loosen a pin.
        """
        updates = self._screen_backend_models(entry, value, "backend_fallbacks")
        fallbacks = dict(entry.runtime.backend_fallbacks)
        for upstream_id, flag in updates.items():
            if not isinstance(flag, bool):
                raise ValueError("backend_fallbacks values must be true or false")
            if flag:
                fallbacks[upstream_id] = True
            else:
                fallbacks.pop(upstream_id, None)
        return fallbacks

    def _screen_audio_prices(self, entry: _Entry, value: object) -> dict[str, float]:
        """Merge a per-minute audio rate update a key at a time; ``null`` clears."""
        if not isinstance(value, dict):
            raise ValueError("audio_prices must be an object keyed by upstream model id")
        updates = {str(k): v for k, v in value.items()}
        unknown = sorted(set(updates) - {m.upstream_id for m in entry.provider.models})
        if unknown:
            raise ValueError(
                f"{entry.provider.provider_id} does not publish {', '.join(unknown[:3])}; "
                "set an audio rate only for a model from its own catalogue"
            )
        prices = dict(entry.runtime.audio_prices)
        for upstream_id, rate in updates.items():
            if rate is None:
                prices.pop(upstream_id, None)
                continue
            if isinstance(rate, bool) or not isinstance(rate, (int, float)):
                raise ValueError("audio_prices values must be a number of USD per minute, or null")
            if not math.isfinite(rate) or rate < 0:
                raise ValueError("audio_prices values must be a finite number >= 0")
            prices[upstream_id] = float(rate)
        return prices

    def _apply_pin_prices(self, entry: _Entry) -> None:
        """A pinned host's price on the model; the catalogue's when unpinned.

        ``blended_cost``, the route target's cost and un-metered spend all
        read the model's two prices, so setting them here is what makes a pin
        change what a request is routed and billed at. A model with no pin
        price gets the catalogue's back, which is how clearing a pin undoes it.
        """
        pinned = entry.runtime.backend_pin_prices
        for model in entry.provider.models:
            price = pinned.get(model.upstream_id) or entry.catalogue_prices.get(model.upstream_id)
            if price is not None:
                model.input_cost_per_mtok, model.output_cost_per_mtok = price
            model.price_per_minute_usd = entry.runtime.audio_prices.get(model.upstream_id)

    def backend_pin_state(self, provider_id: str) -> dict[str, tuple[str, bool]]:
        """``{upstream_id: (tag, fallback)}`` for every pinned model, for the read side."""
        runtime = self._entry(provider_id).runtime
        return {
            upstream_id: (tag, bool(runtime.backend_fallbacks.get(upstream_id)))
            for upstream_id, tag in runtime.backend_pins.items()
        }

    def servable(self) -> list[Provider]:
        """Every provider, carrying only the models it is allowed to serve.

        The routing- and UI-facing view, and the one seam that makes the
        allowlist real: the gateway builds its target index from this, and
        ``GET /api/providers`` renders it, so a model switched off here leaves
        ``/v1/models``, the chat picker, the models list and the cluster graph
        together rather than one at a time.

        ``list()`` deliberately still returns the whole catalogue -- something
        has to, or nothing could offer the operator a model to switch on.
        """
        out: list[Provider] = []
        for entry in self._sorted():
            provider = copy.deepcopy(self._sync(entry).provider)
            provider.models = [m for m in provider.models if entry.runtime.serves(m.upstream_id)]
            out.append(provider)
        return out

    def catalogue(self, provider_id: str) -> list[dict]:
        """Everything this provider publishes, each row saying whether it is on.

        ``GET /api/providers/{id}/models``. The one surface that is not
        filtered, because it is the one the operator chooses from.
        """
        from .serialization import model_public_dict

        entry = self._entry(provider_id)
        now = self._now()
        payload = [
            {
                **model_public_dict(m, entry.runtime, now, provider_enabled=entry.provider.enabled),
                "enabled": entry.runtime.serves(m.upstream_id),
                "backend_pin": entry.runtime.backend_pins.get(m.upstream_id),
                "backend_fallback": bool(
                    entry.runtime.backend_pins.get(m.upstream_id)
                    and entry.runtime.backend_fallbacks.get(m.upstream_id)
                ),
            }
            for m in entry.provider.models
        ]
        assert_no_key_material(payload, self.redactor, "GET /api/providers/{id}/models")
        return payload

    # -- discovery ---------------------------------------------------------

    def refresh(self, provider_id: str) -> Provider:
        """Re-pull the upstream model list. Synchronous, to satisfy the port."""
        self._run_sync(lambda: self._refresh_standalone(provider_id))
        return self.get(provider_id)

    async def _refresh_standalone(self, provider_id: str) -> None:
        async with self._client_factory() as client:
            await self.refresh_async(provider_id, client=client)

    async def refresh_async(
        self, provider_id: str, *, client: httpx.AsyncClient | None = None
    ) -> Provider:
        entry = self._entry(provider_id)
        now = self._now()
        if not self._check_key(entry):
            self._persist()
            return self.get(provider_id)

        client = client or self._client()
        if entry.spec.sign_in == "claude":
            return await self._refresh_claude_catalogue(entry, client)
        url = join_url(entry.provider.base_url, entry.spec.models_path)
        try:
            if entry.spec.sign_in:
                # Also what keeps an idle sign-in alive: the refresh token
                # lives 30 days and rotates on use, and this runs every 6 h.
                key = await self._access_token(entry, client=client)
            else:
                key = self.resolve_key(provider_id)
        except MissingKeyError:
            self._check_key(entry)
            self._persist()
            return self.get(provider_id)
        except UpstreamError as exc:
            self._note_refresh_failure(entry, now, exc.message)
            self._persist()
            return self.get(provider_id)

        headers = auth_headers(entry.spec, key)
        headers["Accept"] = "application/json"
        try:
            response = await client.get(url, headers=headers, timeout=DISCOVERY_TIMEOUT_S)
        except httpx.HTTPError as exc:
            self._note_refresh_failure(entry, now, f"could not reach {url}: {type(exc).__name__}")
            self._persist()
            return self.get(provider_id)

        if response.status_code >= 400:
            message = self._error_message(response.status_code, response.content)
            if response.status_code in (401, 403):
                entry.runtime.note_auth_failure(
                    now, response.status_code, self._credential_label(entry), message,
                    sign_in=bool(entry.spec.sign_in),
                )
            else:
                self._note_refresh_failure(
                    entry, now, f"model list returned {response.status_code}: {message}"
                )
            self._persist()
            return self.get(provider_id)

        try:
            payload = response.json()
        except ValueError:
            self._note_refresh_failure(entry, now, "model list was not JSON")
            self._persist()
            return self.get(provider_id)

        models = parse_models(payload, entry.spec, aliases=entry.runtime.aliases)
        if not models:
            if entry.spec.pull_path and recognized_envelope(payload):
                # A server that hosts its own weights and holds none yet is
                # empty, not broken -- and that is the state every one of them
                # is in between being added and being pulled to, which is the
                # order the UI asks for. Marking it unhealthy there puts a red
                # error on the screen for doing the right thing.
                #
                # Healthy with nothing in it routes nowhere on its own: an
                # empty catalogue contributes no route targets, so /v1/models
                # is unchanged and no request can land here. Health is a claim
                # about the server answering, which it did.
                entry.provider.models = []
                entry.capture_catalogue_prices()
                entry.reindex()
                entry.runtime.last_refreshed = now
                entry.runtime.healthy = True
                entry.runtime.last_error = None
                self._persist()
                log.info(
                    "provider %s has no models yet; nothing pulled onto it",
                    provider_id,
                )
                return self.get(provider_id)
            # Otherwise an empty list is indistinguishable from a shape we
            # failed to parse. Either way, keep what we had.
            self._note_refresh_failure(entry, now, "model list was empty or unrecognized")
            self._persist()
            return self.get(provider_id)

        entry.provider.models = models
        entry.capture_catalogue_prices()
        self._apply_pin_prices(entry)
        entry.reindex()
        entry.runtime.last_refreshed = now
        entry.runtime.healthy = True
        entry.runtime.last_error = None
        self._persist()
        log.info("provider %s refreshed: %d model(s)", provider_id, len(models))
        return self.get(provider_id)

    async def _refresh_claude_catalogue(self, entry: _Entry, client: httpx.AsyncClient) -> Provider:
        """Discover all pages atomically; a refused page never empties the cache."""
        pid = entry.provider.provider_id
        now = self._now()
        try:
            key = await self._access_token(entry, client=client)
            cursor = None
            seen_cursors: set[str] = set()
            rows = []
            refreshed = False
            while True:
                params = {"limit": "1000"}
                if cursor is not None:
                    params["after_id"] = cursor
                response = await client.get(
                    join_url(entry.spec.base_url, "models"), params=params,
                    headers={**claude.oauth_headers(key), "Accept": "application/json"},
                    timeout=DISCOVERY_TIMEOUT_S,
                )
                if response.status_code == 401 and not refreshed:
                    refreshed = True
                    key = await self._access_token(entry, force=True, client=client, rejected_token=key)
                    continue
                if response.status_code != 200:
                    detail = self._error_message(response.status_code, response.content)
                    if response.status_code in (401, 403):
                        entry.runtime.note_auth_failure(
                            now, response.status_code, self._credential_label(entry),
                            detail, sign_in=True,
                        )
                    raise self._upstream_error(pid, response.status_code, f"Claude model list returned {response.status_code}: {detail}")
                payload = _json_object(response.content)
                data = payload.get("data")
                if (
                    not isinstance(data, list) or not isinstance(payload.get("has_more"), bool)
                    or any(not isinstance(r, dict) or not isinstance(r.get("id"), str)
                           or not r["id"].strip() for r in data)
                ):
                    raise self._upstream_error(pid, 502, "Claude model list has a malformed data/has_more envelope")
                for row in data:
                    row = dict(row)
                    context = row.get("max_input_tokens")
                    if (not isinstance(context, bool) and isinstance(context, (int, float))
                            and math.isfinite(context) and context > 0):
                        row["context_length"] = int(context)
                    row["supports_streaming"] = True
                    rows.append(row)
                if not payload["has_more"]:
                    break
                following = payload.get("last_id")
                if not isinstance(following, str) or not following or following in seen_cursors:
                    raise self._upstream_error(pid, 502, "Claude model list has a missing or nonadvancing last_id cursor")
                seen_cursors.add(following)
                cursor = following
            models = parse_models({"data": rows}, entry.spec, aliases=entry.runtime.aliases)
        except (httpx.HTTPError, UpstreamError) as exc:
            if self._entries.get(pid) is not entry:
                raise UnknownProviderError(pid) from None
            message = exc.message if isinstance(exc, UpstreamError) else f"could not reach Claude models: {type(exc).__name__}"
            self._note_refresh_failure(entry, now, message)
            self._persist()
            return self.get(pid)
        if self._entries.get(pid) is not entry:
            raise UnknownProviderError(pid)
        entry.provider.models = models
        entry.capture_catalogue_prices()
        entry.reindex()
        entry.runtime.last_refreshed = now
        entry.runtime.healthy = True
        entry.runtime.last_error = None
        self._persist()
        return self.get(pid)

    def _note_refresh_failure(self, entry: _Entry, now: float, message: str) -> None:
        """Keep the cached list. A refresh failure is not an inference failure.

        With models cached we stay healthy and keep serving them. With nothing
        cached there is nothing to serve, so the provider is marked unhealthy.
        """
        entry.runtime.last_error = self.redactor.scrub(message)
        if not entry.provider.models:
            entry.runtime.healthy = False
        log.warning("provider %s refresh failed: %s", entry.provider.provider_id, message)

    async def refresh_all_async(self) -> None:
        for provider_id in list(self._entries):
            entry = self._entries[provider_id]
            if not entry.provider.enabled:
                continue
            try:
                await self.refresh_async(provider_id)
            except ProviderError as exc:
                log.warning("refresh of %s failed: %s", provider_id, exc)

    # -- backend routing -----------------------------------------------------

    async def list_backends_async(
        self,
        provider_id: str,
        upstream_id: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> list[dict]:
        """This model's backend hosts, as OpenRouter's own endpoints call sees them.

        Live, not cached: the point of asking is to see what is available
        right now to pin against, unlike the model list itself, which is
        served from the last refresh. Only a kind that aggregates several
        backend hosts per model supports this at all.
        """
        entry = self._entry(provider_id)
        if not entry.spec.supports_backend_routing:
            raise AdapterUnsupportedError(
                f"provider {provider_id!r} does not aggregate backend hosts per model"
            )
        author, _, slug = upstream_id.partition("/")
        if not slug:
            raise UpstreamError(provider_id, 400, f"{upstream_id!r} is not an author/slug id")
        key = self.resolve_key(provider_id)
        headers = auth_headers(entry.spec, key)
        headers["Accept"] = "application/json"
        client = client or self._client()
        url = join_url(entry.provider.base_url, f"models/{author}/{slug}/endpoints")
        try:
            response = await client.get(url, headers=headers, timeout=DISCOVERY_TIMEOUT_S)
        except httpx.HTTPError as exc:
            raise UpstreamError(
                provider_id, 502, f"could not reach {url}: {type(exc).__name__}"
            ) from None
        if response.status_code >= 400:
            message = self._error_message(response.status_code, response.content)
            raise UpstreamError(provider_id, response.status_code, message)
        try:
            payload = response.json()
        except ValueError:
            raise UpstreamError(provider_id, 502, "endpoints list was not JSON") from None
        rows = parse_endpoints(payload)
        pin = entry.runtime.backend_pins.get(upstream_id)
        for row in rows:
            row["pinned"] = row["tag"] == pin
        return rows

    def list_backends(self, provider_id: str, upstream_id: str) -> list[dict]:
        """Synchronous wrapper, for a caller with no event loop.

        Its own client, closed on the way out, as ``refresh`` does: the
        wrapper runs on a throwaway loop, and a client cached under that
        loop's id would be handed to whichever later loop reuses the id.
        """

        async def fetch() -> list[dict]:
            async with self._client_factory() as client:
                return await self.list_backends_async(provider_id, upstream_id, client=client)

        return self._run_sync(fetch)

    # -- models ------------------------------------------------------------

    def models(self) -> list[tuple[str, ProviderModel]]:
        """(provider_id, model) for every enabled provider, in priority order.

        Allowlisted, on the same reasoning as the ``enabled`` check beside it:
        this answers "what could a request be routed to", and a model nobody
        switched on is not one of them.
        """
        out: list[tuple[str, ProviderModel]] = []
        for entry in self._sorted():
            if not entry.provider.enabled:
                continue
            for model in entry.provider.models:
                if not entry.runtime.serves(model.upstream_id):
                    continue
                out.append((entry.provider.provider_id, copy.deepcopy(model)))
        return out

    def models_for(self, provider_id: str) -> list[ProviderModel]:
        return [copy.deepcopy(m) for m in self._entry(provider_id).provider.models]

    def find_model(self, provider_id: str, upstream_id: str) -> ProviderModel | None:
        entry = self._entry(provider_id)
        return entry.models_by_upstream.get(upstream_id)

    # -- public shapes -----------------------------------------------------

    def public_list(self, *, include_models: bool = True) -> list[dict]:
        """``GET /api/providers``. Checked for key material before it returns."""
        now = self._now()
        payload = [
            provider_public_dict(e.provider, e.runtime, now, include_models=include_models)
            for e in self._sorted()
        ]
        assert_no_key_material(payload, self.redactor, "GET /api/providers")
        return payload

    def public_dict(self, provider_id: str, *, include_models: bool = True) -> dict:
        entry = self._entry(provider_id)
        payload = provider_public_dict(
            entry.provider, entry.runtime, self._now(), include_models=include_models
        )
        assert_no_key_material(payload, self.redactor, f"GET /api/providers/{provider_id}")
        return payload

    def public_models(self, provider_id: str) -> list[dict]:
        """``GET /api/providers/{id}/models``."""
        from .serialization import model_public_dict

        entry = self._entry(provider_id)
        now = self._now()
        payload = [
            model_public_dict(m, entry.runtime, now, provider_enabled=entry.provider.enabled)
            for m in entry.provider.models
        ]
        assert_no_key_material(payload, self.redactor, "GET /api/providers/{id}/models")
        return payload

    # -- health and spend --------------------------------------------------

    def health(self, provider_id: str) -> tuple[bool, str | None]:
        entry = self._entry(provider_id)
        return entry.runtime.healthy, entry.runtime.last_error

    def spend_today(self, provider_id: str) -> float:
        return self._entry(provider_id).runtime.spend_today(self._now())

    def admitting(self, provider_id: str, upstream_id: str | None = None) -> bool:
        return self._entry(provider_id).runtime.admitting(self._now(), upstream_id)

    # -- route targets -----------------------------------------------------

    @staticmethod
    def target_id(provider_id: str, upstream_id: str) -> str:
        return f"{provider_id}:{upstream_id}"

    @staticmethod
    def split_target_id(target_id: str) -> tuple[str, str]:
        provider_id, _, upstream_id = target_id.partition(":")
        return provider_id, upstream_id

    @staticmethod
    def blended_cost(model: ProviderModel) -> float | None:
        """One scalar for ``RouteTarget.cost_per_mtok``, or None when unpriced.

        The contract carries a single cost; providers publish two. Weighted
        toward output because that is where a serving workload spends. Never
        invented: if the provider publishes nothing, this stays None and
        COST_AWARE skips the target rather than treating it as free.
        """
        if model.input_cost_per_mtok is None and model.output_cost_per_mtok is None:
            return None
        in_cost = model.input_cost_per_mtok or 0.0
        out_cost = model.output_cost_per_mtok or 0.0
        return in_cost * COST_BLEND_INPUT_WEIGHT + out_cost * COST_BLEND_OUTPUT_WEIGHT

    def route_targets(self) -> list[RouteTarget]:
        """Every remote model as a target the gateway can merge with local ones.

        ``strength`` stays zero: it is a normalized score over local hardware
        and we have not measured a remote. ``weight`` is the gateway's to compute.
        """
        now = self._now()
        targets: list[RouteTarget] = []
        for entry in self._sorted():
            if not entry.provider.enabled:
                continue
            for model in entry.provider.models:
                if not entry.runtime.serves(model.upstream_id):
                    continue
                targets.append(
                    RouteTarget(
                        target_id=self.target_id(entry.provider.provider_id, model.upstream_id),
                        kind=TargetKind.REMOTE,
                        backend_url=entry.provider.base_url,
                        weight=0.0,
                        outstanding=int(entry.runtime.outstanding.get(model.upstream_id, 0)),
                        healthy=entry.runtime.healthy,
                        admitting=entry.runtime.admitting(now, model.upstream_id),
                        strength=0.0,
                        cost_per_mtok=self.blended_cost(model),
                    )
                )
        return targets

    def route_targets_by_served_name(self) -> dict[str, list[RouteTarget]]:
        """Targets keyed by the name clients use, which is how G merges them.

        Several remotes can share a ``served_name``, and a local deployment can
        share it too. That is the spill mechanism, not a collision.
        """
        by_name: dict[str, list[RouteTarget]] = {}
        index = {t.target_id: t for t in self.route_targets()}
        for entry in self._sorted():
            if not entry.provider.enabled:
                continue
            for model in entry.provider.models:
                if not entry.runtime.serves(model.upstream_id):
                    continue
                target = index.get(self.target_id(entry.provider.provider_id, model.upstream_id))
                if target is not None:
                    by_name.setdefault(model.served_name, []).append(target)
        return by_name

    # -- forwarding --------------------------------------------------------


    def _prepare(
        self,
        provider_id: str,
        upstream_id: str,
        body: dict | None,
        stream: bool,
        endpoint: str,
        content: bytes | None = None,
        content_type: str | None = None,
        tool_names: dict[str, str] | None = None,
    ) -> tuple[_Entry, str, dict[str, str], dict | bytes]:
        """Every admission check, then the URL, headers and payload to send.

        ``content`` is an opaque upload (a multipart transcription): it gets
        the same gate -- enabled, ``serves()``, rate limit, budget -- and is
        sent byte for byte under ``content_type``, so the payload returned is
        those bytes rather than a dict (finding K5). The caller has already
        spliced the upstream's model name into it.
        """
        entry = self._entry(provider_id)
        if not entry.spec.forwardable:
            raise AdapterUnsupportedError(
                f"provider {provider_id!r}: {entry.spec.unsupported_reason}"
            )
        now = self._now()
        if not entry.provider.enabled:
            raise ProviderNotAdmittingError(provider_id, "provider is disabled")
        # The routing index that chose this target is cached for its own TTL,
        # so a request can arrive for a model the operator switched off a
        # moment ago. Refusing here costs a dict lookup and is the difference
        # between a stale index and a billed call to a model nobody enabled.
        if not entry.runtime.serves(upstream_id):
            raise ProviderNotAdmittingError(
                provider_id, f"{upstream_id} is not enabled on this provider"
            )
        # Rate limit and budget are hard stops. Merely unhealthy is not: a
        # provider recovers on the next request that succeeds.
        if entry.runtime.rate_limited(now, upstream_id):
            raise ProviderNotAdmittingError(
                provider_id,
                f"rate limited for another {entry.runtime.retry_in(now, upstream_id):.0f}s",
                retry_after_s=entry.runtime.retry_in(now, upstream_id),
            )
        budget_block = entry.runtime.budget_block(now)
        if budget_block is not None:
            # The same sentence the providers screen shows.
            raise ProviderNotAdmittingError(provider_id, budget_block)
        if entry.spec.chat_wire is ChatWire.RESPONSES:
            return self._prepare_responses(entry, upstream_id, body, endpoint)
        if entry.spec.chat_wire is ChatWire.ANTHROPIC_MESSAGES:
            if endpoint.strip("/") != "chat/completions" or content is not None:
                raise AdapterUnsupportedError(f"provider {provider_id!r}: {_CLAUDE_ONLY}")
            clean_body = {k: v for k, v in (body or {}).items() if k not in _CLIENT_ROUTING_FIELDS}
            try:
                translated, names = chat_to_messages(clean_body, upstream_id)
            except ValueError as exc:
                raise self._upstream_error(provider_id, 400, str(exc)) from None
            if tool_names is not None:
                tool_names.update(names)
            headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
            return (entry, join_url(entry.spec.base_url, "messages") + "?beta=true",
                    headers, claude.subscription_request(translated))
        key = self.resolve_key(provider_id)
        headers = auth_headers(entry.spec, key)
        if content is not None:
            headers["Content-Type"] = content_type or "application/octet-stream"
            headers["Accept"] = "application/json"
            return entry, join_url(entry.provider.base_url, endpoint), headers, content
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "text/event-stream" if stream else "application/json"

        payload = dict(body or {})
        payload["model"] = upstream_id
        # OpenRouter's own routing fields name *other* models and backends:
        # forwarded, a key holder is served a model outside its allowlist at
        # that model's price (K-2). Routing is the operator's, via the pin.
        for name in _CLIENT_ROUTING_FIELDS:
            payload.pop(name, None)
        if entry.spec.supports_backend_routing:
            pin = entry.runtime.backend_pins.get(upstream_id)
            if pin:
                # Constrain OpenRouter to the backend host the operator chose
                # -- over anything the caller sent. A client `provider` field
                # is the key holder overriding the operator's routing, prices
                # included (finding K7). With the fallback flag the pinned
                # host is tried first and OpenRouter may move on when it is
                # down; without it, that host or nothing.
                if entry.runtime.backend_fallbacks.get(upstream_id):
                    payload["provider"] = {"order": [pin], "allow_fallbacks": True}
                else:
                    payload["provider"] = {"only": [pin]}
        # Only where the endpoint actually has a `stream` field. /v1/audio/speech
        # does not, and a strict upstream answers 400 for an unknown one -- so
        # injecting it unconditionally would break every audio request that ever
        # reached a provider.
        if _accepts_stream(endpoint):
            payload["stream"] = bool(stream)
        if stream and entry.spec.supports_stream_usage:
            # Asked wherever the upstream is known to accept the field, priced
            # or not, and *over* the client's own `stream_options`: a client
            # that sends `include_usage: false` would otherwise stream without
            # a usage frame and be metered on an estimate (finding H5). The
            # client's other options are kept.
            options = payload.get("stream_options")
            payload["stream_options"] = {
                **(options if isinstance(options, dict) else {}),
                "include_usage": True,
            }

        return entry, url, headers, payload

    def _prepare_responses(
        self, entry: _Entry, upstream_id: str, body: dict, endpoint: str
    ) -> tuple[_Entry, str, dict[str, str], dict]:
        """A Chat Completions request rewritten for the Responses API.

        No credential yet: the token may need refreshing, which is a network
        call, and this runs on a worker thread. ``open_upstream`` adds it.
        """
        provider_id = entry.provider.provider_id
        if endpoint.strip("/") != "chat/completions":
            raise AdapterUnsupportedError(f"provider {provider_id!r}: {_RESPONSES_ONLY}")
        try:
            payload = chat_to_responses(body, upstream_id)
        except ValueError as exc:
            raise self._upstream_error(provider_id, 400, str(exc)) from None
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        return entry, join_url(entry.provider.base_url, "responses"), headers, payload

    @asynccontextmanager
    async def open_upstream(
        self,
        provider_id: str,
        upstream_id: str,
        body: dict | None,
        stream: bool = False,
        *,
        endpoint: str = "chat/completions",
        content: bytes | None = None,
        content_type: str | None = None,
        request_id: str | None = None,
    ) -> AsyncIterator[UpstreamResponse]:
        """Open a forwarded request. Status and headers are known on entry.

        The gateway uses this when it wants to mirror the upstream's status
        code and content type. :meth:`forward` is the plain byte-stream form.

        A Responses-wire kind is always asked for a stream -- the upstream
        accepts nothing else -- and its events are translated back into Chat
        Completions as they arrive. A client that did not ask for a stream gets
        the whole stream read here first and one ``chat.completion`` body,
        which is also the only way a failure the upstream reports *inside* the
        stream can become an error status rather than a 200.

        ``content``/``content_type`` forward an opaque upload instead of
        ``body`` (see :meth:`_prepare`). ``request_id`` is derate's trace id:
        sent upstream as ``X-Request-Id`` and handed back in the response
        headers, so a provider's log names the same request the ``requests``
        row does -- never the client's own id (finding K10).
        """
        tool_names: dict[str, str] = {}
        entry, url, headers, payload = await asyncio.to_thread(
            self._prepare, provider_id, upstream_id, body, stream, endpoint,
            content, content_type, tool_names,
        )
        if request_id:
            headers["X-Request-Id"] = request_id
        send = {"content": payload} if isinstance(payload, bytes) else {"json": payload}
        responses_wire = entry.spec.chat_wire is ChatWire.RESPONSES
        messages_wire = entry.spec.chat_wire is ChatWire.ANTHROPIC_MESSAGES
        adapted_wire = responses_wire or messages_wire
        client = self._client()
        timeout = httpx.Timeout(
            connect=CONNECT_TIMEOUT_S,
            read=STREAM_READ_TIMEOUT_S if stream or adapted_wire else READ_TIMEOUT_S,
            write=WRITE_TIMEOUT_S,
            pool=CONNECT_TIMEOUT_S,
        )
        runtime = entry.runtime
        runtime.outstanding[upstream_id] += 1
        sniffer = UsageSniffer(stream=stream, metered=entry.spec.meters_cost)
        converter: ResponsesToChat | MessagesToChat | None = None
        try:
            if entry.spec.sign_in:
                token = await self._access_token(entry)
                if messages_wire:
                    headers.update(claude.inference_headers(token))
                else:
                    headers["Authorization"] = "Bearer " + token
            attempt = 0
            reauthorized = False
            while True:
                now = self._now()
                try:
                    context = client.stream(
                        "POST", url, headers=headers, timeout=timeout, **send
                    )
                    response = await context.__aenter__()
                except httpx.HTTPError as exc:
                    runtime.note_transport_error(now, type(exc).__name__)
                    raise UpstreamError(
                        entry.provider.provider_id,
                        502,
                        f"could not reach upstream: {type(exc).__name__}",
                    ) from None

                if response.status_code < 400:
                    break

                raw = await self._read_error(response)
                await context.__aexit__(None, None, None)
                message = self._error_message(response.status_code, raw)
                status = response.status_code
                retry_after = response.headers.get("retry-after")

                if status == 401 and adapted_wire and not reauthorized:
                    # The token can be revoked or rotated out from under its
                    # expiry time. One forced refresh, then the answer stands.
                    reauthorized = True
                    token = await self._access_token(
                        entry, force=True, rejected_token=headers["Authorization"][7:]
                    )
                    headers["Authorization"] = "Bearer " + token
                    continue
                if messages_wire:
                    error = _json_object(raw).get("error")
                    code = error.get("type") if isinstance(error, dict) else None
                    raw = self._upstream_error(
                        provider_id, status, message,
                        code=self.redactor.scrub(code) if isinstance(code, str) else None,
                    ).body.encode()
                if status == 429:
                    seconds = runtime.note_rate_limit(now, upstream_id, retry_after, message)
                    raise UpstreamError(
                        entry.provider.provider_id, status, message,
                        body=self.redactor.scrub(raw.decode("utf-8", "replace")),
                        retry_after_s=seconds,
                    )
                if status in (401, 403):
                    runtime.note_auth_failure(
                        now, status, self._credential_label(entry), message,
                        sign_in=bool(entry.spec.sign_in),
                    )
                    raise UpstreamError(
                        entry.provider.provider_id, status, message,
                        body=self.redactor.scrub(raw.decode("utf-8", "replace")),
                    )
                if status >= 500 and attempt < SERVER_ERROR_RETRIES:
                    attempt += 1
                    await asyncio.sleep(jittered_delay())
                    continue
                if status >= 500:
                    runtime.note_server_error(now, status, message)
                raise UpstreamError(
                    entry.provider.provider_id,
                    status,
                    message,
                    body=self.redactor.scrub(raw.decode("utf-8", "replace")),
                    retry_after_s=parse_retry_after(retry_after, now),
                )

            if messages_wire:
                converter = MessagesToChat(
                    stream=stream, model=upstream_id, now=self._now(),
                    tool_names=tool_names, scrub=self.redactor.scrub,
                )
            elif responses_wire:
                converter = ResponsesToChat(stream=stream, model=upstream_id, now=self._now())

            # Whether anybody started reading the body, and how many SSE frames
            # went past: what a stream the client walked away from is billed
            # on when no usage frame arrived before it went.
            progress = {"started": False, "frames": 0}

            async def body_iter() -> AsyncIterator[bytes]:
                progress["started"] = True
                def note_failure() -> None:
                    if converter.error is None or upstream.error is not None:
                        return
                    status, code, message = converter.error
                    message = self.redactor.scrub(message)
                    code = self.redactor.scrub(code)
                    seconds = self._note_inband_failure(
                        entry, upstream_id, self._now(), (status, code, message)
                    )
                    upstream.error = self._upstream_error(
                        provider_id, status, message, code=code or None,
                        retry_after_s=seconds,
                    )
                if converter is None:
                    async for chunk in response.aiter_bytes():
                        sniffer.feed(chunk)
                        if stream:
                            progress["frames"] += chunk.count(b"data:")
                        yield chunk
                elif not stream:
                    whole = converter.finish()
                    sniffer.feed(whole)
                    yield whole
                else:
                    async for chunk in response.aiter_bytes():
                        frames = converter.feed(chunk)
                        note_failure()
                        for frame in frames:
                            if upstream.error is not None:
                                frame = self.redactor.scrub_bytes(frame)
                            sniffer.feed(frame)
                            progress["frames"] += frame.count(b"data:")
                            yield frame
                    if isinstance(converter, MessagesToChat):
                        final = converter.finish()
                        note_failure()
                        if final:
                            sniffer.feed(final)
                            progress["frames"] += final.count(b"data:")
                            yield final

            if converter is None:
                out_headers = self._safe_headers(response.headers, request_id)
                media_type = response.headers.get("content-type")
            else:
                out_headers = {
                    name: value
                    for name, value in self._safe_headers(response.headers, request_id).items()
                    if name.lower() not in ("content-type", "content-length")
                }
                media_type = "text/event-stream" if stream else "application/json"
            clean = False
            try:
                if converter is not None and not stream:
                    await self._drain_adapted_stream(entry, upstream_id, response, converter)
                upstream = UpstreamResponse(
                    status_code=response.status_code,
                    headers=out_headers,
                    media_type=media_type,
                    body=body_iter(),
                )
                yield upstream
                clean = True
            finally:
                await context.__aexit__(None, None, None)
                # Spend is recorded for any body somebody read, however it
                # ended: a provider bills the prompt and every token it sent
                # before the client hung up or the transfer broke (finding
                # H5). A body nobody started -- a hedge that lost -- was
                # never read and is not ours to bill.
                if clean or progress["started"]:
                    try:
                        self._record_usage(
                            entry,
                            upstream_id,
                            sniffer,
                            body=body,
                            frames=progress["frames"],
                        )
                    except Exception:
                        log.exception(
                            "provider %s: could not record usage", provider_id
                        )
            failure = converter.error if converter is not None else None
            if failure is None:
                runtime.note_success(self._now(), upstream_id)
        finally:
            runtime.outstanding[upstream_id] -= 1
            if runtime.outstanding[upstream_id] <= 0:
                del runtime.outstanding[upstream_id]

    async def _drain_adapted_stream(
        self, entry: _Entry, upstream_id: str, response: httpx.Response,
        converter: ResponsesToChat | MessagesToChat,
    ) -> None:
        """Read a whole translated stream for a client that asked for one body.

        The caller owns connection cleanup, including cancellation while this
        drains before yielding a response. Errors here become non-200 replies.
        """
        provider_id = entry.provider.provider_id
        try:
            async for chunk in response.aiter_bytes():
                converter.feed(chunk)
        except httpx.HTTPError as exc:
            entry.runtime.note_transport_error(self._now(), type(exc).__name__)
            raise UpstreamError(
                provider_id, 502, f"could not reach upstream: {type(exc).__name__}"
            ) from None
        if isinstance(converter, MessagesToChat):
            converter.finish()
        failure = converter.error
        if failure is None and not converter.finished:
            failure = (502, "", f"the {entry.spec.display_name} stream ended before the response completed")
        if failure is None:
            return
        status, code, message = failure
        seconds = self._note_inband_failure(entry, upstream_id, self._now(), failure)
        raise self._upstream_error(
            provider_id, status, message, code=code or None, retry_after_s=seconds
        )

    def _note_inband_failure(
        self, entry: _Entry, upstream_id: str, now: float, failure: tuple[int, str, str]
    ) -> float | None:
        """What the matching HTTP status would have done to the provider's state.

        Returns the backoff for a rate limit, which is the one number the
        client is owed back.
        """
        status, _code, message = failure
        message = self.redactor.scrub(message)
        runtime = entry.runtime
        if status == 429:
            return runtime.note_rate_limit(now, upstream_id, None, message)
        if status in (401, 403):
            runtime.note_auth_failure(
                now, status, self._credential_label(entry), message,
                sign_in=bool(entry.spec.sign_in),
            )
        elif status >= 500:
            runtime.note_server_error(now, status, message)
        return None

    async def forward(
        self,
        provider_id: str,
        upstream_id: str,
        body: dict,
        stream: bool = False,
        *,
        endpoint: str = "chat/completions",
    ) -> AsyncIterator[bytes]:
        """Forward a request and stream the response back, chunk by chunk.

        Nothing is buffered. Errors are raised as :class:`UpstreamError` before
        the first byte, carrying the upstream's own status code and message.
        """
        async with self.open_upstream(
            provider_id, upstream_id, body, stream, endpoint=endpoint
        ) as response:
            async for chunk in response.body:
                yield chunk

    async def forward_target(
        self, target_id: str, body: dict, stream: bool = False, **kwargs
    ) -> AsyncIterator[bytes]:
        provider_id, upstream_id = self.split_target_id(target_id)
        async for chunk in self.forward(provider_id, upstream_id, body, stream, **kwargs):
            yield chunk

    async def _read_error(self, response: httpx.Response) -> bytes:
        try:
            raw = await response.aread()
        except httpx.HTTPError:
            return b""
        return raw[:ERROR_BODY_LIMIT_BYTES]

    def _error_message(self, status: int, raw: bytes | str) -> str:
        """Pull the upstream's own message out, scrubbed. Never invent one."""
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        text = text.strip()
        message = ""
        if text.startswith("{"):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, dict):
                    message = str(error.get("message") or "")
                elif isinstance(error, str):
                    message = error
                if not message:
                    for key in ("message", "detail", "error_message"):
                        value = payload.get(key)
                        if isinstance(value, str) and value:
                            message = value
                            break
        if not message:
            message = text[:512] or f"HTTP {status}"
        return self.redactor.scrub(message)

    def _safe_headers(
        self, headers: httpx.Headers, request_id: str | None = None
    ) -> dict[str, str]:
        """The allowlisted upstream headers, scrubbed, plus derate's own
        ``x-request-id`` when there is one. Everything else stays here."""
        out: dict[str, str] = {}
        for name, value in headers.items():
            if name.lower() in RESPONSE_HEADER_ALLOWLIST:
                out[name] = self.redactor.scrub(value)
        if request_id:
            out["x-request-id"] = request_id
        return out

    @asynccontextmanager
    async def open_upstream_raw(
        self,
        provider_id: str,
        upstream_id: str,
        content: bytes,
        content_type: str,
        *,
        endpoint: str = "audio/transcriptions",
        request_id: str | None = None,
    ) -> AsyncIterator[UpstreamResponse]:
        """:meth:`open_upstream` for a body this gateway cannot read (a
        multipart upload), kept as its own name so a caller that duck-types the
        providers port finds it or falls back, rather than raising TypeError on
        a keyword an older port predates. Same gate, same retry ladder, same
        usage accounting as the JSON path: it is that path, with the bytes
        forwarded as they came under the caller's own content type. Never
        streaming: a transcription answers with one JSON document."""
        async with self.open_upstream(
            provider_id, upstream_id, None, False,
            endpoint=endpoint, content=content, content_type=content_type,
            request_id=request_id,
        ) as opened:
            yield opened

    def attach_events(self, events: Any) -> None:
        """Point budget crossings at an emitter, after construction.

        `node.py` builds this service before the telemetry bundle exists, so
        the wiring happens in `gateway/app.py` where `GatewayEvents` is in
        scope. Without it the service accounts exactly as before and simply
        reports no crossings.
        """
        self._events = events

    def _check_budget(self, entry: _Entry) -> None:
        """Emit when the cap CROSSES, in either direction, and only then.

        Two ways to cross and both are real: spend rises past a fixed cap, or
        an operator lowers the cap below what has already been spent today. So
        this is called from `_record_usage` and from `update()` rather than
        from one of them.

        `over_budget` was previously only ever ASKED -- at admission, and when
        the providers screen rendered -- so nothing knew when the crossing
        happened, only that it currently held. Comparing against the last
        reported sentence is what turns that into an edge.
        """
        if self._events is None:
            return
        provider_id = entry.provider.provider_id
        try:
            now = self._now()
            block = entry.runtime.budget_block(now)
            if block == self._budget_state.get(provider_id):
                return  # no crossing; a standing condition stays standing
            self._budget_state[provider_id] = block
            day = utc_day(now)
            if block is None:
                self._events.budget_cleared(provider_id, day)
            else:
                self._events.budget_reached(
                    provider_id,
                    detail=block,
                    daily_budget_usd=entry.runtime.daily_budget_usd or 0.0,
                    spend_today_usd=entry.runtime.spend_today(now),
                    day=day,
                )
        except Exception:  # pragma: no cover - reporting may not break serving
            log.debug("could not report a budget crossing", exc_info=True)

    def _record_usage(
        self,
        entry: _Entry,
        upstream_id: str,
        sniffer: UsageSniffer,
        *,
        body: dict | None = None,
        frames: int = 0,
    ) -> None:
        """Add one request to the provider's day.

        The upstream's own usage block when it sent one; otherwise -- a stream
        cut short before its usage frame, or an upstream that never sends one
        -- the gateway's prompt estimate and one token per SSE frame seen.
        Recording nothing let a request that was billed upstream leave the
        daily budget untouched (finding H5).
        """
        usage = sniffer.result()
        if usage is None:
            # An opaque upload (body None) has no prompt to estimate, but it
            # is still a request on the provider's day: zero tokens, counted.
            usage = Usage(
                input_tokens=estimate_prompt_tokens(body) if body is not None else 0,
                output_tokens=max(0, frames),
            )
        model = entry.models_by_upstream.get(upstream_id)
        metered = usage.cost_usd
        rate = model.price_per_minute_usd if model else None
        if metered is None and rate is not None:
            # Audio carries no tokens; a per-minute rate on the upstream's own
            # `duration` is the only price it has (K5r). No rate: unpriced.
            seconds = sniffer.audio_duration_s()
            if seconds is not None:
                metered = rate * seconds / 60.0
        cost = entry.runtime.record_usage(
            self._now(),
            usage.input_tokens,
            usage.output_tokens,
            model.input_cost_per_mtok if model else None,
            model.output_cost_per_mtok if model else None,
            metered_cost_usd=metered,
        )
        entry.runtime.prune_spend(self._now())
        if cost:
            log.debug(
                "provider %s charged %.6f USD (%d in, %d out)",
                entry.provider.provider_id,
                cost,
                usage.input_tokens,
                usage.output_tokens,
            )
        # Spend is written on a timer, not per request. A disk write in the
        # hot path would cost more than the accounting is worth.
        self._spend_dirty = True
        self._check_budget(entry)
        if self._now() - self._spend_persisted_at >= SPEND_PERSIST_INTERVAL_S:
            self._persist()

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Begin the periodic model refresh. Safe to call twice."""
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def _refresh_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._refresh_interval_s)
                await self.refresh_all_async()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - a refresh must never kill the loop
                log.exception("provider refresh cycle failed")

    async def aclose(self) -> None:
        if self._spend_dirty:
            self._persist()
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except (asyncio.CancelledError, Exception):
                pass
            self._refresh_task = None
        for client in list(self._clients.values()):
            if not client.is_closed:
                await client.aclose()
        self._clients.clear()

    def _run_sync(self, make_coro: Callable[[], object]):
        """Run a coroutine from sync code, whether or not a loop is running."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(make_coro())  # type: ignore[arg-type]
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(make_coro())).result()  # type: ignore[arg-type]
