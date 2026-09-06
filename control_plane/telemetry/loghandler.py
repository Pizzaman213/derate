"""Structured logs into the journal.

A handler, not a filter on a logger. providers/service.py:_install_redacting_filter
documents the reason at length: a logging.Filter attached to a parent logger is
never consulted for a child logger's records, so a redaction filter installed
on `control_plane.providers` protected nothing until it was walked onto every
child by hand. That is audit finding M-18, and it shipped inert once already.

A handler sits on the root logger and every record reaches it through the
propagation chain, so there is no equivalent hole. It is also the only thing
that covers the gateway's loggers at all: they are named `gateway.proxy`,
`gateway.router` and so on -- outside the `control_plane.` hierarchy entirely --
and nothing redacts them today.

Volume is the risk. Logs are the one stream with no natural bound, so the
default floor is INFO, DEBUG is opt-in, and a single message is truncated
rather than allowed to carry a megabyte of traceback into the archive.
"""

from __future__ import annotations

import logging
from typing import Any

from . import config
from .records import NULL_SINK, TelemetrySink

#: Never journalled. The journal's own writer logs through this handler, and
#: recording those records would be a loop that only ends when the disk fills.
EXCLUDED_LOGGERS = (
    "control_plane.telemetry",
    "asyncio",
)


class JournalLogHandler(logging.Handler):
    """Copies every record it sees into the telemetry journal."""

    def __init__(
        self,
        sink: TelemetrySink = NULL_SINK,
        *,
        level: str | int = config.LOG_SHIP_LEVEL,
        redactor: Any = None,
        max_chars: int = config.LOG_MESSAGE_MAX_CHARS,
    ) -> None:
        super().__init__(level=level)
        self._sink = sink
        self._redactor = redactor
        self._max_chars = max_chars

    def emit(self, record: logging.LogRecord) -> None:
        name = record.name or ""
        if name.startswith(EXCLUDED_LOGGERS):
            return
        try:
            message = record.getMessage()
        except Exception:
            return
        if record.exc_info:
            try:
                message = f"{message}\n{self.format_exception(record)}"
            except Exception:
                pass
        # Redact before anything is written down, not after.
        if self._redactor is not None:
            try:
                message = self._redactor.scrub(message)
            except Exception:
                # A redactor that cannot run means we cannot prove the line is
                # clean, so the line does not get written.
                return
        if len(message) > self._max_chars:
            message = message[: self._max_chars] + "... [truncated]"
        entry: dict[str, Any] = {
            "ts": record.created,
            "level": record.levelname,
            "logger": name,
            "message": message,
        }
        if record.pathname and record.lineno:
            entry["where"] = f"{record.module}:{record.lineno}"
        try:
            self._sink.log(entry)
        except Exception:
            pass  # a telemetry failure must never break logging

    def format_exception(self, record: logging.LogRecord) -> str:
        import traceback

        return "".join(traceback.format_exception(*record.exc_info))


def install(
    sink: TelemetrySink = NULL_SINK,
    *,
    level: str | None = None,
    redactor: Any = None,
    root: logging.Logger | None = None,
) -> JournalLogHandler | None:
    """Attach the handler to the root logger. Idempotent.

    Call this *after* uvicorn.run() has installed its own configuration, or
    pass log_config=None: uvicorn's LOGGING_CONFIG lands after basicConfig and
    sets propagate=False on its access logger, so a handler attached too early
    never sees an access log line.
    """
    if sink is NULL_SINK or sink is None:
        return None
    root = root or logging.getLogger()
    for existing in root.handlers:
        if isinstance(existing, JournalLogHandler):
            return existing
    handler = JournalLogHandler(
        sink, level=level or config.log_ship_level(), redactor=redactor
    )
    root.addHandler(handler)
    # uvicorn's access and error loggers do not propagate to root, so the
    # handler has to be put on them directly or every HTTP line is lost.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        if not logger.propagate and handler not in logger.handlers:
            logger.addHandler(handler)
    return handler


def uninstall(root: logging.Logger | None = None) -> None:
    root = root or logging.getLogger()
    for logger in [root, *(logging.getLogger(n) for n in
                           ("uvicorn", "uvicorn.error", "uvicorn.access"))]:
        for handler in list(logger.handlers):
            if isinstance(handler, JournalLogHandler):
                logger.removeHandler(handler)
