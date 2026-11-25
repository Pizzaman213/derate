"""telemetry/places.py: the operator's CIDR -> place table -- what it refuses
and how it says so, the file it keeps, and which place answers an address."""

from __future__ import annotations

import os

import pytest

from control_plane.telemetry.places import MAX_PLACES, KnownPlaces, Place, PlaceError, parse_places


def _row(**over):
    return {"cidr": "10.0.0.0/8", "label": "Office", "lat": 45.5, "lon": -73.6, **over}


def test_a_bare_address_becomes_a_single_host_range():
    parsed = parse_places([_row(cidr=" 10.1.2.3 "), _row(cidr="::1")])
    assert [p.cidr for p in parsed] == ["10.1.2.3/32", "::1/128"]
    # Host bits are dropped rather than refused.
    assert parse_places([_row(cidr="10.1.2.3/8")])[0].cidr == "10.0.0.0/8"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"cidr": 1}, "places[1].cidr: an address or CIDR range is required"),
        ({"cidr": "nope"}, "places[1].cidr: 'nope' is not an address or CIDR range"),
        ({"cidr": "10.0.0.0/33"}, "places[1].cidr: '10.0.0.0/33' is not an address or CIDR range"),
        ({"label": "  "}, "places[1].label: label is required"),
        ({"label": None}, "places[1].label: label is required"),
        ({"label": 7}, "places[1].label: label must be a string"),
        ({"label": "x" * 65}, "places[1].label: label is longer than 64 characters"),
        ({"lat": True}, "places[1].lat: latitude must be a number"),
        ({"lat": "45"}, "places[1].lat: latitude must be a number"),
        ({"lat": 90.5}, "places[1].lat: latitude must be between -90 and 90"),
        ({"lat": float("nan")}, "places[1].lat: latitude must be between -90 and 90"),
        ({"lon": -180.1}, "places[1].lon: longitude must be between -180 and 180"),
        ({"lon": float("inf")}, "places[1].lon: longitude must be between -180 and 180"),
    ],
)
def test_a_bad_row_is_refused_naming_its_index_and_field(raw, message):
    with pytest.raises(PlaceError) as err:
        parse_places([_row(cidr="192.168.0.0/24"), _row(**raw)])
    assert str(err.value) == message


def test_the_edges_of_the_globe_and_a_64_character_label_are_accepted():
    parsed = parse_places([_row(lat=-90, lon=180, label="x" * 64)])
    assert parsed == [Place(cidr="10.0.0.0/8", label="x" * 64, lat=-90.0, lon=180.0)]


def test_two_spellings_of_one_range_are_a_duplicate():
    with pytest.raises(PlaceError) as err:
        parse_places([_row(cidr="10.0.0.0/8"), _row(cidr="10.9.9.9/8")])
    assert str(err.value) == "places[1].cidr: '10.0.0.0/8' is listed twice"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"cidr": "10.0.0.0/8"}, "places must be a list"),
        (None, "places must be a list"),
        (["10.0.0.0/8"], "places[0]: each place must be an object"),
        ([_row(cidr=f"10.0.{i // 256}.{i % 256}") for i in range(MAX_PLACES + 1)],
         "places: at most 256 entries"),
    ],
)
def test_the_table_itself_must_be_a_short_list_of_objects(raw, message):
    with pytest.raises(PlaceError) as err:
        parse_places(raw)
    assert str(err.value) == message


def test_a_missing_file_is_an_empty_table_and_a_broken_one_says_why(tmp_path):
    missing = KnownPlaces(tmp_path / "places.json")
    missing.load()
    assert (missing.places, missing.error) == ([], None)

    path = tmp_path / "broken.json"
    path.write_bytes(b"{")
    broken = KnownPlaces(path)
    broken.load()
    assert broken.places == [] and broken.error
    assert broken.match("10.1.2.3") is None

    path.write_text('{"version": 1, "places": [{"cidr": "nope", "label": "x", "lat": 0, "lon": 0}]}')
    invalid = KnownPlaces(path)
    invalid.load()
    assert invalid.places == [] and invalid.error.startswith("places[0].cidr:")


def test_save_round_trips_through_a_private_file(tmp_path):
    path = tmp_path / "geoip" / "places.json"
    table = parse_places([_row(), _row(cidr="2001:db8::/32", label="VPN", lat=-33.9, lon=151.2)])
    KnownPlaces(path).save(table)
    assert os.stat(path).st_mode & 0o777 == 0o600
    again = KnownPlaces(path)
    again.load()
    assert (again.places, again.error) == (table, None)
    assert again.as_dicts()[1] == {"cidr": "2001:db8::/32", "label": "VPN", "lat": -33.9, "lon": 151.2}
    # Nothing but the table is left behind in the folder.
    assert [p.name for p in path.parent.iterdir()] == ["places.json"]


def test_the_longest_matching_prefix_answers(tmp_path):
    known = KnownPlaces(tmp_path / "places.json")
    known.save(parse_places([
        _row(cidr="10.0.0.0/8", label="Wide", lat=1.0, lon=2.0),
        _row(cidr="10.1.0.0/16", label="Narrow", lat=3.0, lon=4.0),
        _row(cidr="::/0", label="Any v6", lat=5.0, lon=6.0),
    ]))
    assert known.match("10.1.2.3").label == "Narrow"
    assert known.match("10.2.0.0").label == "Wide"
    assert known.match("11.0.0.0") is None
    # IPv4-mapped IPv6 is the IPv4 address, not a match for the v6 range.
    assert known.match("::ffff:10.1.2.3").label == "Narrow"
    assert known.match("2001:db8::1").label == "Any v6"
    assert known.match("garbage") is None
    loc = known.match("10.1.2.3")
    assert (loc.lat, loc.lon, loc.source, loc.city, loc.country) == (3.0, 4.0, "place", None, None)
