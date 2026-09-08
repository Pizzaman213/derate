"""One folder on disk for what a derate process says out loud.

Before this module there were no log files. ``basicConfig`` put every record on
stderr and ``telemetry/loghandler.py`` put a redacted copy into SQLite, so the
only plain text an operator could read back was whatever the shell that started
the process happened to redirect -- ``/tmp/derate-live/coordinator.log`` on the
development box, nothing at all inside the container, and a different answer on
every machine. Two files in one folder replaces that:

``node.log``   every record the root logger sees, at ``DERATE_LOG_LEVEL``.
``proxy.log``  the same stream narrowed to :data:`PROXY_LOGGERS` -- the path a
               request takes out to an upstream and back.

**The narrow file is the point, and it is not the same thing as grepping the
wide one.** A traceback's continuation lines do not carry the logger name, so
``grep gateway.proxy node.log`` splits exactly the records worth reading: the
first line of an upstream failure survives and the exception under it does not.
Filtering at the handler keeps whole records. It also matters that the proxy is
a handful of loggers inside a process that is simultaneously a registry, a link
prober, a deployment manager and a 1 Hz sampler -- on a busy coordinator its
lines are a rounding error in the wide file.

Three properties, in this order:

1. **Nothing here fails a startup.** An unwritable folder is a warning and no
   file handlers, never a raised exception -- stderr and the journal are
   untouched. That is the same tradeoff every other writer under the data root
   already makes, and ``paths.py`` explains at length why the fallback has to
   be somewhere writable *before* the write is attempted.

2. **A key never lands in a file.** The redacting filter sits on the
   *handler*, not on a logger. ``providers/service.py::_install_redacting_filter``
   documents why that distinction is load-bearing -- a ``logging.Filter``
   attached to a logger is consulted only for records logged through that
   object, never for a child's -- and the loggers written here are mostly
   ``gateway.*``, outside the ``control_plane.`` hierarchy entirely, where that
   filter has never reached. A handler on the root logger sees them through
   propagation.

   The scrubber lives in ``control_plane/redaction.py`` rather than in
   ``providers/secrets.py`` where it was written, because it is needed on every
   node and ``node.py`` promises the worker path imports nothing from
   ``control_plane.providers``. The instance a file handler starts with is a
   fresh one -- vendor-prefix patterns, no remembered values -- and
   :func:`adopt_redactor` swaps in the provider service's the moment the
   composition root builds one.

3. **The folder is bounded.** Both files rotate. The volume this lands on is
   the one holding the model cache, where 894 GiB of weights leaves no room for
   a log that grows forever.

Installed from the two entrypoints -- ``control_plane/node.py`` and
``control_plane/gateway/main.py`` -- immediately after ``basicConfig``, so a
crash during startup is in the file rather than only on a stderr nobody
captured.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .paths import logs_dir
from .redaction import Redactor, SecretRedactingFilter

log = logging.getLogger(__name__)

#: The wide file: everything the root logger admits.
NODE_LOG = "node.log"

#: The narrow file: the request path, and nothing else.
PROXY_LOG = "proxy.log"

#: What "the proxy" means, as logger prefixes. This is the path a request takes
#: from arriving on ``/v1`` to coming back from an upstream -- the surface
#: chosen, admitted, routed, forwarded, and the two things that can take a
#: target out from under it -- plus every provider module, which is where a
#: remote upstream's own failures are described.
#:
#: Prefixes, not exact names: ``control_plane.providers`` covers the eleven
#: modules under it that each log through ``getLogger(__name__)``, and it keeps
#: covering a twelfth.
PROXY_LOGGERS = (
    "gateway.proxy",
    "gateway.openai",
    "gateway.router",
    "gateway.admission",
    "gateway.parking",
    "gateway.breaker",
    "gateway.budget",
    "gateway.errors",
    "control_plane.providers",
)

#: One format for stderr and for both files, so a line copied out of a file
#: reads the same as the one somebody saw in a terminal. Both entrypoints
#: import this for their ``basicConfig`` rather than retyping it.
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

#: Rotation. 32 MiB is roughly a day of a busy coordinator's INFO stream at the
#: access-log volume measured in ``telemetry/config.py`` (99.6% of records), and
#: four files is the horizon that matters: long enough to still hold the
#: morning's launch when somebody looks after lunch, short enough that the whole
#: folder is bounded at 256 MiB across both files and cannot crowd the weights.
LOG_MAX_BYTES = 32 * 1024 * 1024
LOG_BACKUPS = 3


def _env_flag(name: str, default: bool, env: Mapping[str, str]) -> bool:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int, env: Mapping[str, str]) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether to write files at all. On by default; ``DERATE_LOG_FILES=0`` off.

    An off switch exists because a container that is already having its stderr
    collected by the host's logging driver does not need a second copy, and
    because a read-only estate should be able to say so rather than be found
    out one warning at a time.
    """
    return _env_flag("DERATE_LOG_FILES", True, os.environ if env is None else env)


