"""Background scan that adopts a detected runtime without a click.

``detect.py`` and ``gateway/runtime_api.py`` both document a deliberate stance:
detection proposes, a human accepts, because an unowned box appearing in the
routing table is a request going somewhere nobody meant. This loop overrides
that stance on purpose, at an operator's explicit choice
(``GatewaySettings.auto_adopt_runtimes`` / ``DERATE_AUTO_ADOPT_RUNTIMES``) --
it is not a bypass of the safety the manual route enforces, it is the same
check (a positive identification via the runtime's own native API, never a
guess) run on a timer instead of on a click.

Every round does exactly what a human clicking "Adopt" on every node's sheet
would do: probe each roster member with :func:`detect_runtime`, and register
anything found that is not already a provider. Nothing here scans a subnet --
the roster is the same one ``runtime_api.py`` reads from, i.e. nodes a human
already admitted.
"""

from __future__ import annotations

import asyncio
import logging

from .config import RUNTIME_AUTOADOPT_INTERVAL_S
from .detect import detect_runtime
from .service import existing_provider_for

log = logging.getLogger(__name__)


class RuntimeAutoAdopter:
    """Owns one background task. Construct once, ``start()``/``stop()`` around it."""

    def __init__(
        self,
        *,
        registry,
        providers,
        interval_s: float = RUNTIME_AUTOADOPT_INTERVAL_S,
        enabled: bool = True,
    ) -> None:
        self._registry = registry
        self._providers = providers
        self._interval_s = interval_s
        self._enabled = enabled
        self._running = False
        self._task: asyncio.Task | None = None

    def _client(self):
        """The registry's HTTP client, or None on a registry that has no such
        thing. Same duck-typed read as runtime_api.py's helper of the same
        name -- a stub registry is a valid deployment of this surface and
        simply has nothing to probe with."""
        return getattr(self._registry, "_client", None)

    async def scan_once(self) -> list[str]:
        """One round over the roster. Returns the provider_ids newly adopted.

        Never raises: a probe failure or a registration failure for one node
        must not stop the rest of the round, and the round itself must not
        stop the loop (that is enforced by the caller in :meth:`_loop`).
        """
        if not self._enabled:
            return []
        client = self._client()
        if client is None:
            return []
        try:
            nodes = self._registry.list_nodes()
        except Exception:
            log.debug("autoadopt: could not list nodes", exc_info=True)
            return []

        adopted: list[str] = []
        for state in nodes:
            profile = getattr(state, "profile", None)
            address = getattr(profile, "address", None)
            node_id = getattr(profile, "node_id", None)
            if not address or not node_id:
                continue
            try:
                found = await detect_runtime(address, client)
            except Exception:
                log.debug("autoadopt: probe of %s failed", node_id, exc_info=True)
                continue
            if found is None:
                continue
            try:
                existing = existing_provider_for(self._providers.list(), found["base_url"])
            except Exception:
                log.debug("autoadopt: could not list providers", exc_info=True)
                continue
            if existing is not None:
                continue
            # Named for the machine, not the product, for the same reason
            # runtime_api.py's manual route does: several nodes can run the
            # same kind, and "ollama" three times over is a list nobody can
            # act on.
            provider_id = f"{node_id}-{found['kind']}"
            try:
                provider = await self._providers.add_async(
                    {
                        "provider_id": provider_id,
                        "kind": found["kind"],
                        "base_url": found["base_url"],
                    }
                )
            except Exception:
                log.warning(
                    "autoadopt: could not register %s on %s",
                    found["kind"],
                    node_id,
                    exc_info=True,
                )
                continue
            adopted_id = getattr(provider, "provider_id", provider_id)
            adopted.append(adopted_id)
            log.info(
                "auto-adopted %s on %s as provider %s", found["kind"], node_id, adopted_id
            )
        return adopted

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # a bad round must not kill the loop
                log.exception("runtime auto-adopt round failed")
            await asyncio.sleep(self._interval_s)

    async def start(self) -> None:
        """Idempotent. A no-op when disabled, so callers need not check first."""
        if not self._enabled or self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="providers-autoadopt")

    async def stop(self) -> None:
        self._running = False
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
