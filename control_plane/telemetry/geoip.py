"""Where a request came from: the stored ``client_ip`` placed on a map.

The address is already on every request row (``requests.client_ip``); this
module turns it into a place at READ time, from a local copy of DB-IP's free
City Lite database, and never writes the answer down. A location is a function
of the address AND the database build, so a stored one would be wrong after
the next monthly refresh with nothing to say so.

Three pieces:

* :class:`GeoDatabase` -- the ``.mmdb`` reader, reopened when the file on disk
  changes, with a small memo in front of it. A missing or unreadable file is a
  state (:meth:`GeoDatabase.status`), never an exception: the request map says
  why it has nothing to draw rather than answering 500.
* :class:`GeoUpdater` -- downloads the current month's file once, refreshes it
  monthly, backs off after a failure. Never runs when the operator pinned a
  file with ``DERATE_GEOIP_DB`` or switched downloads off with
  ``DERATE_GEOIP_DOWNLOAD=0``.
* :func:`summarize_origins` -- folds ``(ip, requests, last_ts)`` rows into one
  point per place, and counts what could not be placed and why.

DB-IP Lite is CC BY 4.0: anything that shows a location from it must show
:data:`ATTRIBUTION_TEXT` linking :data:`ATTRIBUTION_URL`.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import time
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Generic, TypeVar

import httpx

log = logging.getLogger(__name__)

SOURCE = "dbip-city-lite"
#: ``data_dir()/geoip/`` holds the downloaded file.
DB_SUBDIR = "geoip"
DB_FILENAME = "dbip-city-lite.mmdb"
#: ``month`` is ``YYYY-MM``. DB-IP publishes one file a month and keeps the
#: previous month's up while the new one is being posted.
URL_TEMPLATE = "https://download.db-ip.com/free/dbip-city-lite-{month}.mmdb.gz"
#: DB-IP publishes monthly; a file older than this is replaced.
REFRESH_AFTER_S = 35 * 86400
#: The updater loop's period.
CHECK_INTERVAL_S = 86400
#: After a failed download, nothing is retried for this long.
RETRY_AFTER_S = 3600
#: Decompressed size cap. The November 2026 file is ~121 MB.
MAX_DB_BYTES = 512 * 1024 * 1024
USER_AGENT = "derate/geoip-update"
#: CC BY 4.0 requires this on any screen that shows a location from the data.
ATTRIBUTION_TEXT = "IP Geolocation by DB-IP"
ATTRIBUTION_URL = "https://db-ip.com"

#: Lookups memoised per reader; cleared when full rather than evicted, because
#: a request map's working set is a few hundred addresses at most.
_CACHE_MAX = 4096


@dataclass(frozen=True)
class Location:
    lat: float
    lon: float
    city: str | None
    region: str | None
    #: ISO 3166-1 alpha-2.
    country: str | None
    #: The operator's name for a known place (``telemetry/places.py``).
    label: str | None = None
    #: ``"database"`` (the IP database), ``"place"`` (a known place),
    #: ``"coordinator"`` (this machine's own public address) or ``"relay"`` (a
    #: tailnet device's Tailscale relay region) -- see ``telemetry/tailnet.py``.
    source: str = "database"


def _address(ip: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The parsed address, with an IPv4-mapped IPv6 one unwrapped to IPv4."""
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def classify(ip: str | None) -> str:
    """``"public"``, ``"local"`` (parses but is not globally routable: loopback,
    RFC 1918, link-local, ULA, ...) or ``"invalid"`` (None or garbage)."""
    addr = _address(ip)
    if addr is None:
        return "invalid"
    return "public" if addr.is_global else "local"


def location_from_record(rec: Any) -> Location | None:
    """A DB-IP / GeoLite2 City record as a :class:`Location`; None without
    coordinates. Names are the English ones."""
    if not isinstance(rec, dict):
        return None
    loc = rec.get("location") or {}
    lat, lon = loc.get("latitude"), loc.get("longitude")
    if lat is None or lon is None:
        return None
    subdivisions = rec.get("subdivisions") or [{}]
    return Location(
        lat=float(lat),
        lon=float(lon),
        city=(rec.get("city") or {}).get("names", {}).get("en"),
        region=(subdivisions[0] or {}).get("names", {}).get("en"),
        country=(rec.get("country") or {}).get("iso_code"),
    )


T = TypeVar("T")