class _OnlyProxy(logging.Filter):
    """Admits :data:`PROXY_LOGGERS` and rejects everything else."""

    def __init__(self, prefixes: tuple[str, ...] = PROXY_LOGGERS) -> None:
        super().__init__()
        self._prefixes = prefixes

    def filter(self, record: logging.LogRecord) -> bool:
        return (record.name or "").startswith(self._prefixes)


def _redacting_filter(redactor: Any) -> logging.Filter:
    return SecretRedactingFilter(redactor if redactor is not None else Redactor())


def _handler(
    path: Path,
    *,
    level: int | str,
    max_bytes: int,
    backups: int,
    filters: list[logging.Filter],
) -> logging.handlers.RotatingFileHandler:
    # delay=False on purpose: the file is opened here, where an unwritable
    # folder is caught and turns into a warning. Deferred to the first record
    # the same failure surfaces inside emit(), which prints a traceback to
    # stderr for every line logged thereafter.
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8", delay=False
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    for f in filters:
        handler.addFilter(f)
    return handler


def install(
    *,
    level: str | int | None = None,
    directory: Path | str | None = None,
    root: logging.Logger | None = None,
    redactor: Any = None,
    env: Mapping[str, str] | None = None,
) -> Path | None:
    """Attach the two rotating file handlers to the root logger. Idempotent.

    Returns the folder written to, or ``None`` when files are switched off or
    the folder cannot be used. Never raises.

    *redactor* is the provider subsystem's when there is one. There is not, at
    the moment this is called: the entrypoints install logging before the
    composition root builds a ``ProviderService``, so a fresh
    :class:`~control_plane.redaction.Redactor` starts the process -- which
    still applies the vendor-prefix patterns, just not any *remembered* value
    -- and :func:`adopt_redactor` upgrades it as soon as one exists.
    """
    env = os.environ if env is None else env
    if not enabled(env):
        return None

    root = root or logging.getLogger()
    existing = [h for h in root.handlers if getattr(h, "_derate_logfile", None)]
    if existing:
        return Path(existing[0].baseFilename).parent

    folder = Path(directory) if directory is not None else logs_dir(env)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("no log folder at %s (%s); logging to stderr only", folder, exc)
        return None

    redact = _redacting_filter(redactor)

    level = level if level is not None else env.get("DERATE_LOG_LEVEL", "INFO")
    max_bytes = _env_int("DERATE_LOG_MAX_BYTES", LOG_MAX_BYTES, env)
    backups = _env_int("DERATE_LOG_BACKUPS", LOG_BACKUPS, env)

    made: list[logging.Handler] = []
    try:
        for name, extra in ((NODE_LOG, []), (PROXY_LOG, [_OnlyProxy()])):
            handler = _handler(
                folder / name,
                level=level,
                max_bytes=max_bytes,
                backups=backups,
                filters=[redact, *extra],
            )
            handler._derate_logfile = name  # type: ignore[attr-defined]
            made.append(handler)
    except OSError as exc:
        for handler in made:
            handler.close()
        log.warning("could not open log files in %s (%s); stderr only", folder, exc)
        return None

    for handler in made:
        root.addHandler(handler)
    # Logged after the handlers are attached, so the first line in the file
    # says where the file is -- which is the line somebody reading a copied
    # excerpt needs and never has.
    log.info("logging to %s (%s, %s)", folder, NODE_LOG, PROXY_LOG)
    return folder


def adopt_redactor(redactor: Any, root: logging.Logger | None = None) -> None:
    """Point the installed handlers at the provider service's redactor.

    A :class:`~control_plane.redaction.Redactor` only scrubs values it has been
    told to remember, and the provider service is what remembers them
    -- ``telemetry/service.py::_provider_redactor`` shares the instance for the
    same reason. Called from the composition root right where the service is
    built. A no-op when no files are installed, so the stub gateway and the
    tests do not have to care.
    """
    if redactor is None:
        return
    root = root or logging.getLogger()
    replacement = _redacting_filter(redactor)
    for handler in root.handlers:
        if not getattr(handler, "_derate_logfile", None):
            continue
        for existing in list(handler.filters):
            if isinstance(existing, SecretRedactingFilter):
                handler.removeFilter(existing)
        handler.addFilter(replacement)


def uninstall(root: logging.Logger | None = None) -> None:
    """Detach and close the file handlers. For tests and for a clean shutdown."""
    root = root or logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_derate_logfile", None):
            root.removeHandler(handler)
            handler.close()


def paths(env: Mapping[str, str] | None = None) -> dict[str, Path]:
    """Where the two files are, whether or not anything is installed."""
    folder = logs_dir(os.environ if env is None else env)
    return {"dir": folder, "node": folder / NODE_LOG, "proxy": folder / PROXY_LOG}
