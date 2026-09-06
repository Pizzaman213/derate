"""Assembling the pieces, and the one switch that turns them all off.

create_app() builds one of these. It owns the node's journal, the
coordinator's archive, the collector that moves rows between them, and the
gateway's event bus.

Two rules govern construction:

Never fail to start because telemetry cannot. A data directory that is
missing, read-only or full degrades to the no-op sink and a log line. The
gateway serving tokens matters more than the gateway remembering that it did.

Off unless there is somewhere to write. The default data root is /data, which
exists in the container (entrypoint.sh creates it) and does not exist on a
development machine or in the test suite. So the container records and
`pytest` does not, without either having to say so.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from control_plane.deploy.events import EventBus

from . import config
from .events import SOURCE_DEPLOY, GatewayEvents, journal_events
from .records import NULL_SINK, TelemetrySink

log = logging.getLogger(__name__)


class Telemetry:
    """The node's telemetry, whole. Inert when disabled."""

    def __init__(
        self,
        journal: Any = None,
        archive: Any = None,
        *,
        coordinator: bool = True,
        reason: str = "",
    ) -> None:
        self.journal = journal
        self.archive = archive
        self.collector = None
        self.coordinator = coordinator
        self.reason = reason
        self.sink: TelemetrySink = journal or NULL_SINK
        self.gateway_events = GatewayEvents(sink=self.sink)
        self._log_handler = None

    @property
    def enabled(self) -> bool:
        return self.journal is not None

    # -- construction ----------------------------------------------------

    @classmethod
    def disabled(cls, reason: str) -> "Telemetry":
        return cls(reason=reason)

    @classmethod
    def from_env(cls, root: Path | str | None = None, node_id: str = "") -> "Telemetry":
        if not config.enabled():
            return cls.disabled("SPARKPLANE_TELEMETRY is off")
        base = Path(root) if root is not None else config.data_dir()
        if not base.is_dir():
            # Not an error. It is how a dev machine and the test suite stay
            # clean without anyone having to opt out.
            return cls.disabled(f"{base} does not exist")
        if not os.access(base, os.W_OK):
            log.warning("telemetry is off: %s is not writable", base)
            return cls.disabled(f"{base} is not writable")
        return cls.open(base, node_id=node_id)

    @classmethod
    def open(
        cls, root: Path | str, node_id: str = "", coordinator: bool = True
    ) -> "Telemetry":
        from .archive import Archive
        from .journal import Journal

        try:
            journal = Journal(config.journal_path(root), node_id=node_id)
            journal.start()
        except Exception as exc:
            log.warning("telemetry is off: could not open the journal (%s)", exc)
            return cls.disabled(f"journal: {type(exc).__name__}")

        archive = None
        if coordinator:
            try:
                archive = Archive(config.archive_path(root))
            except Exception as exc:
                # A journal without an archive still records. The node keeps
                # its own history and a later coordinator can collect it.
                log.warning("telemetry archive unavailable (%s); journalling only", exc)
        return cls(journal, archive, coordinator=coordinator)

    # -- lifecycle -------------------------------------------------------

    async def start(
        self,
        registry: Any = None,
        node_id: str | None = None,
        providers: Any = None,
    ) -> None:
        if not self.enabled:
            log.info("telemetry not recording: %s", self.reason)
            return
        if node_id and not self.journal.node_id:
            self.journal.node_id = node_id
        self.capture_logs(_provider_redactor(providers))
        if self.archive is None:
            return

        from .collector import Collector

        self.collector = Collector(
            self.archive,
            client=_agent_client(registry),
            agents=lambda: _agent_urls(registry),
        )
        # Under the registry's own name for this node when there is one, so
        # the local journal and the roster agree and the coordinator does not
        # end up with a second cursor for itself.
        local_id = (
            getattr(registry, "local_node_id", None) or self.journal.node_id or "local"
        )
        self.journal.node_id = self.journal.node_id or local_id
        self.collector.add_local(local_id, self.journal)
        await self.collector.start()
        log.info(
            "telemetry recording to %s, archiving to %s",
            self.journal.path,
            self.archive.path,
        )

    async def stop(self) -> None:
        if self._log_handler is not None:
            from .loghandler import uninstall

            uninstall()
            self._log_handler = None
        if self.collector is not None:
            await self.collector.stop()
            self.collector = None
        if self.journal is not None:
            self.journal.close()
        if self.archive is not None:
            self.archive.close()

    # -- wiring other producers ------------------------------------------

    def capture_logs(self, redactor: Any = None) -> None:
        """Start journalling log records, redacted.

        The redactor is the provider subsystem's, so any key it has ever
        resolved is scrubbed here too. Without one, only the pattern rules
        apply -- still better than the gateway loggers' current nothing.
        """
        if not self.enabled or self._log_handler is not None:
            return
        from .loghandler import install

        self._log_handler = install(self.sink, redactor=redactor)

    def watch_deployments(self, bus: EventBus) -> None:
        """Copy the deployment manager's events into the journal."""
        journal_events(bus, self.sink, SOURCE_DEPLOY)

    def status(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "reason": self.reason}
        out: dict[str, Any] = {"enabled": True, "journal": self.journal.stats()}
        if self.archive is not None:
            out["archive"] = self.archive.status()
        return out


def _provider_redactor(providers: Any) -> Any:
    """The provider service's redactor, so keys it has resolved are scrubbed.

    Sharing the instance rather than making a new one matters: a Redactor only
    scrubs values it has been told to remember, and the provider service is
    what remembers them.
    """
    return getattr(providers, "redactor", None)


def _agent_urls(registry: Any) -> dict[str, str]:
    """node_id -> agent URL, for every member the registry knows.

    Duck-typed, in the same style internal_api.py uses for the registry's
    optional methods: a stub registry without these simply yields no peers,
    and the coordinator collects only itself.
    """
    if registry is None:
        return {}
    getter = getattr(registry, "agent_urls", None)
    if callable(getter):
        try:
            return dict(getter() or {})
        except Exception:
            return {}
    urls = getattr(registry, "_agent_urls", None)
    return dict(urls) if isinstance(urls, dict) else {}


def _agent_client(registry: Any) -> Any:
    if registry is None:
        return None
    return getattr(registry, "_client", None) or getattr(registry, "client", None)