class MmdbFile(Generic[T]):
    """One ``.mmdb`` on disk, each record read through *parse*. Every method is
    called on the event loop. :class:`GeoDatabase` is the city file; the
    network-owner file (``telemetry/networks.py``) is the same reader."""

    def __init__(self, path: Path, parse: Callable[[Any], T | None], source: str) -> None:
        self.path = Path(path)
        self.source = source
        self._parse = parse
        self._reader: Any = None
        self._mtime: float | None = None
        self.build: str | None = None
        self.error: str | None = None
        self._cache: dict[str, T | None] = {}

    def _close(self) -> None:
        reader, self._reader = self._reader, None
        if reader is not None:
            try:
                reader.close()
            except Exception:
                pass

    def open(self) -> bool:
        """(Re)open the file. True when a reader is ready."""
        self._close()
        self._cache.clear()
        self.build = None
        self.error = None
        try:
            import maxminddb
        except ImportError as exc:  # the request map degrades; the gateway still starts
            self.error = f"maxminddb is not installed: {exc}"
            return False
        try:
            self._mtime = self.path.stat().st_mtime
            reader = maxminddb.open_database(str(self.path), maxminddb.MODE_AUTO)
        except FileNotFoundError:
            self._mtime = None
            return False
        except Exception as exc:  # InvalidDatabaseError, OSError, a truncated file
            self.error = str(exc) or type(exc).__name__
            return False
        self._reader = reader
        self.build = datetime.fromtimestamp(reader.metadata().build_epoch, UTC).date().isoformat()
        return True

    def reload_if_changed(self) -> None:
        """Reopen when the file appeared, disappeared or was replaced."""
        try:
            mtime: float | None = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self._mtime:
            self.open()

    def lookup(self, ip: str) -> T | None:
        """What the file says about *ip*, or None: no file, not a public
        address, or no record for it."""
        if self._reader is None:
            return None
        if ip in self._cache:
            return self._cache[ip]
        addr = _address(ip)
        found: T | None = None
        if addr is not None and addr.is_global:
            try:
                found = self._parse(self._reader.get(str(addr)))
            except ValueError:
                found = None
        if len(self._cache) >= _CACHE_MAX:
            self._cache.clear()
        self._cache[ip] = found
        return found

    def status(self) -> dict[str, Any]:
        if self._reader is not None:
            state = "ready"
        elif self.error is not None:
            state = "failed"
        else:
            state = "missing"
        return {
            "state": state,
            "source": self.source,
            "build": self.build,
            "path": str(self.path),
            "detail": self.error,
        }


class GeoDatabase(MmdbFile[Location]):
    """The IP-to-city file."""

    def __init__(self, path: Path) -> None:
        super().__init__(path, location_from_record, SOURCE)


class GeoDownloadError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


async def fetch_to(url: str, dest: Path) -> int:
    """Download the gzipped ``.mmdb`` at *url* into *dest*, decompressing on the
    way. Atomic: *dest* is replaced only by a complete file. Returns the bytes
    written."""
    part = dest.with_suffix(".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, read=120.0),
            follow_redirects=True,
            max_redirects=3,
            headers={"user-agent": USER_AGENT},
        ) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise GeoDownloadError(f"{url} answered {resp.status_code}", resp.status_code)
                inflate = zlib.decompressobj(16 + zlib.MAX_WBITS)
                with open(part, "wb") as out:
                    async for chunk in resp.aiter_raw():
                        data = inflate.decompress(chunk)
                        written += len(data)
                        if written > MAX_DB_BYTES:
                            raise GeoDownloadError(
                                f"{url} decompressed past {MAX_DB_BYTES} bytes; refusing it"
                            )
                        out.write(data)
                    tail = inflate.flush()
                    written += len(tail)
                    out.write(tail)
                if not inflate.eof:
                    raise GeoDownloadError(f"{url} ended before the end of its gzip stream")
        os.replace(part, dest)
        return written
    except zlib.error as exc:
        raise GeoDownloadError(f"{url} is not a gzip stream: {exc}") from exc
    finally:
        part.unlink(missing_ok=True)


def _months(now: float) -> tuple[str, str]:
    """This UTC month and the one before it, as ``YYYY-MM``."""
    today = datetime.fromtimestamp(now, UTC)
    previous = today.replace(day=1) - timedelta(days=1)
    return today.strftime("%Y-%m"), previous.strftime("%Y-%m")


