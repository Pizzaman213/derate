"""The model registry: one writer, one place to read.

Two refresh paths, because the facts they carry cost wildly different things.
``refresh_fast`` reads deployments and providers, both of which are in-memory
on the coordinator, so it can run whenever something changes.
``refresh_cache`` takes the weights-on-disk half from a fan-out over every
node agent, which is slow and partially fails. Each stamps when it last ran,
and the endpoint says so, because a screen that presents a five-minute-old
disk figure as current is telling a small lie every five minutes.

**This is a materialized view, not a new owner of truth.** A deployment's
state still belongs to ``DeploymentManager`` and ``deployments/*.json``; a
provider's models still belong to ``providers.json``. What lives here is the
*merge* -- one shape, written in one place, instead of the six the browser was
folding. Nothing routes off it: ``/v1/chat/completions`` and ``/v1/models`` go
through the live ``TargetIndex``, because a stale row that sent traffic to a
dead backend, or that promised a name the router had already dropped, would be
a far worse bug than a stale screen.

Neither refresh patches rows. Both are full re-reads of the owning store, so
the worst reachable state is a stale row with an honest timestamp on it --
never a wrong row that no re-read will correct. That property is why the
deployment event tap only marks this dirty and never writes: a tap runs on the
producer's thread, and a SQLite write there would block the bus.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import astuple
from pathlib import Path
from typing import Any

from . import db as _db
from .build import build
from .records import (
    FACET_ORDER,
    CacheScan,
    ModelRecord,
    RowCache,
    RowDeployment,
    RowProvider,
)

log = logging.getLogger(__name__)

#: The feeds named in the payload's ``sources``. A screen that used to notice
#: a failed fetch per feed now has only this to go on.
FAST_SOURCES = ("deployments", "providers", "catalog")


class ModelInventory:
    """Owns the registry database. Every entry point takes the lock."""

    def __init__(self, path: Path | str, clock=time.time) -> None:
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.RLock()
        self._conn = self._open()

    def _open(self) -> sqlite3.Connection:
        try:
            conn, _ = _db.open_database(self.path)
            return conn
        except sqlite3.DatabaseError:
            # A corrupt file must never lock an operator out of a running
            # cluster, and nothing in here is anything but derived, so the
            # recovery is to throw it away. Same posture as every JSON store
            # in this project, which logs and returns empty rather than
            # raising into a request handler.
            log.warning(
                "model registry at %s could not be opened; discarding it and "
                "rebuilding from the live sources",
                self.path,
                exc_info=True,
            )
            for suffix in ("", "-wal", "-shm"):
                try:
                    self.path.with_name(self.path.name + suffix).unlink(missing_ok=True)
                except OSError:
                    log.exception("could not remove %s%s", self.path, suffix)
            conn, _ = _db.open_database(self.path)
            return conn

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:  # pragma: no cover - closing twice is not an error
                pass

    # -- writes ------------------------------------------------------------

    def refresh_fast(
        self,
        *,
        deployments: Sequence[Any] = (),
        provider_facts: Sequence[Mapping[str, Any]] = (),
        catalogues: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        curated: Sequence[Any] = (),
        errors: Mapping[str, str] | None = None,
    ) -> int:
        """Rebuild everything except the on-disk weights.

        ``errors`` names feeds the caller could not read, mapped to the
        sentence to show. A feed that failed keeps whatever it contributed
        last time rather than being silently emptied -- which is the same
        rule ``refresh_cache`` applies per node, for the same reason.
        """
        now = self._clock()
        errors = dict(errors or {})
        records = build(
            deployments=deployments,
            provider_facts=provider_facts,
            catalogues=catalogues,
            curated=curated,
            now=now,
        )
        digest = _digest(records)

        with self._lock:
            if errors:
                # A partial read must not be written as though it were the
                # whole picture: it would delete every row the failed feed
                # owns. Record what happened and leave the tables alone.
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._note_sources(now, errors, counts=None)
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
                return self.revision

            if self._meta(_DIGEST_KEY) == digest:
                # Nothing moved. Rewriting 400-odd provider rows every tick
                # because a poll arrived is how a background task turns into
                # a continuous disk write; the journal's own history records
                # that mistake costing three times the CPU it should have.
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._note_sources(now, {}, counts=None, touch_only=True)
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
                return self.revision

            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for table in _db.FAST_TABLES:
                    self._conn.execute(f"DELETE FROM {table}")
                for record in records:
                    self._write_record(record, now)
                self._set_meta(_DIGEST_KEY, digest)
                self._note_sources(
                    now,
                    {},
                    counts={
                        "deployments": sum(len(r.deployments) for r in records),
                        "providers": sum(len(r.providers) for r in records),
                        "catalog": sum(1 for r in records if "catalog" in r.facets),
                    },
                )
                revision = self._bump_revision()
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return revision

    def _write_record(self, record: ModelRecord, now: float) -> None:
        self._conn.execute(
            "INSERT INTO models(model_id, label, detail, default_context, "
            "default_concurrency, observed_at) VALUES(?,?,?,?,?,?)",
            (
                record.model_id,
                record.label,
                record.detail or "",
                record.default_context,
                record.default_concurrency,
                now,
            ),
        )
        self._conn.executemany(
            "INSERT OR IGNORE INTO model_facets(model_id, facet) VALUES(?,?)",
            [(record.model_id, f) for f in record.facets],
        )
        self._conn.executemany(
            "INSERT OR IGNORE INTO model_served_names(model_id, served_name) VALUES(?,?)",
            [(record.model_id, n) for n in record.served_names],
        )
        self._conn.executemany(
            "INSERT OR REPLACE INTO model_deployments(deployment_id, model_id, "
            "served_name, state, runtime, node_ids, last_error) VALUES(?,?,?,?,?,?,?)",
            [
                (
                    d.deployment_id,
                    record.model_id,
                    d.served_name,
                    d.state,
                    d.runtime,
                    json.dumps(d.node_ids),
                    d.last_error,
                )
                for d in record.deployments
            ],
        )
        self._conn.executemany(
            "INSERT OR REPLACE INTO model_provider_rows(model_id, provider_id, "
            "upstream_id, served_name, display_name, served, provider_enabled, "
            "context_length, modality, input_cost_per_mtok, output_cost_per_mtok, "
            "supports_tools, supports_streaming, healthy, last_error, admitting, "
            "admission_block) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    record.model_id,
                    p.provider_id,
                    p.upstream_id,
                    p.served_name,
                    p.display_name,
                    int(p.served),
                    _nullable_bool(p.provider_enabled),
                    p.context_length,
                    p.modality,
                    p.input_cost_per_mtok,
                    p.output_cost_per_mtok,
                    _nullable_bool(p.supports_tools),
                    _nullable_bool(p.supports_streaming),
                    _nullable_bool(p.healthy),
                    p.last_error,
                    _nullable_bool(p.admitting),
                    p.admission_block,
                )
                for p in record.providers
            ],
        )

    def refresh_cache(self, report: Mapping[str, Any] | None) -> int:
        """Take the weights-on-disk half from a ``GET /api/storage`` payload.

        Fed rather than fetched: ``/api/storage`` already fans out to every
        node agent, so this accepts the reading somebody else paid for
        instead of scheduling a second cluster-wide disk walk. The direction
        is one-way on purpose -- storage feeds the registry, never the
        reverse, or a stale registry would start answering the Storage tab.

        A node whose agent did not answer **keeps the rows it had**. Deleting
        them would turn one unreachable worker into a confident claim that
        nothing is downloaded anywhere, which is the same mistake as
        rendering an unreadable disk as 0 bytes free.
        """
        now = self._clock()
        nodes = list((report or {}).get("nodes") or [])
        measured_at = (report or {}).get("measured_at") or now
        scanned = 0
        digest = _cache_digest(nodes)

        with self._lock:
            if self._meta(_CACHE_DIGEST_KEY) == digest:
                # Nothing on any disk moved. Six screens poll `/api/storage` on
                # a 30s timer and every one of them feeds this, so without the
                # short-circuit the cache rows were deleted and reinserted
                # every two or three seconds -- and the revision counter, which
                # exists so a reader can tell a change from a re-poll, climbed
                # past 250 in five minutes and meant nothing.
                #
                # `attempted_at` still moves: we did ask, and a screen saying
                # "still asking, last answer 40 minutes ago" needs both halves.
                # It is three UPDATEs rather than a rewrite of every row, and
                # it deliberately does not bump the revision, because nothing a
                # reader renders has changed.
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._conn.execute(
                        "UPDATE cache_scans SET attempted_at=?", (now,)
                    )
                    self._conn.execute(
                        "UPDATE sources SET attempted_at=? WHERE source='cache'",
                        (now,),
                    )
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
                return self.revision

            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for node in nodes:
                    node_id = node.get("node_id")
                    if not node_id:
                        continue
                    models = node.get("models") or {}
                    available = bool(models.get("available"))
                    reason = models.get("reason")
                    if not available:
                        # model_cache is deliberately untouched for this node.
                        # observed_at is preserved -- COALESCE keeps whatever
                        # the last successful read stamped, or NULL if there
                        # never was one -- while attempted_at moves.
                        self._conn.execute(
                            "INSERT INTO cache_scans(node_id, available, reason, "
                            "observed_at, attempted_at) VALUES(?,0,?,NULL,?) "
                            "ON CONFLICT(node_id) DO UPDATE SET "
                            "available=0, reason=excluded.reason, "
                            "attempted_at=excluded.attempted_at",
                            (node_id, reason, now),
                        )
                        continue
                    self._conn.execute(
                        "DELETE FROM model_cache WHERE node_id=?", (node_id,)
                    )
                    rows = []
                    for repo in models.get("repos") or []:
                        repo_id = repo.get("repo_id")
                        if not repo_id:
                            continue
                        # A cache directory with no files in it is a resolve
                        # that touched the repo and wrote nothing. Counting it
                        # would say "already downloaded" about a model of
                        # which not one byte is present. Exact, never a size
                        # threshold.
                        if repo.get("blob_count") == 0:
                            continue
                        rows.append(
                            (
                                repo_id,
                                node_id,
                                repo.get("folder") or repo_id,
                                int(repo.get("bytes") or 0),
                                repo.get("blob_count"),
                            )
                        )
                    self._conn.executemany(
                        "INSERT OR REPLACE INTO model_cache(model_id, node_id, folder, "
                        "bytes, blob_count) VALUES(?,?,?,?,?)",
                        rows,
                    )
                    self._conn.execute(
                        "INSERT OR REPLACE INTO cache_scans(node_id, available, "
                        "reason, observed_at, attempted_at) VALUES(?,1,?,?,?)",
                        (node_id, reason, measured_at, now),
                    )
                    scanned += 1
                self._conn.execute(
                    "INSERT OR REPLACE INTO sources(source, ok, reason, observed_at, "
                    "attempted_at, rows) VALUES('cache',?,NULL,?,?,?)",
                    (int(scanned > 0), measured_at if scanned else None, now, scanned),
                )
                if scanned:
                    self._set_meta("cache_at", repr(measured_at))
                self._set_meta(_CACHE_DIGEST_KEY, digest)
                revision = self._bump_revision()
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return revision

    def drop_nodes(self, keep: set[str]) -> None:
        """Forget nodes that have left the roster.

        Called only with a roster the caller actually read. If
        ``list_nodes()`` raised, do nothing at all: a registry hiccup must
        never wipe the disk picture for the whole cluster.
        """
        with self._lock:
            rows = self._conn.execute("SELECT node_id FROM cache_scans").fetchall()
            gone = [r["node_id"] for r in rows if r["node_id"] not in keep]
            if not gone:
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for node_id in gone:
                    self._conn.execute(
                        "DELETE FROM model_cache WHERE node_id=?", (node_id,)
                    )
                    self._conn.execute(
                        "DELETE FROM cache_scans WHERE node_id=?", (node_id,)
                    )
                self._bump_revision()
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # -- reads -------------------------------------------------------------

    def list_models(self) -> list[ModelRecord]:
        """Every model, with the on-disk half joined on.

        The ``ondisk`` facet is computed here rather than stored, because it
        comes from the slow refresh while the fast one rewrites
        ``model_facets`` wholesale. A model that is *only* on disk -- somebody
        pulled it by hand -- has no row in ``models`` and gets one
        synthesised, which is how the browser behaved too.
        """
        with self._lock:
            records: dict[str, ModelRecord] = {}
            for row in self._conn.execute("SELECT * FROM models"):
                records[row["model_id"]] = ModelRecord(
                    model_id=row["model_id"],
                    label=row["label"],
                    detail=row["detail"] or "",
                    default_context=row["default_context"],
                    default_concurrency=row["default_concurrency"],
                    observed_at=row["observed_at"],
                )
            for row in self._conn.execute("SELECT * FROM model_facets"):
                rec = records.get(row["model_id"])
                if rec is not None:
                    rec.facets.append(row["facet"])
            for row in self._conn.execute("SELECT * FROM model_served_names"):
                rec = records.get(row["model_id"])
                if rec is not None:
                    rec.served_names.append(row["served_name"])
            for row in self._conn.execute("SELECT * FROM model_deployments"):
                rec = records.get(row["model_id"])
                if rec is None:
                    continue
                rec.deployments.append(
                    RowDeployment(
                        deployment_id=row["deployment_id"],
                        served_name=row["served_name"],
                        state=row["state"],
                        runtime=row["runtime"],
                        node_ids=_load_list(row["node_ids"]),
                        last_error=row["last_error"],
                    )
                )
            for row in self._conn.execute("SELECT * FROM model_provider_rows"):
                rec = records.get(row["model_id"])
                if rec is None:
                    continue
                rec.providers.append(
                    RowProvider(
                        provider_id=row["provider_id"],
                        display_name=row["display_name"],
                        served_name=row["served_name"],
                        upstream_id=row["upstream_id"],
                        served=bool(row["served"]),
                        provider_enabled=_to_bool(row["provider_enabled"]),
                        context_length=row["context_length"],
                        modality=row["modality"],
                        input_cost_per_mtok=row["input_cost_per_mtok"],
                        output_cost_per_mtok=row["output_cost_per_mtok"],
                        supports_tools=_to_bool(row["supports_tools"]),
                        supports_streaming=_to_bool(row["supports_streaming"]),
                        healthy=_to_bool(row["healthy"]),
                        last_error=row["last_error"],
                        admitting=_to_bool(row["admitting"]),
                        admission_block=row["admission_block"],
                    )
                )
            cache_at = self._meta_locked_float("cache_at")
            for row in self._conn.execute("SELECT * FROM model_cache"):
                model_id = row["model_id"]
                rec = records.get(model_id)
                if rec is None:
                    rec = ModelRecord(
                        model_id=model_id, label=model_id, observed_at=cache_at or 0.0
                    )
                    records[model_id] = rec
                rec.cached.append(
                    RowCache(
                        node_id=row["node_id"],
                        folder=row["folder"],
                        bytes=row["bytes"],
                        blob_count=row["blob_count"],
                    )
                )

        out = []
        for rec in records.values():
            if rec.cached:
                rec.facets.append("ondisk")
            seen = set(rec.facets)
            rec.facets = [f for f in FACET_ORDER if f in seen]
            rec.served_names = sorted(set(rec.served_names))
            out.append(rec)
        out.sort(key=lambda r: r.model_id)
        return out

    def cache_scans(self) -> list[CacheScan]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cache_scans ORDER BY node_id"
            ).fetchall()
        return [
            CacheScan(
                node_id=r["node_id"],
                available=bool(r["available"]),
                reason=r["reason"],
                observed_at=r["observed_at"],
                attempted_at=r["attempted_at"],
            )
            for r in rows
        ]

    @property
    def revision(self) -> int:
        with self._lock:
            return int(self._meta(_db.REVISION_KEY) or 0)

    def sources(self) -> dict[str, Any]:
        """Per-feed health, verbatim, plus which nodes could be read.

        Load-bearing rather than decorative. The Models tab's rule is that a
        feed failing greys nothing and empties nothing -- it prints one line
        naming the feed and the server's own sentence. Folding five fetches
        into one leaves the browser with no failed request to notice, so the
        sentence has to travel here or it stops existing.
        """
        with self._lock:
            rows = self._conn.execute("SELECT * FROM sources").fetchall()
        out: dict[str, Any] = {
            r["source"]: {
                "ok": bool(r["ok"]),
                "reason": r["reason"],
                "observed_at": r["observed_at"],
                "attempted_at": r["attempted_at"],
                "rows": r["rows"],
            }
            for r in rows
        }
        out.setdefault("cache", {"ok": False, "reason": None, "observed_at": None})
        out["cache"]["nodes"] = [
            {
                "node_id": s.node_id,
                "available": s.available,
                "reason": s.reason,
                "observed_at": s.observed_at,
                "attempted_at": s.attempted_at,
            }
            for s in self.cache_scans()
        ]
        return out

    # -- meta --------------------------------------------------------------

    def _note_sources(
        self,
        now: float,
        errors: Mapping[str, str],
        counts: Mapping[str, int] | None,
        *,
        touch_only: bool = False,
    ) -> None:
        for source in FAST_SOURCES:
            reason = errors.get(source)
            ok = reason is None
            if touch_only or (ok and counts is None):
                self._conn.execute(
                    "UPDATE sources SET attempted_at=? WHERE source=?", (now, source)
                )
                self._conn.execute(
                    "INSERT OR IGNORE INTO sources(source, ok, reason, observed_at, "
                    "attempted_at, rows) VALUES(?,1,NULL,?,?,NULL)",
                    (source, now, now),
                )
                continue
            if ok:
                self._conn.execute(
                    "INSERT OR REPLACE INTO sources(source, ok, reason, observed_at, "
                    "attempted_at, rows) VALUES(?,1,NULL,?,?,?)",
                    (source, now, now, (counts or {}).get(source)),
                )
            else:
                # observed_at is left where it was: the rows this feed owns
                # are still standing, and saying they were read just now
                # would be a lie about the only number that says how stale
                # they are.
                self._conn.execute(
                    "INSERT INTO sources(source, ok, reason, observed_at, "
                    "attempted_at, rows) VALUES(?,0,?,NULL,?,NULL) "
                    "ON CONFLICT(source) DO UPDATE SET "
                    "ok=0, reason=excluded.reason, attempted_at=excluded.attempted_at",
                    (source, reason, now),
                )

    def _meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row["v"] if row else None

    def _meta_locked_float(self, key: str) -> float | None:
        try:
            raw = self._meta(key)
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(k, v) VALUES(?,?)", (key, value)
        )

    def _bump_revision(self) -> int:
        current = int(self._meta(_db.REVISION_KEY) or 0) + 1
        self._set_meta(_db.REVISION_KEY, str(current))
        return current


_DIGEST_KEY = "fast_digest"
_CACHE_DIGEST_KEY = "cache_digest"


def _digest(records: Sequence[ModelRecord]) -> str:
    """A hash of exactly what the fast tables store.

    Over the stored content and nothing else. Anything that moves on its own
    -- spend, outstanding request counts, a clock -- must stay out of it, or
    the digest never settles and every tick rewrites several hundred provider
    rows forever.
    """
    h = hashlib.sha256()
    for record in sorted(records, key=lambda r: r.model_id):
        h.update(record.model_id.encode())
        h.update(record.label.encode())
        h.update((record.detail or "").encode())
        h.update(repr((record.default_context, record.default_concurrency)).encode())
        h.update(repr(record.facets).encode())
        h.update(repr(record.served_names).encode())
        for dep in sorted(record.deployments, key=lambda d: d.deployment_id):
            h.update(repr(astuple(dep)).encode())
        for prov in sorted(record.providers, key=lambda p: (p.provider_id, p.upstream_id)):
            h.update(repr(astuple(prov)).encode())
    return h.hexdigest()


def _cache_digest(nodes: Sequence[Mapping[str, Any]]) -> str:
    """A hash of what the cache tables store, and nothing that moves on its own.

    `measured_at` is deliberately excluded: it changes on every fan-out
    whether or not a single byte moved on any disk, and hashing it would make
    this short-circuit unreachable -- which is precisely the bug it exists to
    close. Six screens poll `/api/storage` on a 30s timer, so without this the
    registry rewrote its cache rows every two or three seconds forever and the
    revision counter became noise.
    """
    h = hashlib.sha256()
    for node in sorted(nodes, key=lambda n: str(n.get("node_id") or "")):
        models = node.get("models") or {}
        h.update(str(node.get("node_id")).encode())
        h.update(repr((bool(models.get("available")), models.get("reason"))).encode())
        repos = [
            (r.get("repo_id"), r.get("folder"), r.get("bytes"), r.get("blob_count"))
            for r in models.get("repos") or []
            if r.get("repo_id") and r.get("blob_count") != 0
        ]
        h.update(repr(sorted(repos, key=lambda r: str(r[0]))).encode())
    return h.hexdigest()


def _nullable_bool(value: bool | None) -> int | None:
    return None if value is None else int(bool(value))


def _to_bool(value: Any) -> bool | None:
    return None if value is None else bool(value)


def _load_list(raw: Any) -> list[str]:
    try:
        parsed = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x) for x in parsed] if isinstance(parsed, list) else []
