"""The servers one request passed through, in order, each placed on the map.

A request's row already names every server it touched: ``client_ip``, the
``node_id`` whose gateway took it, and ``attempt_chain`` -- each attempt's
``target_id``, which is a deployment (its plan's nodes) or a provider's
``provider:upstream`` target (the provider's ``base_url``). This module turns
that into hops and places each one with the same resolution order the request
map uses (a known place, then the IP database), at read time and never stored.

What it cannot know it says: a deployment deleted since has no nodes on
record, a node no longer in the cluster has no address, and a provider's
endpoint is placed where its ADDRESS is registered -- a CDN or anycast front
door can be far from where the model ran, so that hop carries a note rather
than passing for the inference site.

Three steps, so each runs where it must: :func:`route_hops` reads the row
against the cluster, :func:`resolve_hosts` does DNS (blocking, so the caller
runs it in a thread), :func:`place_hops` locates -- on the event loop, which
is where :class:`~control_plane.telemetry.geoip.GeoDatabase` is called.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import asdict
from typing import Any
from urllib.parse import urlparse

from control_plane.telemetry.geoip import Location, classify

#: The note on a provider hop placed by the IP database.
PROVIDER_NOTE = (
    "Where the provider's address is registered: a CDN or anycast front door "
    "can be far from where the model ran."
)
GONE_NODE = "This node is no longer in the cluster."


def _hop(role: str, name: str, address: str | None, **extra: Any) -> dict[str, Any]:
    return {
        "role": role,
        "name": name,
        "address": address,
        "host": None,
        "location": None,
        "attempt": None,
        "status": None,
        "ms": None,
        "target_id": None,
        "note": None,
        **extra,
    }


def _attempts(row: dict[str, Any]) -> list[dict[str, Any]]:
    """The attempts in order; a row with no chain recorded is its own one."""
    chain = row.get("attempt_chain")
    if isinstance(chain, list) and chain:
        return [a for a in chain if isinstance(a, dict) and a.get("target_id")]
    if row.get("target_id"):
        return [{"target_id": row["target_id"], "status": row.get("status"), "ms": row.get("duration_ms")}]
    return []


def route_hops(
    row: dict[str, Any],
    *,
    node_address: Callable[[str], str | None],
    deployment_nodes: Callable[[str], list[str] | None],
    provider_url: Callable[[str], str | None],
) -> list[dict[str, Any]]:
    """Client, gateway, then each attempt's servers, in the order the request
    met them, unplaced. ``deployment_nodes`` answers None for an id that is not
    a deployment; ``provider_url`` None for a provider that no longer exists."""
    ip = row.get("client_ip") or None
    hops = [_hop("client", ip or "no address recorded", ip)]

    gateway = row.get("node_id") or ""
    if gateway:
        addr = node_address(gateway)
        hops.append(_hop("gateway", gateway, addr, note=None if addr else GONE_NODE))

    for n, attempt in enumerate(_attempts(row), start=1):
        tid = str(attempt["target_id"])
        result = {"attempt": n, "status": attempt.get("status"), "ms": attempt.get("ms"), "target_id": tid}
        nodes = deployment_nodes(tid)
        if nodes is not None:
            for node in nodes:
                addr = node_address(node)
                hops.append(_hop("node", node, addr, **result, note=None if addr else GONE_NODE))
            if not nodes:
                hops.append(_hop("target", tid, None, **result, note="The deployment has no nodes on record."))
            continue
        provider_id, sep, _ = tid.partition(":")
        if not sep:
            hops.append(
                _hop("target", tid, None, **result, note="This deployment no longer exists, so its nodes are not known.")
            )
            continue
        url = provider_url(provider_id)
        host = urlparse(url).hostname if url else None
        if host:
            hops.append(_hop("provider", provider_id, None, host=host, **result))
        else:
            hops.append(
                _hop("provider", provider_id, None, **result,
                     note="This provider no longer exists, so its endpoint is not known.")
            )
    return hops


def resolve_hosts(hops: list[dict[str, Any]], resolve: Callable[[str], str | None]) -> None:
    """Fill each provider hop's address from its host. Blocking: DNS."""
    for hop in hops:
        if hop["host"] and hop["address"] is None:
            hop["address"] = resolve(hop["host"])
            if hop["address"] is None:
                hop["note"] = f"{hop['host']} did not resolve."


def place_hops(hops: list[dict[str, Any]], locate: Callable[[str], Location | None]) -> None:
    """Locate every hop that has an address; note a provider placed by the
    database, whose registered address says little about where it ran."""
    for hop in hops:
        loc = locate(hop["address"]) if hop["address"] else None
        hop["location"] = asdict(loc) if loc is not None else None
        if hop["role"] == "provider" and loc is not None and loc.source == "database":
            hop["note"] = PROVIDER_NOTE


def resolve_host(host: str) -> str | None:
    """The first address *host* resolves to, or None. An address literal is
    returned as it is, without a lookup."""
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return None
    for info in infos:
        addr = str(info[4][0])
        if classify(addr) != "invalid":
            return addr
    return None
