"""gateway/request_route.py: a request's row read into the servers it passed
through, in order, and each one placed -- or the reason it cannot be."""

from __future__ import annotations

from control_plane.gateway.request_route import (
    GONE_NODE,
    PROVIDER_NOTE,
    place_hops,
    resolve_host,
    resolve_hosts,
    route_hops,
)
from control_plane.telemetry.geoip import Location

NODES = {"spark-01": "192.168.11.13", "ws-3090": "192.168.11.20"}
DEPLOYMENTS = {"d-pair": ["spark-01", "ws-3090"], "d-empty": []}
PROVIDERS = {"openrouter": "https://openrouter.ai/api/v1", "lan": "http://192.168.11.50:8000/v1"}
LAB = Location(45.5, -73.6, None, None, None, label="Lab", source="place")
SF = Location(37.77, -122.42, "San Francisco", "California", "US")


def _hops(row):
    return route_hops(
        row,
        node_address=NODES.get,
        deployment_nodes=DEPLOYMENTS.get,
        provider_url=PROVIDERS.get,
    )


def _locate(ip):
    if ip.startswith("192.168.11."):
        return LAB
    return SF if ip == "104.18.2.115" else None


def test_hops_follow_the_request_client_gateway_then_each_attempt_in_order():
    row = {
        "client_ip": "100.67.68.35",
        "node_id": "spark-01",
        "attempt_chain": [
            {"target_id": "d-pair", "status": 503, "ms": 12.0},
            {"target_id": "openrouter:qwen/qwen3-30b-a3b", "status": 200, "ms": 640.5},
        ],
    }
    hops = _hops(row)
    assert [(h["role"], h["name"], h["attempt"]) for h in hops] == [
        ("client", "100.67.68.35", None),
        ("gateway", "spark-01", None),
        ("node", "spark-01", 1),
        ("node", "ws-3090", 1),
        ("provider", "openrouter", 2),
    ]
    # Each node of a two-node deployment carries the attempt's outcome.
    assert [(h["status"], h["ms"]) for h in hops[2:4]] == [(503, 12.0), (503, 12.0)]
    assert hops[4]["host"] == "openrouter.ai" and hops[4]["address"] is None
    assert hops[4]["target_id"] == "openrouter:qwen/qwen3-30b-a3b"

    resolve_hosts(hops, {"openrouter.ai": "104.18.2.115"}.get)
    place_hops(hops, _locate)
    assert hops[0]["location"] is None  # a tailnet address nothing covers
    assert hops[1]["location"]["label"] == "Lab" and hops[1]["note"] is None
    assert hops[4]["location"]["city"] == "San Francisco"
    assert hops[4]["note"] == PROVIDER_NOTE


def test_a_row_with_no_chain_is_its_own_single_attempt():
    row = {"client_ip": "8.8.8.8", "node_id": "spark-01", "target_id": "d-pair", "status": 200, "duration_ms": 88.0}
    hops = _hops(row)
    assert [(h["role"], h["status"], h["ms"]) for h in hops[2:]] == [("node", 200, 88.0), ("node", 200, 88.0)]


def test_what_cannot_be_known_says_why():
    row = {
        "client_ip": None,
        "node_id": "gone-node",
        "attempt_chain": [
            {"target_id": "d-deleted", "status": 502},
            {"target_id": "d-empty", "status": 502},
            {"target_id": "removed:some/model", "status": 401},
            {"target_id": "openrouter:x", "status": 200},
        ],
    }
    hops = _hops(row)
    resolve_hosts(hops, lambda _host: None)
    place_hops(hops, _locate)
    notes = [(h["role"], h["name"], h["note"]) for h in hops]
    assert notes == [
        ("client", "no address recorded", None),
        ("gateway", "gone-node", GONE_NODE),
        ("target", "d-deleted", "This deployment no longer exists, so its nodes are not known."),
        ("target", "d-empty", "The deployment has no nodes on record."),
        ("provider", "removed", "This provider no longer exists, so its endpoint is not known."),
        ("provider", "openrouter", "openrouter.ai did not resolve."),
    ]
    assert all(h["location"] is None for h in hops)


def test_a_provider_on_a_known_place_carries_no_anycast_note():
    hops = _hops({"node_id": "spark-01", "attempt_chain": [{"target_id": "lan:m", "status": 200}]})
    resolve_hosts(hops, resolve_host)  # an address literal: no DNS
    place_hops(hops, _locate)
    provider = hops[-1]
    assert (provider["address"], provider["location"]["label"], provider["note"]) == ("192.168.11.50", "Lab", None)


def test_resolve_host_returns_a_literal_without_a_lookup():
    assert resolve_host("192.168.11.50") == "192.168.11.50"
    assert resolve_host("::1") == "::1"
