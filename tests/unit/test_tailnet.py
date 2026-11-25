"""telemetry/tailnet.py: a LAN or tailnet address placed through what this
machine's tailscaled knows -- and left unplaced when it knows nothing."""

from __future__ import annotations

import asyncio
import json
import stat

from control_plane.telemetry.geoip import Location
from control_plane.telemetry.tailnet import Tailnet, locate_local, parse_status

LAKE_MARY = Location(28.76, -81.32, "Lake Mary", "Florida", "US")
LISBON = Location(38.72, -9.14, "Lisbon", "Lisbon", "PT")
DATABASE = {"69.109.237.253": LAKE_MARY, "85.240.1.2": LISBON}

STATUS = {
    "Self": {
        "TailscaleIPs": ["100.108.31.115", "fd7a:115c:a1e0::ba33:1f74"],
        "Addrs": ["10.100.0.2:41641", "69.109.237.253:41641", "192.168.0.172:41641"],
    },
    "Peer": {
        "nodekey:a": {"HostName": "MacBook", "TailscaleIPs": ["100.67.68.35", "fd7a:115c:a1e0::4e33:4424"],
                      "CurAddr": "", "Relay": "mia"},
        "nodekey:b": {"HostName": "agx", "TailscaleIPs": ["100.90.181.55"], "CurAddr": "192.168.0.41:41641", "Relay": "mia"},
        "nodekey:c": {"HostName": "travel", "TailscaleIPs": ["100.70.0.9"], "CurAddr": "[::ffff:85.240.1.2]:41641", "Relay": "mad"},
        "nodekey:d": {"HostName": "odd", "TailscaleIPs": ["100.70.0.10"], "CurAddr": "", "Relay": "xyz"},
    },
}


def _at(ip):
    return locate_local(ip, parse_status(STATUS), DATABASE.get)


def test_this_machine_its_lan_and_loopback_are_placed_at_its_public_address():
    snap = parse_status(STATUS)
    # The first PUBLIC endpoint, not the first one listed.
    assert snap.public_ip == "69.109.237.253"
    for ip in ("127.0.0.1", "192.168.0.75", "100.108.31.115", "fd7a:115c:a1e0::ba33:1f74"):
        loc = _at(ip)
        assert (loc.lat, loc.lon, loc.source) == (28.76, -81.32, "coordinator"), ip
    assert _at("192.168.0.75").label == "this coordinator's network (Lake Mary, Florida, US)"
    assert _at("100.108.31.115").label == "this coordinator (Lake Mary, Florida, US)"


def test_a_relayed_peer_is_placed_at_its_relay_region_and_says_so():
    loc = _at("100.67.68.35")
    assert (loc.lat, loc.lon, loc.source, loc.city) == (25.76, -80.19, "relay", "Miami")
    assert loc.label == "MacBook (now near the Miami relay)"
    # Either of a peer's addresses is the peer.
    assert _at("fd7a:115c:a1e0::4e33:4424") == loc


def test_a_direct_peer_is_placed_by_its_endpoint():
    # A private endpoint: the peer is on a network this machine reaches directly.
    lan = _at("100.90.181.55")
    assert (lan.source, lan.label) == ("coordinator", "agx, now on this coordinator's network (Lake Mary, Florida, US)")
    # A public one (here IPv4-mapped, bracketed): the database places it.
    away = _at("100.70.0.9")
    assert (away.lat, away.source, away.label) == (38.72, "database", "travel (now in Lisbon, Lisbon, PT)")


def test_what_tailscale_does_not_know_stays_unplaced():
    assert _at("100.99.99.99") is None  # a tailnet address that is no peer
    assert _at("100.70.0.10") is None  # a relay region with no coordinates
    assert _at("garbage") is None
    assert locate_local("192.168.0.75", None, DATABASE.get) is None  # no Tailscale
    # This machine's public address with no database record: nothing to place local traffic at.
    assert locate_local("192.168.0.75", parse_status(STATUS), {}.get) is None


def test_refresh_reads_the_daemon_once_a_minute_and_keeps_the_last_answer(tmp_path):
    script = tmp_path / "tailscale"
    calls = tmp_path / "calls"
    script.write_text(f'#!/bin/sh\necho x >> {calls}\ncat <<"EOF"\n{json.dumps(STATUS)}\nEOF\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    net = Tailnet(str(script))
    asyncio.run(net.refresh())
    asyncio.run(net.refresh())
    assert net.snapshot is not None and net.snapshot.public_ip == "69.109.237.253"
    assert calls.read_text().count("x") == 1

    script.write_text("#!/bin/sh\necho 'failed to connect to local tailscaled' >&2\nexit 1\n")
    net._asked = float("-inf")
    asyncio.run(net.refresh())
    assert net.error and net.snapshot is not None  # the last good answer stays

    off = Tailnet(None)
    asyncio.run(off.refresh())
    assert (off.snapshot, off.error) == (None, None)
