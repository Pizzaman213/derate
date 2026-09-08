"""The model registry's SQLite schema and connection.

One database, one table per kind of fact, at ``<data_dir>/models.db``. The
idiom is transcribed from ``telemetry/archive.py`` rather than invented: WAL,
``isolation_level=None`` with explicit transactions, a ``busy_timeout`` long
enough that a concurrent reader never surfaces as an error, and a ``meta``
table stamped with the schema version.

Why a database and not another JSON store. The other stores here each hold
one component's records and are read whole -- ``providers.json`` is 150 KB
and rewritten in full on every mutation, which is fine for a file one service
owns. This holds the *union* of five sources, is written by one writer and
read by every screen, and the interesting questions about it are joins: which
models does this node hold, which are served and by whom, which are published
and switched off. Those are the questions a table answers and a document does
not.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

log = logging.getLogger(__name__)

#: 1 is the first. A bump discards the file and rebuilds from the live
#: sources: every row here is derived, so there is nothing to migrate and
#: nothing is lost by starting over. See ``ModelInventory.__init__``.
SCHEMA_VERSION = 1

#: Bumped on every committed refresh. Serves as the ETag and lets a reader
#: tell "nothing changed" from "not refreshed yet".
REVISION_KEY = "revision"

_SCHEMA = """
-- Deliberately narrow. There is no `total_params`, `native_dtype`,
-- `downloads`, `likes` or `tags` column, though the screen renders all of
-- them, because none of the five sources this file is built from knows one:
-- the first two are answers from the fit gate's capacity walk and the rest
-- come from a HuggingFace search. A column here would be NULL on every row
-- forever, and worse than useless -- `rows.check.mjs` fails a row that
-- carries `total_params` without a verdict to have got it from, which is
-- exactly what filling one in from here would produce.
CREATE TABLE IF NOT EXISTS models(
  model_id            TEXT PRIMARY KEY,
  label               TEXT NOT NULL,
  detail              TEXT NOT NULL DEFAULT '',
  default_context     INTEGER,
  default_concurrency INTEGER,
  observed_at         REAL NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS model_facets(
  model_id TEXT NOT NULL,
  facet    TEXT NOT NULL,
  PRIMARY KEY(model_id, facet)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS model_served_names(
  model_id    TEXT NOT NULL,
  served_name TEXT NOT NULL,
  PRIMARY KEY(model_id, served_name)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS model_deployments(
  deployment_id TEXT PRIMARY KEY,
  model_id      TEXT NOT NULL,
  served_name   TEXT NOT NULL,
  state         TEXT NOT NULL,
  runtime       TEXT NOT NULL,
  node_ids      TEXT NOT NULL DEFAULT '[]',
  last_error    TEXT
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS ix_model_deployments_model
  ON model_deployments(model_id);

-- `served` is the whole reason this is one table and not two. The screen
-- draws "served by" and "published, switched off" as separate lists, and
-- built them from two endpoints polled at different intervals -- so for one
-- poll after somebody enabled a model, a row claimed both and the pane drew
-- a Serve button beside a Stop serving one. Here it is one row with a flag,
-- decided once by the writer.
CREATE TABLE IF NOT EXISTS model_provider_rows(
  model_id            TEXT NOT NULL,
  provider_id         TEXT NOT NULL,
  upstream_id         TEXT NOT NULL,
  served_name         TEXT NOT NULL,
  display_name        TEXT NOT NULL,
  served              INTEGER NOT NULL,
  -- Whether the PROVIDER is switched on, which is a different question from
  -- whether this model is in its allowlist, and the two disagree more often
  -- than they look. `ProviderService.servable()` filters by the allowlist and
  -- deliberately does NOT filter by `provider.enabled` -- `build_index` does
  -- that separately -- so an allowlisted model on a disabled provider is
  -- listed by `/api/providers` today while nothing routes to it. Carrying
  -- both means a reader can tell "served" from "would be served if the
  -- provider were on" instead of inheriting that ambiguity.
  provider_enabled    INTEGER,
  context_length      INTEGER,
  modality            TEXT,
  input_cost_per_mtok REAL,
  output_cost_per_mtok REAL,
  supports_tools      INTEGER,
  supports_streaming  INTEGER,
  healthy             INTEGER,
  last_error          TEXT,
  admitting           INTEGER,
  admission_block     TEXT,
  PRIMARY KEY(model_id, provider_id, upstream_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS ix_model_provider_rows_model
  ON model_provider_rows(model_id);

-- `blob_count` is carried rather than a `complete` verdict: whether what is
-- on disk is the whole download needs an expected size, and only the variant
-- ladder ever knows one. A base repository has nothing to check against, and
-- guessing there would call a finished download partial.
CREATE TABLE IF NOT EXISTS model_cache(
  model_id   TEXT NOT NULL,
  node_id    TEXT NOT NULL,
  folder     TEXT NOT NULL,
  bytes      INTEGER NOT NULL,
  blob_count INTEGER,
  PRIMARY KEY(model_id, node_id, folder)
) WITHOUT ROWID;

-- Per-node readability, kept apart from the rows themselves so that "on no
-- node" and "we could not look" stay different answers. A node whose agent
-- did not respond keeps its previous model_cache rows and lands here with
-- available=0 and a sentence; dropping its rows would turn one unreachable
-- worker into a confident claim that nothing is downloaded.
-- `observed_at` is the last SUCCESSFUL read and `attempted_at` the last try.
-- They are separate because a node that has gone away must keep saying how
-- old its surviving rows are while still showing that we are still asking.
-- `observed_at IS NULL` means never measured, which is a third answer again,
-- and it is never rendered as a zero.
CREATE TABLE IF NOT EXISTS cache_scans(
  node_id      TEXT PRIMARY KEY,
  available    INTEGER NOT NULL,
  reason       TEXT,
  observed_at  REAL,
  attempted_at REAL NOT NULL
) WITHOUT ROWID;

-- One row per contributing feed. This exists because collapsing five feeds
-- into one endpoint would otherwise destroy the Models tab's own rule: a feed
-- that fails greys nothing and empties nothing, it prints one line naming the
-- feed and the server's own sentence. With one request there is no failed
-- fetch left for the browser to notice, so the sentence has to travel in the
-- payload or it stops existing.
CREATE TABLE IF NOT EXISTS sources(
  source       TEXT PRIMARY KEY,   -- deployments | providers | catalog | cache
  ok           INTEGER NOT NULL,
  reason       TEXT,
  observed_at  REAL,
  attempted_at REAL NOT NULL,
  rows         INTEGER
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

#: Rewritten wholesale by every fast refresh, in one transaction.
FAST_TABLES = (
    "models",
    "model_facets",
    "model_served_names",
    "model_deployments",
    "model_provider_rows",
)


def connect(path: Path | str) -> sqlite3.Connection:
    """Open (creating if needed) the registry database.

    ``check_same_thread`` is off because the refresh runs through
    ``asyncio.to_thread`` and lands on whichever pool thread is free, exactly
    as the archive's collector does. Every entry point on
    :class:`~control_plane.inventory.service.ModelInventory` takes its lock,
    so the connection is still only used by one thread at a time.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path), timeout=30.0, isolation_level=None, check_same_thread=False
    )
    # BEFORE journal_mode. `PRAGMA journal_mode=WAL` fixes the page size, and
    # SQLite then refuses an auto_vacuum change without saying so -- see
    # telemetry/journal.py::_connect, where that ordering cost a coordinator
    # its entire telemetry record.
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


def open_database(path: Path | str) -> tuple[sqlite3.Connection, threading.RLock]:
    """Connect, apply the schema, and reset the file if its version moved.

    A version mismatch is not an error to raise at a caller. Every row in
    here is derived from a source that is still live, so the cheapest correct
    answer to "this file was written by another schema" is to throw it away
    and let the next refresh -- seconds later -- rebuild it.
    """
    conn = connect(path)
    lock = threading.RLock()
    conn.executescript(_SCHEMA)

    found = _stored_version(conn)
    if found is not None and found != SCHEMA_VERSION:
        log.info(
            "model registry at %s is schema v%s, this build wants v%s; "
            "discarding it and rebuilding from the live sources",
            path,
            found,
            SCHEMA_VERSION,
        )
        _drop_everything(conn)
        conn.executescript(_SCHEMA)

    conn.execute(
        "INSERT OR REPLACE INTO meta(k, v) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    return conn, lock


def _stored_version(conn: sqlite3.Connection) -> int | None:
    try:
        row = conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
    except sqlite3.DatabaseError:
        return None
    if row is None:
        return None
    try:
        return int(row["v"])
    except (TypeError, ValueError):
        return None


def _drop_everything(conn: sqlite3.Connection) -> None:
    for table in (*FAST_TABLES, "model_cache", "cache_scans", "sources", "meta"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