class GeoUpdater:
    """Keeps one DB-IP ``.mmdb`` current -- the city file by default, the
    network-owner file with its own *url_template*. Owns one background task;
    ``start()``/``stop()`` around it."""

    def __init__(
        self, db: MmdbFile[Any], *, enabled: bool, pinned: bool, url_template: str = URL_TEMPLATE
    ) -> None:
        self.db = db
        self._url_template = url_template
        self._enabled = enabled
        #: The operator supplied the file (``DERATE_GEOIP_DB``): never download.
        self._pinned = pinned
        self.downloading = False
        self.last_error: str | None = None
        self.next_attempt_ts = 0.0
        self._running = False
        self._task: asyncio.Task | None = None
        self._job: asyncio.Task | None = None

    async def refresh_once(self) -> None:
        """Download when the file is missing, stale or unreadable. Never raises."""
        if self._pinned or self.downloading or time.time() < self.next_attempt_ts:
            return
        self.db.reload_if_changed()
        try:
            age = time.time() - self.db.path.stat().st_mtime
        except OSError:
            age = None
        if age is not None and age < REFRESH_AFTER_S and self.db.status()["state"] == "ready":
            return
        self.downloading = True
        try:
            for month in _months(time.time()):
                url = self._url_template.format(month=month)
                try:
                    size = await fetch_to(url, self.db.path)
                except GeoDownloadError as exc:
                    if exc.status == 404:
                        log.info("geoip: %s is not published yet", url)
                        continue
                    raise
                log.info("geoip: downloaded %s (%d bytes) to %s", url, size, self.db.path)
                break
            else:
                raise GeoDownloadError(f"neither of the last two months is published at {url}", 404)
            self.db.open()
            if self.db.status()["state"] != "ready":
                raise GeoDownloadError(f"the downloaded file could not be read: {self.db.error}")
            self.last_error = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = str(exc) or type(exc).__name__
            self.next_attempt_ts = time.time() + RETRY_AFTER_S
            log.warning("geoip: download failed, retrying in %ds: %s", RETRY_AFTER_S, exc)
        finally:
            self.downloading = False

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # a bad round must not kill the loop
                log.exception("geoip refresh round failed")
            await asyncio.sleep(CHECK_INTERVAL_S)

    async def start(self) -> None:
        """Idempotent. A no-op when downloads are off or the file is pinned."""
        if not self._enabled or self._pinned or self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name=f"telemetry-{self.db.source}")

    async def stop(self) -> None:
        self._running = False
        for task in (self._task, self._job):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._task = self._job = None

    def ensure(self) -> None:
        """Start a download now if there is no usable database and none is
        running or backing off -- so opening the map after a failure does not
        wait for the next daily round."""
        if (
            self._enabled
            and not self._pinned
            and not self.downloading
            and (self._job is None or self._job.done())
            and self.db.status()["state"] != "ready"
            and time.time() >= self.next_attempt_ts
        ):
            self._job = asyncio.create_task(self.refresh_once(), name=f"telemetry-{self.db.source}-now")

    def status(self) -> dict[str, Any]:
        out = self.db.status()
        out["downloading"] = self.downloading
        out["last_error"] = self.last_error
        out["retry_in_s"] = max(0.0, self.next_attempt_ts - time.time())
        if self.downloading:
            out["state"] = "downloading"
        if self._pinned:
            out["pinned"] = True
        return out


def summarize_origins(
    rows: Iterable[dict[str, Any]], lookup: Callable[[str], Location | None]
) -> dict[str, Any]:
    """One point per place from ``{ip, requests, last_ts}`` rows.

    Points group addresses that resolve to the same coordinates and are sorted
    by requests, most first. Every request is counted exactly once: in
    ``located``, ``local`` (a non-global address no known place covers),
    ``unknown`` (no address recorded) or ``unlocated`` (public, with no record
    in the database). *lookup* runs for every parseable address, local ones
    included, so a known place can put the local network on the map.
    """
    points: dict[tuple[float, float], dict[str, Any]] = {}
    counts = {"located": 0, "local": 0, "unlocated": 0, "unknown": 0}
    for row in rows:
        ip = row.get("ip")
        n = int(row.get("requests") or 0)
        kind = classify(ip)
        if kind == "invalid":
            counts["unknown"] += n
            continue
        loc = lookup(ip)
        if loc is None:
            counts["local" if kind == "local" else "unlocated"] += n
            continue
        counts["located"] += n
        last_ts = row.get("last_ts")
        point = points.get((loc.lat, loc.lon))
        if point is None:
            points[(loc.lat, loc.lon)] = {
                "lat": loc.lat,
                "lon": loc.lon,
                "city": loc.city,
                "region": loc.region,
                "country": loc.country,
                "label": loc.label,
                "source": loc.source,
                "requests": n,
                "ips": 1,
                "last_ts": last_ts,
            }
            continue
        point["requests"] += n
        point["ips"] += 1
        if last_ts is not None and (point["last_ts"] is None or last_ts > point["last_ts"]):
            point["last_ts"] = last_ts
    ordered = sorted(points.values(), key=lambda p: (-p["requests"], -(p["last_ts"] or 0)))
    return {"points": ordered, **counts}
