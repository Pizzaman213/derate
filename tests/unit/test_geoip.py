"""telemetry/geoip.py: classifying an address, reading a City record, folding
rows into places, and the updater's download / fallback / backoff rules."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import maxminddb
import pytest

from control_plane import paths
from control_plane.telemetry import geoip
from control_plane.telemetry.geoip import GeoDatabase, GeoDownloadError, GeoUpdater, Location


@pytest.fixture(autouse=True)
def _own_data_dir(tmp_path, monkeypatch):
    # Never the operator's: conftest keeps an exported DERATE_DATA_DIR.
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path / "data"))


RECORD = {
    "city": {"names": {"en": "Mountain View"}},
    "subdivisions": [{"names": {"en": "California"}}],
    "country": {"iso_code": "US"},
    "location": {"latitude": 37.4, "longitude": -122.1},
}
BUILD_EPOCH = 1_793_491_200  # 2026-11-01T00:00:00Z


class FakeReader:
    """Stands in for maxminddb's reader: one record for every public address."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.closed = False

    def metadata(self):
        return SimpleNamespace(build_epoch=BUILD_EPOCH)

    def get(self, ip: str):
        self.asked.append(ip)
        return RECORD

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_reader(monkeypatch):
    readers: list[FakeReader] = []

    def open_database(path, mode):
        with open(path, "rb"):  # FileNotFoundError when absent, as the real one
            pass
        readers.append(FakeReader())
        return readers[-1]

    monkeypatch.setattr(maxminddb, "open_database", open_database)
    return readers


def _months_now() -> tuple[str, str]:
    now = datetime.now(UTC)
    return now.strftime("%Y-%m"), (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


def _db_path():
    return paths.data_dir() / geoip.DB_SUBDIR / geoip.DB_FILENAME


# -- classify / record ---------------------------------------------------------


@pytest.mark.parametrize(
    ("ip", "kind"),
    [
        ("8.8.8.8", "public"),
        ("2001:4860:4860::8888", "public"),
        ("::ffff:8.8.8.8", "public"),
        ("10.0.0.1", "local"),
        ("192.168.0.69", "local"),
        ("127.0.0.1", "local"),
        ("::1", "local"),
        ("fd00::1", "local"),
        ("169.254.1.1", "local"),
        ("::ffff:192.168.0.1", "local"),
        (None, "invalid"),
        ("", "invalid"),
        ("nope", "invalid"),
    ],
)
def test_classify(ip, kind):
    assert geoip.classify(ip) == kind


def test_a_full_record_names_city_region_and_country():
    assert geoip.location_from_record(RECORD) == Location(
        lat=37.4, lon=-122.1, city="Mountain View", region="California", country="US"
    )


@pytest.mark.parametrize("subdivisions", [None, []])
def test_a_record_without_subdivisions_has_no_region(subdivisions):
    rec = {**RECORD, "subdivisions": subdivisions}
    if subdivisions is None:
        del rec["subdivisions"]
    loc = geoip.location_from_record(rec)
    assert loc is not None and loc.region is None and loc.city == "Mountain View"


def test_a_record_without_coordinates_is_no_location():
    assert geoip.location_from_record({k: v for k, v in RECORD.items() if k != "location"}) is None
    assert geoip.location_from_record(None) is None


# -- summarize_origins ---------------------------------------------------------


def test_addresses_at_one_place_merge_and_every_request_is_counted_once():
    # Real routable addresses: the documentation ranges (192.0.2/24,
    # 198.51.100/24, 203.0.113/24) are not global and classify as local.
    here = Location(1.0, 2.0, "A", None, "AA")
    there = Location(3.0, 4.0, "B", "R", "BB")
    # A known place answers for a local address; the database never does.
    lab = Location(45.5, -73.6, None, None, None, label="Lab", source="place")
    places = {"8.8.8.8": here, "8.8.4.4": here, "1.1.1.1": there, "127.0.0.1": lab}
    rows = [
        {"ip": "8.8.8.8", "requests": 3, "last_ts": 10.0},
        {"ip": "1.1.1.1", "requests": 5, "last_ts": 30.0},
        {"ip": "8.8.4.4", "requests": 4, "last_ts": 20.0},
        {"ip": "127.0.0.1", "requests": 2, "last_ts": 5.0},
        {"ip": "203.0.113.5", "requests": 1, "last_ts": 5.0},
        {"ip": None, "requests": 6, "last_ts": 5.0},
        {"ip": "garbage", "requests": 1, "last_ts": 5.0},
        {"ip": "9.9.9.9", "requests": 7, "last_ts": 5.0},  # public, no record
    ]
    out = geoip.summarize_origins(rows, lambda ip: places.get(ip))
    assert [
        (p["label"] or p["city"], p["requests"], p["ips"], p["last_ts"]) for p in out["points"]
    ] == [
        ("A", 7, 2, 20.0),
        ("B", 5, 1, 30.0),
        ("Lab", 2, 1, 5.0),
    ]
    assert (out["points"][2]["label"], out["points"][2]["source"]) == ("Lab", "place")
    assert out["points"][0]["source"] == "database"
    assert (out["located"], out["local"], out["unknown"], out["unlocated"]) == (14, 1, 7, 7)


# -- GeoDatabase ---------------------------------------------------------------


def test_an_absent_file_is_missing_and_a_garbage_one_failed(tmp_path):
    db = GeoDatabase(tmp_path / "none.mmdb")
    db.open()
    assert db.status()["state"] == "missing" and db.status()["detail"] is None
    assert db.lookup("8.8.8.8") is None

    bad = tmp_path / "bad.mmdb"
    bad.write_bytes(b"this is not a maxmind database" * 10)
    db = GeoDatabase(bad)
    db.open()
    status = db.status()
    assert status["state"] == "failed" and status["detail"]


def test_lookup_asks_the_reader_only_for_public_addresses(tmp_path, fake_reader):
    path = tmp_path / "db.mmdb"
    path.write_bytes(b"x")
    db = GeoDatabase(path)
    assert db.open() is True
    assert db.status()["build"] == "2026-11-01"
    assert db.lookup("127.0.0.1") is None and db.lookup("10.0.0.1") is None
    mapped = db.lookup("::ffff:8.8.8.8")
    assert mapped is not None and mapped.country == "US"
    plain = db.lookup("8.8.8.8")
    assert plain is not None and plain.city == "Mountain View"
    db.lookup("8.8.8.8")
    # The mapped form reaches the reader unwrapped; the repeat is memoised.
    assert fake_reader[0].asked == ["8.8.8.8", "8.8.8.8"]


def test_a_replaced_file_is_reopened(tmp_path, fake_reader):
    path = tmp_path / "db.mmdb"
    path.write_bytes(b"x")
    db = GeoDatabase(path)
    db.open()
    db.reload_if_changed()
    assert len(fake_reader) == 1
    stat = path.stat()
    path.write_bytes(b"y")
    os.utime(path, (stat.st_atime, stat.st_mtime + 10))
    db.reload_if_changed()
    assert len(fake_reader) == 2 and fake_reader[0].closed


# -- GeoUpdater ----------------------------------------------------------------


def _fake_fetch(monkeypatch, answers):
    """``answers`` maps a month to an exception or None (= success)."""
    urls: list[str] = []

    async def fetch_to(url, dest):
        urls.append(url)
        for month, outcome in answers.items():
            if month in url and outcome is not None:
                raise outcome
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"mmdb")
        return 4

    monkeypatch.setattr(geoip, "fetch_to", fetch_to)
    return urls


