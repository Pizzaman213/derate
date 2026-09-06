"""Durable storage for link measurements.

Two properties matter here. Measurements survive a restart, because re-measuring
is disruptive and takes tens of seconds, so losing them on a coordinator bounce
would mean planning blind at exactly the wrong moment. And a measurement in
progress never blocks a read: writes swap a whole new mapping into place, so
readers hold a consistent snapshot without taking a lock at all.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading

from .record import AnnotatedLink, from_json, pair_key, to_json

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class LinkStore:
    """A JSON-backed map from unordered node pair to measurement."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = os.fspath(path)
        self._write_lock = threading.Lock()
        # Readers touch this reference and nothing else. Writers build a new
        # dict and rebind, so a reader can never observe a half-applied update.
        self._links: dict[tuple[str, str], AnnotatedLink] = {}
        self._load()

    @property
    def path(self) -> str:
        return self._path

    def get(self, a: str, b: str) -> AnnotatedLink | None:
        return self._links.get(pair_key(a, b))

    def all(self) -> list[AnnotatedLink]:
        return list(self._links.values())

    def put(self, link: AnnotatedLink) -> None:
        with self._write_lock:
            updated = dict(self._links)
            updated[pair_key(link.src, link.dst)] = link
            self._links = updated
            self._flush(updated)

    def delete(self, a: str, b: str) -> bool:
        with self._write_lock:
            key = pair_key(a, b)
            if key not in self._links:
                return False
            updated = dict(self._links)
            del updated[key]
            self._links = updated
            self._flush(updated)
            return True

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            # A corrupt store is not worth a crash on startup. Everything in it
            # is re-derivable by measuring again, and the UI will show the pairs
            # as unmeasured, which is the honest state.
            log.warning("link store at %s is unreadable (%s); starting empty", self._path, exc)
            return

        loaded: dict[tuple[str, str], AnnotatedLink] = {}
        for entry in doc.get("links", []):
            try:
                link = from_json(entry)
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("skipping malformed link record %r: %s", entry, exc)
                continue
            loaded[pair_key(link.src, link.dst)] = link
        self._links = loaded

    def _flush(self, links: dict[tuple[str, str], AnnotatedLink]) -> None:
        doc = {
            "version": SCHEMA_VERSION,
            # A list rather than an object: node ids are opaque strings and we
            # are not in the business of escaping them into JSON keys.
            "links": [to_json(link) for link in links.values()],
        }
        directory = os.path.dirname(os.path.abspath(self._path))
        try:
            os.makedirs(directory, exist_ok=True)
            # Write beside the target and rename, so a crash mid-write leaves
            # the previous good file rather than a truncated one.
            fd, tmp = tempfile.mkstemp(prefix=".links-", suffix=".json", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(doc, fh, indent=2, sort_keys=True)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self._path)
            except BaseException:
                _unlink_quietly(tmp)
                raise
        except OSError as exc:
            # In-memory state stands. Losing durability is bad; losing the
            # measurement we just spent thirty seconds taking is worse.
            log.error("could not persist link store to %s: %s", self._path, exc)


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
