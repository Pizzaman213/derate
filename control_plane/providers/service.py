"""The provider service: registry, discovery, forwarding, health, spend.

Implements :class:`~control_plane.contracts.ports.ProviderPort` and the three
extras Agent G routes on: :meth:`ProviderService.forward`,
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
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..contracts.providers import Provider, ProviderKind, ProviderModel
from ..contracts.routing import RouteTarget, TargetKind
from .config import (
    COST_BLEND_INPUT_WEIGHT,
    COST_BLEND_OUTPUT_WEIGHT,
    CONNECT_TIMEOUT_S,
    DISCOVERY_TIMEOUT_S,
    ERROR_BODY_LIMIT_BYTES,
    PROVIDER_REFRESH_S,
    READ_TIMEOUT_S,
    SERVER_ERROR_RETRIES,
    SPEND_PERSIST_INTERVAL_S,
    STREAM_READ_TIMEOUT_S,
    WRITE_TIMEOUT_S,
    data_dir,
)
from .discovery import parse_models, recognized_envelope
from .errors import (
    PullRefusedError,
    PullUnsupportedError,
    AdapterUnsupportedError,
    MissingKeyError,
    ProviderError,
    ProviderNotAdmittingError,
    UnknownProviderError,
    UpstreamError,
)
from .kinds import KindSpec, auth_headers, join_url, native_base, spec_for
from .runtime import ProviderRuntime, jittered_delay, parse_retry_after
from .secrets import Redactor, SecretRedactingFilter, SecretStore, looks_like_secret
from .serialization import assert_no_key_material, provider_public_dict
from .store import ProviderStore
from .usage import UsageSniffer

log = logging.getLogger(__name__)


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

_ID_SAFE = re.compile(r"[^a-z0-9._-]+")

#: Endpoints whose request body has a `stream` field. Everything else -- today
#: that means /v1/audio/speech and /v1/audio/transcriptions -- must not have one
#: added, because a strict upstream rejects an unknown field outright.
_STREAMING_ENDPOINTS = ("chat/completions", "completions")


def _accepts_stream(endpoint: str) -> bool:
    tail = endpoint.strip("/")
    return any(tail.endswith(name) for name in _STREAMING_ENDPOINTS)


@dataclass
class UpstreamResponse:
    """A live upstream response, headers known, body not yet read."""

    status_code: int
    headers: dict[str, str]
    media_type: str | None
    body: AsyncIterator[bytes]


# Hop-by-hop and framing headers we must not copy onto our own response.
_DROP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "content-encoding",
    "authorization",
    "x-api-key",
    "set-cookie",
}


@dataclass
class _Entry:
    provider: Provider
    runtime: ProviderRuntime
    spec: KindSpec
    # Set when we disabled the provider ourselves for an unresolvable key, so
    # we know it is ours to re-enable once the reference resolves.
    auto_disabled: bool = False
    models_by_upstream: dict[str, ProviderModel] = field(default_factory=dict)

    def reindex(self) -> None:
        self.models_by_upstream = {m.upstream_id: m for m in self.provider.models}


class ProviderService:
    """Remote OpenAI-compatible upstreams as first-class route targets."""

    def __init__(
        self,
        *,
        data_path: Path | None = None,
        store: ProviderStore | None = None,
        secrets: SecretStore | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        now: Callable[[], float] = time.time,
        refresh_interval_s: float = PROVIDER_REFRESH_S,
    ) -> None:
        root = Path(data_path) if data_path is not None else data_dir()
        self.redactor = Redactor()
        self.secrets = secrets or SecretStore(root / "secrets.json", redactor=self.redactor)
        # Share one redactor so a value resolved anywhere is scrubbed everywhere.
        self.redactor = self.secrets.redactor
        self.store = store or ProviderStore(root / "providers.json", redactor=self.redactor)
        self.store.redactor = self.redactor
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
            follow_redirects=True,
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
            entry.reindex()
            self._entries[provider.provider_id] = entry
        # A missing key disables a provider. It never fails startup.
        for entry in self._entries.values():
            self._check_key(entry)
        if self._entries:
            log.info("loaded %d provider(s) from disk", len(self._entries))

    @staticmethod
    def _sync(entry: _Entry) -> _Entry:
        """Mirror live runtime state onto the contract record before it is read."""
        entry.provider.healthy = entry.runtime.healthy
        entry.provider.last_error = entry.runtime.last_error
        entry.provider.last_refreshed = entry.runtime.last_refreshed
        return entry

    def _persist(self) -> None:
        self._spend_dirty = False
        self._spend_persisted_at = self._now()
        entries = [self._sync(e) for e in self._entries.values()]
        self.store.save([(e.provider, e.runtime) for e in entries])

    # -- keys --------------------------------------------------------------

    def _check_key(self, entry: _Entry) -> bool:
        """Resolve the key reference. Disable on a miss, re-enable on a return."""
        if not entry.spec.requires_key and not entry.provider.api_key_ref:
            entry.runtime.clear_missing_key()
            return True
        if self.secrets.has(entry.provider.api_key_ref):
            if entry.runtime.missing_key_ref is not None:
                entry.runtime.clear_missing_key()
                entry.runtime.healthy = True
                entry.runtime.last_error = None
                if entry.auto_disabled:
                    entry.provider.enabled = True
                    entry.auto_disabled = False
                    log.info(
                        "provider %s re-enabled; api_key_ref %s now resolves",
                        entry.provider.provider_id,
                        entry.provider.api_key_ref,
                    )
            return True
        if entry.provider.enabled:
            entry.auto_disabled = True
        entry.provider.enabled = False
        entry.runtime.note_missing_key(entry.provider.api_key_ref)
        entry.provider.healthy = False
        entry.provider.last_error = entry.runtime.last_error
        log.warning(
            "provider %s disabled: api_key_ref %s does not resolve",
            entry.provider.provider_id,
            entry.provider.api_key_ref,
        )
        return False

    def resolve_key(self, provider_id: str) -> str:
        """Request time only. The one place a value is ever produced."""
        entry = self._entry(provider_id)
        if not entry.spec.requires_key and not entry.provider.api_key_ref:
            return ""
        value = self.secrets.get(entry.provider.api_key_ref)
        if value is None:
            raise MissingKeyError(provider_id, entry.provider.api_key_ref)
        return value

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

    def _register(self, spec: dict) -> str:
        kind = ProviderKind(spec.get("kind", ProviderKind.CUSTOM))
        kind_spec = spec_for(kind)

        base_url = str(spec.get("base_url") or kind_spec.base_url).strip()
        if not base_url:
            raise ValueError(f"{kind.value} needs a base_url")
        if looks_like_secret(base_url):
            raise ValueError(
                "base_url looks like it contains key material; put the key in an "
                "environment variable or secrets.json and reference it by name"
            )

        api_key_ref = str(spec.get("api_key_ref") or "").strip()
        if looks_like_secret(api_key_ref):
            # The whole design rests on this field being a name. A key here
            # would be displayed everywhere a reference safely can be.
            raise ValueError(
                "api_key_ref must be the NAME of an environment variable or "
                "secrets.json key, not the key itself"
            )
        if kind_spec.requires_key and not api_key_ref:
            raise ValueError(f"{kind.value} needs an api_key_ref")

        provider_id = str(spec.get("provider_id") or "").strip() or self._mint_id(kind)
        if provider_id in self._entries:
            raise ValueError(f"provider {provider_id!r} already exists")

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
        budget = spec.get("daily_budget_usd")
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
            daily_budget_usd=None if budget is None else float(budget),
            aliases=aliases,
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
    ) -> dict:
        """Tell a self-hosted provider to fetch *model*, then re-read its catalogue.

        Streamed rather than awaited whole, for one reason that matters: the
        response's first frames carry the download total, and that is the only
        moment before several minutes of transfer at which anything can decide
        the weights are too big for the machine. ``on_size`` is called once with
        that total; raising from it aborts the transfer, which is how the
        caller's memory gate refuses without first filling somebody's SD card.

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
        _screen_free_text(model, "model")

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
            value = patch["daily_budget_usd"]
            entry.runtime.daily_budget_usd = None if value is None else float(value)
        if "base_url" in patch:
            base_url = str(patch["base_url"]).strip()
            if looks_like_secret(base_url):
                raise ValueError("base_url looks like it contains key material")
            entry.provider.base_url = base_url
        if "api_key_ref" in patch:
            ref = str(patch["api_key_ref"]).strip()
            if looks_like_secret(ref):
                raise ValueError("api_key_ref must be a reference, not a key")
            entry.provider.api_key_ref = ref
        if "aliases" in patch:
            aliases = {str(k): str(v) for k, v in (patch["aliases"] or {}).items()}
            for alias_key, alias_value in aliases.items():
                _screen_free_text(alias_key, "aliases key")
                _screen_free_text(alias_value, f"aliases[{alias_key!r}]")
            entry.runtime.aliases = aliases
            self._apply_aliases(entry)
        if entry.provider.enabled:
            self._check_key(entry)
        self._persist()
        return self.get(provider_id)

    def remove(self, provider_id: str) -> None:
        self._entry(provider_id)
        del self._entries[provider_id]
        self._persist()
        log.info("removed provider %s", provider_id)

    def _apply_aliases(self, entry: _Entry) -> None:
        for model in entry.provider.models:
            model.served_name = entry.runtime.aliases.get(model.upstream_id, model.upstream_id)
        entry.provider.models.sort(key=lambda m: m.served_name)
        entry.reindex()

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
        url = join_url(entry.provider.base_url, entry.spec.models_path)
        try:
            key = self.resolve_key(provider_id)
        except MissingKeyError:
            self._check_key(entry)
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
                    now, response.status_code, entry.provider.api_key_ref, message
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
        entry.reindex()
        entry.runtime.last_refreshed = now
        entry.runtime.healthy = True
        entry.runtime.last_error = None
        self._persist()
        log.info("provider %s refreshed: %d model(s)", provider_id, len(models))
        return self.get(provider_id)

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

    # -- models ------------------------------------------------------------

    def models(self) -> list[tuple[str, ProviderModel]]:
        """(provider_id, model) for every enabled provider, in priority order."""
        out: list[tuple[str, ProviderModel]] = []
        for entry in self._sorted():
            if not entry.provider.enabled:
                continue
            for model in entry.provider.models:
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

        payload = [model_public_dict(m) for m in self._entry(provider_id).provider.models]
        assert_no_key_material(payload, self.redactor, "GET /api/providers/{id}/models")
        return payload

    # -- health and spend --------------------------------------------------

    def health(self, provider_id: str) -> tuple[bool, str | None]:
        entry = self._entry(provider_id)
        return entry.runtime.healthy, entry.runtime.last_error

    def spend_today(self, provider_id: str) -> float:
        return self._entry(provider_id).runtime.spend_today(self._now())

    def admitting(self, provider_id: str) -> bool:
        return self._entry(provider_id).runtime.admitting(self._now())

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
        """Every remote model as a target Agent G can merge with local ones.

        ``strength`` stays zero: it is a normalized score over local hardware
        and we have not measured a remote. ``weight`` is Agent G's to compute.
        """
        now = self._now()
        targets: list[RouteTarget] = []
        for entry in self._sorted():
            if not entry.provider.enabled:
                continue
            admitting = entry.runtime.admitting(now)
            for model in entry.provider.models:
                targets.append(
                    RouteTarget(
                        target_id=self.target_id(entry.provider.provider_id, model.upstream_id),
                        kind=TargetKind.REMOTE,
                        backend_url=entry.provider.base_url,
                        weight=0.0,
                        outstanding=int(entry.runtime.outstanding.get(model.upstream_id, 0)),
                        healthy=entry.runtime.healthy,
                        admitting=admitting,
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
                target = index.get(self.target_id(entry.provider.provider_id, model.upstream_id))
                if target is not None:
                    by_name.setdefault(model.served_name, []).append(target)
        return by_name

    # -- forwarding --------------------------------------------------------


    def _prepare(
        self, provider_id: str, upstream_id: str, body: dict, stream: bool, endpoint: str
    ) -> tuple[_Entry, str, dict[str, str], dict]:
        entry = self._entry(provider_id)
        if not entry.spec.forwardable:
            raise AdapterUnsupportedError(
                f"provider {provider_id!r}: {entry.spec.unsupported_reason}"
            )
        now = self._now()
        if not entry.provider.enabled:
            raise ProviderNotAdmittingError(provider_id, "provider is disabled")
        # Rate limit and budget are hard stops. Merely unhealthy is not: a
        # provider recovers on the next request that succeeds.
        if entry.runtime.rate_limited(now):
            raise ProviderNotAdmittingError(
                provider_id,
                f"rate limited for another {entry.runtime.retry_in(now):.0f}s",
                retry_after_s=entry.runtime.retry_in(now),
            )
        if entry.runtime.over_budget(now):
            raise ProviderNotAdmittingError(
                provider_id,
                f"daily budget of ${entry.runtime.daily_budget_usd:.2f} reached",
            )

        key = self.resolve_key(provider_id)
        headers = auth_headers(entry.spec, key)
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "text/event-stream" if stream else "application/json"

        payload = dict(body)
        payload["model"] = upstream_id
        # Only where the endpoint actually has a `stream` field. /v1/audio/speech
        # does not, and a strict upstream answers 400 for an unknown one -- so
        # injecting it unconditionally would break every audio request that ever
        # reached a provider.
        if _accepts_stream(endpoint):
            payload["stream"] = bool(stream)
        model = entry.models_by_upstream.get(upstream_id)
        priced = model is not None and self.blended_cost(model) is not None
        if (
            stream
            and priced
            and entry.spec.supports_stream_usage
            and "stream_options" not in payload
        ):
            # Only asked for where we can price it and the upstream is known to
            # accept the field. Spend accounting is not worth a 400.
            payload["stream_options"] = {"include_usage": True}

        url = join_url(entry.provider.base_url, endpoint)
        return entry, url, headers, payload

    @asynccontextmanager
    async def open_upstream(
        self,
        provider_id: str,
        upstream_id: str,
        body: dict,
        stream: bool = False,
        *,
        endpoint: str = "chat/completions",
    ) -> AsyncIterator[UpstreamResponse]:
        """Open a forwarded request. Status and headers are known on entry.

        The gateway uses this when it wants to mirror the upstream's status
        code and content type. :meth:`forward` is the plain byte-stream form.
        """
        entry, url, headers, payload = self._prepare(
            provider_id, upstream_id, body, stream, endpoint
        )
        client = self._client()
        timeout = httpx.Timeout(
            connect=CONNECT_TIMEOUT_S,
            read=STREAM_READ_TIMEOUT_S if stream else READ_TIMEOUT_S,
            write=WRITE_TIMEOUT_S,
            pool=CONNECT_TIMEOUT_S,
        )
        runtime = entry.runtime
        runtime.outstanding[upstream_id] += 1
        sniffer = UsageSniffer(stream=stream)
        try:
            attempt = 0
            while True:
                now = self._now()
                try:
                    context = client.stream(
                        "POST", url, json=payload, headers=headers, timeout=timeout
                    )
                    response = await context.__aenter__()
                except httpx.HTTPError as exc:
                    runtime.note_transport_error(now, type(exc).__name__)
                    raise UpstreamError(
                        provider_id,
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

                if status == 429:
                    seconds = runtime.note_rate_limit(now, retry_after, message)
                    raise UpstreamError(
                        provider_id, status, message,
                        body=self.redactor.scrub(raw.decode("utf-8", "replace")),
                        retry_after_s=seconds,
                    )
                if status in (401, 403):
                    runtime.note_auth_failure(now, status, entry.provider.api_key_ref, message)
                    raise UpstreamError(
                        provider_id, status, message,
                        body=self.redactor.scrub(raw.decode("utf-8", "replace")),
                    )
                if status >= 500 and attempt < SERVER_ERROR_RETRIES:
                    attempt += 1
                    await asyncio.sleep(jittered_delay())
                    continue
                if status >= 500:
                    runtime.note_server_error(now, status, message)
                raise UpstreamError(
                    provider_id,
                    status,
                    message,
                    body=self.redactor.scrub(raw.decode("utf-8", "replace")),
                    retry_after_s=parse_retry_after(retry_after, now),
                )

            async def body_iter() -> AsyncIterator[bytes]:
                async for chunk in response.aiter_bytes():
                    sniffer.feed(chunk)
                    yield chunk

            try:
                yield UpstreamResponse(
                    status_code=response.status_code,
                    headers=self._safe_headers(response.headers),
                    media_type=response.headers.get("content-type"),
                    body=body_iter(),
                )
            finally:
                await context.__aexit__(None, None, None)
            runtime.note_success(self._now())
            self._record_usage(entry, upstream_id, sniffer)
        finally:
            runtime.outstanding[upstream_id] -= 1
            if runtime.outstanding[upstream_id] <= 0:
                del runtime.outstanding[upstream_id]

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

    def _safe_headers(self, headers: httpx.Headers) -> dict[str, str]:
        out: dict[str, str] = {}
        for name, value in headers.items():
            if name.lower() in _DROP_HEADERS:
                continue
            out[name] = self.redactor.scrub(value)
        return out

    def _record_usage(self, entry: _Entry, upstream_id: str, sniffer: UsageSniffer) -> None:
        usage = sniffer.result()
        if usage is None:
            return
        model = entry.models_by_upstream.get(upstream_id)
        cost = entry.runtime.record_usage(
            self._now(),
            usage.input_tokens,
            usage.output_tokens,
            model.input_cost_per_mtok if model else None,
            model.output_cost_per_mtok if model else None,
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
