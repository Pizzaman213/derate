"""Telemetry paths, retention horizons, and env-var bindings.

Kept local to this package. Nothing here is a frozen contract.

Every horizon below is a default that a deployment can move, but the defaults
are chosen against real arithmetic rather than round numbers. At 1 Hz a node
sample is roughly 140 bytes on disk, so thirty days of raw samples is about
365 MB per node -- nothing on a Spark's NVMe. (It was 102 bytes and 265 MB
before the clock-derate and paging columns; both figures are measured against
twenty thousand real GB10 rows, not estimated, so they can be re-measured the
same way when the shape moves again.) A request row is roughly 260
bytes, which is also nothing at demo rates and 2.2 GB/day at a sustained 100
requests per second. That asymmetry is why ``REQUESTS_RAW_RETENTION_S`` is
shorter than ``SAMPLES_RAW_RETENTION_S``: the rollups carry the long story for
the stream that can actually run away, and the cheap stream keeps everything.
"""

from __future__ import annotations

import os
from pathlib import Path

from control_plane.paths import data_dir as _data_dir

DAY_S = 24 * 3600.0

#: Subdirectory of the data root that holds both databases.
TELEMETRY_DIR = "telemetry"
JOURNAL_FILE = "journal.db"
ARCHIVE_FILE = "archive.db"

# -- L1, the per-node journal ------------------------------------------------

#: How long a node keeps its own records once they have been collected. This
#: is the window that survives a coordinator outage, which is the whole reason
#: a worker keeps anything at all: there is no coordinator failover, so a
#: worker that journals nothing loses everything the coordinator was down for.
JOURNAL_RETENTION_S = 72 * 3600.0

#: Hard ceiling on the journal file. Past this, the oldest rows go even if
#: they were never collected -- and that writes a gap marker, because a
#: trimmed window must read as trimmed and never as quiet.
JOURNAL_MAX_BYTES = 512 * 1024 * 1024

#: Rows buffered in memory before the writer thread must catch up. Past this
#: the oldest queued row is dropped and a counter moves. Blocking the caller
#: is never an option: the request path is one of the callers.
JOURNAL_QUEUE_MAX = 8192

#: The writer thread commits when either of these trips. Batching is what
#: keeps a 1 Hz sampler and a busy proxy from paying a durable write each.
JOURNAL_BATCH_ROWS = 512
JOURNAL_BATCH_INTERVAL_S = 0.25

#: How often the journal trims itself.
JOURNAL_TRIM_INTERVAL_S = 300.0

# -- L2, collection ----------------------------------------------------------

#: How often the coordinator drains each node's journal.
SHIP_INTERVAL_S = 5.0

#: Caps on one poll. A backlog drains over several pages rather than in one
#: enormous response that neither side can hold.
#:
#: 500 rather than something larger because ingesting a page is one uninterrupted
#: stretch of JSON parsing and SQLite inserts. At 2000 rows that stretch ran ~15 ms
#: and showed up directly in the gateway's p99 every time the collector fired; at
#: 500 it is ~4 ms, and the drain loop yields between pages so several small
#: stretches cost the tail far less than one big one.
SHIP_MAX_ROWS = 500
SHIP_MAX_BYTES = 1024 * 1024
SHIP_TIMEOUT_S = 10.0

#: Pause between pages of one node's backlog. Yielding alone is not enough:
#: asyncio.sleep(0) gives the loop one turn, and the next page's parse-and-insert
#: begins before the requests that piled up behind the last one have drained.
#: Measured, at 400 rps: without this the collector put ~15 ms on the gateway's
#: p99; the same work paced across short pauses costs a fraction of that, and a
#: collector that takes 200 ms rather than 20 ms to drain a backlog it visits
#: every five seconds is not paying for anything.
SHIP_PAGE_PAUSE_S = 0.005

# -- L3, the coordinator's archive -------------------------------------------

SAMPLES_RAW_RETENTION_S = 30 * DAY_S
REQUESTS_RAW_RETENTION_S = 7 * DAY_S
EVENTS_RETENTION_S = 30 * DAY_S
LOGS_RETENTION_S = 7 * DAY_S

ROLLUP_1M_RETENTION_S = 90 * DAY_S
ROLLUP_1H_RETENTION_S = 400 * DAY_S

#: Ceiling on the archive. Enforced by dropping the oldest raw day and
#: recording it in ``gaps``.
ARCHIVE_MAX_BYTES = 16 * 1024 * 1024 * 1024

#: How often rollups run and expired rows go. On a timer, deliberately:
#: deploy/store.py's purge_expired is called only from reconcile(), so a
#: long-running process never collects. Do not repeat that here.
COMPACT_INTERVAL_S = 60.0

