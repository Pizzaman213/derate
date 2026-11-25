"""Places the operator knows: CIDR ranges pinned to coordinates.

IP geolocation stops at the city. DB-IP Lite places an address at its city's
point with no per-record accuracy, and no free database goes below a few
kilometres -- so the only way to put a request within a mile of where it came
from is for somebody who knows to say so. The operator names their own
networks, an office's egress address, a VPN, as ``CIDR -> (lat, lon, label)``;
a match here beats the database, and it is the only way the local network
(loopback, RFC 1918) appears on the request map at all.

Stored as ``data_dir()/geoip/places.json``, beside the database, written
atomically and 0600 the way ``gateway/settings_store.py`` writes. A file that
cannot be read is a state (:attr:`KnownPlaces.error`), never an exception:
the gateway starts and the map shows what the database knows.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from control_plane import fsutil
from control_plane.telemetry.geoip import Location, _address

log = logging.getLogger(__name__)

#: ``data_dir()/geoip/places.json``, beside the database.
PLACES_FILENAME = "places.json"
MAX_PLACES = 256
MAX_LABEL = 64
SCHEMA_VERSION = 1

_Network = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True)
class Place:
    #: Normalised: ``str(ipaddress.ip_network(value, strict=False))``.
    cidr: str
    label: str
    lat: float
    lon: float


class PlaceError(ValueError):
    pass


def _coordinate(i: int, field: str, value: Any, name: str, bound: int) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PlaceError(f"places[{i}].{field}: {name} must be a number")
    v = float(value)
    if not (math.isfinite(v) and -bound <= v <= bound):
        raise PlaceError(f"places[{i}].{field}: {name} must be between -{bound} and {bound}")
    return v


def parse_places(raw: Any) -> list[Place]:
    """The validated table, in input order. Raises :class:`PlaceError` naming
    the row and field: ``places[{i}].{field}: {why}``."""
    if not isinstance(raw, list):
        raise PlaceError("places must be a list")
    if len(raw) > MAX_PLACES:
        raise PlaceError(f"places: at most {MAX_PLACES} entries")
    out: list[Place] = []
    seen: set[str] = set()
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            raise PlaceError(f"places[{i}]: each place must be an object")
        value = row.get("cidr")
        if not isinstance(value, str):
            raise PlaceError(f"places[{i}].cidr: an address or CIDR range is required")
        try:
            cidr = str(ipaddress.ip_network(value.strip(), strict=False))
        except ValueError:
            raise PlaceError(
                f"places[{i}].cidr: '{value}' is not an address or CIDR range"
            ) from None
        label = row.get("label")
        if label is not None and not isinstance(label, str):
            raise PlaceError(f"places[{i}].label: label must be a string")
        label = (label or "").strip()
        if not label:
            raise PlaceError(f"places[{i}].label: label is required")
        if len(label) > MAX_LABEL:
            raise PlaceError(
                f"places[{i}].label: label is longer than {MAX_LABEL} characters"
            )
        lat = _coordinate(i, "lat", row.get("lat"), "latitude", 90)
        lon = _coordinate(i, "lon", row.get("lon"), "longitude", 180)
        if cidr in seen:
            raise PlaceError(f"places[{i}].cidr: '{cidr}' is listed twice")
        seen.add(cidr)
        out.append(Place(cidr=cidr, label=label, lat=lat, lon=lon))
    return out


class KnownPlaces:
    """The operator's table, loaded once and replaced whole on save."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.places: list[Place] = []
        self.error: str | None = None
        #: ``places``' networks, parsed once per load/save, never per lookup.
        self._nets: list[_Network] = []

    def _set(self, places: list[Place]) -> None:
        self.places = list(places)
        self._nets = [ipaddress.ip_network(p.cidr, strict=False) for p in self.places]

    def load(self) -> None:
        """Read the file. A missing one is an empty table; an unusable one is an
        empty table and :attr:`error`. Never raises."""
        self.error = None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise PlaceError("the file is not a JSON object")
            if raw.get("version") != SCHEMA_VERSION:
                raise PlaceError(f"schema {raw.get('version')!r} is not {SCHEMA_VERSION}")
            self._set(parse_places(raw.get("places")))
        except FileNotFoundError:
            self._set([])
        except (OSError, ValueError) as exc:  # JSONDecodeError and PlaceError are ValueErrors
            self._set([])
            self.error = str(exc) or type(exc).__name__
            log.warning("geoip places: %s is unusable: %s", self.path, exc)

    def save(self, places: list[Place]) -> None:
        """Replace the table, atomically, 0600. Raises OSError when the file
        cannot be written; the table in memory is then unchanged."""
        text = json.dumps(
            {"version": SCHEMA_VERSION, "places": [_as_dict(p) for p in places]}, indent=2
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".places-")
        try:
            fsutil.harden_fd(fd, self.path)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self._set(places)
        self.error = None

    def match(self, ip: str) -> Location | None:
        """The known place covering *ip*: the longest prefix wins, the first in
        file order on a tie. None when no place covers it."""
        addr = _address(ip)
        if addr is None:
            return None
        best: Place | None = None
        best_len = -1
        for place, net in zip(self.places, self._nets, strict=True):
            if net.version == addr.version and addr in net and net.prefixlen > best_len:
                best, best_len = place, net.prefixlen
        if best is None:
            return None
        return Location(
            lat=best.lat,
            lon=best.lon,
            city=None,
            region=None,
            country=None,
            label=best.label,
            source="place",
        )

    def as_dicts(self) -> list[dict[str, Any]]:
        """``[{cidr, label, lat, lon}]``, in file order."""
        return [_as_dict(p) for p in self.places]


def _as_dict(place: Place) -> dict[str, Any]:
    return {"cidr": place.cidr, "label": place.label, "lat": place.lat, "lon": place.lon}
