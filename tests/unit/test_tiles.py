"""gateway/tiles.py and GET /api/map/tiles/{z}/{x}/{y}: OSM is asked once per
tile, the answer is kept, and nothing outside zoom 1-15 is ever fetched."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from control_plane import paths
from control_plane.gateway import create_app, tiles
from tests.unit.test_gateway import build_deps

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    # Never the operator's data dir, and no lock or semaphore bound to another
    # test's event loop.
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(tiles, "_semaphore", None)
    monkeypatch.setattr(tiles, "_locks", {})


@pytest.fixture
def client():
    with TestClient(create_app(build_deps())) as c:
        yield c


def _fake_fetch(monkeypatch, outcome):
    calls: list[tuple[int, int, int]] = []

    async def fetch(z, x, y):
        calls.append((z, x, y))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(tiles, "fetch", fetch)
    return calls


@pytest.mark.parametrize(
    ("z", "x", "y", "ok"),
    [
        (0, 0, 0, False),
        (1, 0, 0, True),
        (1, 1, 1, True),
        (1, 2, 0, False),
        (15, 32767, 32767, True),
        (15, 32768, 0, False),
        (16, 0, 0, False),
        (3, -1, 0, False),
    ],
)
def test_valid_is_zoom_one_to_fifteen_inside_the_grid(z, x, y, ok):
    assert tiles.valid(z, x, y) is ok


def test_a_tile_is_fetched_once_then_served_from_disk(client, monkeypatch):
    calls = _fake_fetch(monkeypatch, PNG)
    first = client.get("/api/map/tiles/5/3/7")
    assert first.status_code == 200, first.text
    assert first.headers["content-type"] == "image/png" and first.content == PNG
    assert (paths.data_dir() / "tiles" / "5" / "3" / "7.png").read_bytes() == PNG
    second = client.get("/api/map/tiles/5/3/7")
    assert second.status_code == 200 and second.content == PNG
    assert calls == [(5, 3, 7)]


def test_a_tile_osm_does_not_have_is_404(client, monkeypatch):
    _fake_fetch(monkeypatch, None)
    r = client.get("/api/map/tiles/4/1/1")
    assert r.status_code == 404 and r.json()["error"]["code"] == "tile_not_found"


def test_an_upstream_failure_is_502_and_caches_nothing(client, monkeypatch):
    _fake_fetch(monkeypatch, RuntimeError("tile.openstreetmap.org answered 429"))
    r = client.get("/api/map/tiles/4/1/1")
    assert r.status_code == 502 and r.json()["error"]["code"] == "tile_upstream_failed"
    assert "429" in r.json()["error"]["message"]
    assert not (paths.data_dir() / "tiles" / "4" / "1" / "1.png").exists()


def test_out_of_range_is_refused_without_fetching(client, monkeypatch):
    calls = _fake_fetch(monkeypatch, PNG)
    r = client.get("/api/map/tiles/16/0/0")
    assert r.status_code == 404 and r.json()["error"]["code"] == "tile_out_of_range"
    assert calls == []