BUCKET_1M_S = 60.0
BUCKET_1H_S = 3600.0

#: Below this span a query answers from raw rows; below the next, from
#: 1-minute rollups; beyond it, from hourly ones. This is what stops a UI
#: asking for 2.6 million rows because someone dragged a date picker.
AUTO_STEP_RAW_MAX_S = 6 * 3600.0
AUTO_STEP_1M_MAX_S = 7 * DAY_S

#: Cap on rows any single history query returns.
QUERY_MAX_ROWS = 5000

#: How far back the registry's in-RAM ring reaches, matching
#: registry/config.py's TELEMETRY_RING_SAMPLES at 1 Hz. Asking it for more than
#: it holds is not an error, but there is no point building the window larger.
TELEMETRY_RING_S = 300

# -- Log capture -------------------------------------------------------------

#: Records below this level stay in the process's stderr and never reach the
#: journal. Logs are the one stream with no natural bound, so the default
#: floor is INFO and DEBUG is a deliberate opt-in.
LOG_SHIP_LEVEL = "INFO"

#: Truncation for a single log message. A traceback is worth keeping; a
#: megabyte of one is not.
LOG_MESSAGE_MAX_CHARS = 8192

#: Journalled only if asked for. Not wrong, just enormous: one row per HTTP
#: request, and the coordinator polls every node's agent at 1 Hz for telemetry
#: plus health and journal drains on top. Measured on a three-node cluster,
#: these two were **99.6%** of the log stream and 81% of every row in the
#: archive, and the journal was reaching its byte cap in ~30 hours against a
#: 72-hour retention -- which trims by dropping the oldest rows and writing a
#: gap marker, so access logging was eating the coordinator-outage window the
#: journal exists to provide.
#:
#: Nothing is lost that is worth keeping. Every ``/v1/*`` request is already a
#: row in ``requests`` with tokens, TTFT, decode, duration, cost and retry
#: reason; an access line is a strictly poorer record of the traffic that
#: matters and the only record of the traffic that does not. ``uvicorn.error``
#: is deliberately NOT here -- startup, shutdown and crash lines live there.
#:
#: Prefixes, matched the same way ``EXCLUDED_LOGGERS`` is. Set
#: ``DERATE_TELEMETRY_QUIET_LOGGERS`` to a comma-separated list to change it,
#: or to the empty string to record everything again.
NOISY_LOGGERS = (
    "uvicorn.access",
    "httpx",
)


def data_dir() -> Path:
    """The data root.

    ``DERATE_DATA_DIR`` is the name every Python component reads --
    providers/config.py, registry/config.py and links/service.py all read it
    independently with the same ``/data`` fallback. The shell exports
    ``DERATE_DATA`` and entrypoint.sh bridges the two; reading the shell
    name here would split them the moment anyone overrides one.
    """
    return _data_dir()


def telemetry_dir(root: Path | str | None = None) -> Path:
    base = Path(root) if root is not None else data_dir()
    return base / TELEMETRY_DIR


def journal_path(root: Path | str | None = None) -> Path:
    return telemetry_dir(root) / JOURNAL_FILE


def archive_path(root: Path | str | None = None) -> Path:
    return telemetry_dir(root) / ARCHIVE_FILE


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def enabled() -> bool:
    """Whether to record anything at all. On by default."""
    return _env_flag("DERATE_TELEMETRY", True)


def retention_days() -> float:
    """Raw-sample horizon, in days. Scales the other raw horizons with it."""
    return _env_float("DERATE_TELEMETRY_RETENTION_DAYS", SAMPLES_RAW_RETENTION_S / DAY_S)


def log_ship_level() -> str:
    return os.environ.get("DERATE_TELEMETRY_LOG_LEVEL", LOG_SHIP_LEVEL).upper()


def quiet_loggers() -> tuple[str, ...]:
    """Logger prefixes not written to the journal.

    Read by the log handler, and by ``/api/history/logs`` as its default
    ``exclude`` -- one list, so a window that predates the handler change reads
    the same as one recorded after it. An explicitly empty value means "record
    and return everything", which is the pre-change behaviour.
    """
    raw = os.environ.get("DERATE_TELEMETRY_QUIET_LOGGERS")
    if raw is None:
        return NOISY_LOGGERS
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def journal_max_bytes() -> int:
    return _env_int("DERATE_TELEMETRY_MAX_BYTES", JOURNAL_MAX_BYTES)


def ship_interval_s() -> float:
    return _env_float("DERATE_TELEMETRY_SHIP_INTERVAL_S", SHIP_INTERVAL_S)