def test_a_missing_database_is_downloaded_into_the_data_dir(monkeypatch, fake_reader):
    urls = _fake_fetch(monkeypatch, {})
    db = GeoDatabase(_db_path())
    updater = GeoUpdater(db, enabled=True, pinned=False)
    asyncio.run(updater.refresh_once())
    current, _ = _months_now()
    assert urls == [geoip.URL_TEMPLATE.format(month=current)]
    assert _db_path().exists()
    status = updater.status()
    assert status["state"] == "ready" and status["last_error"] is None
    # A young, readable file is not fetched again on the next round.
    asyncio.run(updater.refresh_once())
    assert len(urls) == 1


def test_the_previous_month_is_tried_when_this_one_is_not_up(monkeypatch, fake_reader):
    current, previous = _months_now()
    urls = _fake_fetch(monkeypatch, {current: GeoDownloadError("404", 404)})
    updater = GeoUpdater(GeoDatabase(_db_path()), enabled=True, pinned=False)
    asyncio.run(updater.refresh_once())
    assert urls == [geoip.URL_TEMPLATE.format(month=m) for m in (current, previous)]
    assert updater.status()["state"] == "ready"


def test_a_failed_download_backs_off(monkeypatch, fake_reader):
    current, _ = _months_now()
    urls = _fake_fetch(monkeypatch, {current: GeoDownloadError("answered 503", 503)})
    updater = GeoUpdater(GeoDatabase(_db_path()), enabled=True, pinned=False)
    asyncio.run(updater.refresh_once())
    assert len(urls) == 1  # a 503 is not "not published yet"
    status = updater.status()
    assert status["state"] == "missing" and "503" in status["last_error"]
    assert updater.next_attempt_ts > time.time() + geoip.RETRY_AFTER_S - 60
    asyncio.run(updater.refresh_once())
    assert len(urls) == 1


def test_a_downloaded_file_that_cannot_be_read_is_an_error_not_ready(monkeypatch):
    _fake_fetch(monkeypatch, {})
    updater = GeoUpdater(GeoDatabase(_db_path()), enabled=True, pinned=False)
    asyncio.run(updater.refresh_once())  # the real reader, on b"mmdb"
    status = updater.status()
    assert status["state"] == "failed" and status["last_error"]
    assert updater.next_attempt_ts > time.time()


def test_a_pinned_file_is_never_downloaded(monkeypatch, tmp_path):
    urls = _fake_fetch(monkeypatch, {})
    updater = GeoUpdater(GeoDatabase(tmp_path / "mine.mmdb"), enabled=True, pinned=True)
    asyncio.run(updater.refresh_once())
    assert urls == [] and updater.status()["pinned"] is True


def test_disabled_downloads_start_no_task(tmp_path):
    updater = GeoUpdater(GeoDatabase(tmp_path / "x.mmdb"), enabled=False, pinned=False)

    async def go():
        await updater.start()
        updater.ensure()
        return updater._task, updater._job

    assert asyncio.run(go()) == (None, None)
