"""Proof of possession for ``POST /api/nodes/join``, in both directions.

The joiner never sends its cluster or enrollment token. It sends an HMAC over
its node id, the agent URL it wants recorded, a fresh nonce and the time, keyed
by the token. Whoever answers the POST -- including a rogue mDNS "coordinator"
-- learns nothing it can replay: the MAC is bound to that agent URL, expires
after ``JOIN_PROOF_SKEW_S`` and its nonce is accepted once.

The answer proves itself the same way. A coordinator that verified the proof
returns ``ack``: the same HMAC under the same token over ``"ack\\n" + node_id``,
the joiner's own nonce and time, *and the canonical form of every other field
in the response*. Only something that holds the token the joiner presented can
compute it, so a node adopts nothing -- an agent token, a cluster id, a status
-- from a response that lacks it, and an on-path host cannot swap one field of
an acknowledged answer for its own value: the ack would no longer match.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import Mapping

JOIN_PROOF_SKEW_S = 120


@dataclass(frozen=True)
class JoinProof:
    nonce: str
    ts: int
    mac: str

    def to_dict(self) -> dict:
        return {"nonce": self.nonce, "ts": self.ts, "mac": self.mac}


def sign(token: str, node_id: str, agent_url: str, nonce: str, ts: int) -> str:
    msg = f"{node_id}\n{agent_url}\n{nonce}\n{ts}".encode()
    return hmac.new(token.encode(), msg, hashlib.sha256).hexdigest()


def make(token: str, node_id: str, agent_url: str, now: float) -> JoinProof:
    nonce = secrets.token_hex(16)
    ts = int(now)
    return JoinProof(nonce, ts, sign(token, node_id, agent_url, nonce, ts))


def matches(proof: JoinProof, token: str, node_id: str, agent_url: str) -> bool:
    expected = sign(token, node_id, agent_url, proof.nonce, proof.ts)
    return hmac.compare_digest(expected, str(proof.mac))


def _canonical(body: Mapping) -> str:
    """Every field of a join response except ``ack`` itself, in one spelling."""
    return json.dumps(
        {k: v for k, v in body.items() if k != "ack"},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def ack(
    token: str, node_id: str, agent_url: str, nonce: str, ts: int, body: Mapping
) -> str:
    """The coordinator's answer to a proof it verified under *token*, bound to
    the response *body* it is about to send (every key but ``ack``)."""
    msg = f"ack\n{node_id}\n{agent_url}\n{nonce}\n{ts}\n{_canonical(body)}".encode()
    return hmac.new(token.encode(), msg, hashlib.sha256).hexdigest()


def ack_matches(
    presented: object,
    token: str,
    node_id: str,
    agent_url: str,
    sent: JoinProof,
    body: Mapping,
) -> bool:
    """Whether a join response's ``ack`` answers the proof this node *sent*
    and covers every other field of the *body* it arrived in."""
    if not isinstance(presented, str) or not presented:
        return False
    expected = ack(token, node_id, agent_url, sent.nonce, sent.ts, body)
    return hmac.compare_digest(expected.encode(), presented.encode("utf-8", "ignore"))


def from_dict(data: object) -> JoinProof:
    """Parse a body's ``proof``; ValueError unless all three fields are there."""
    if not isinstance(data, dict):
        raise ValueError("proof must be an object")
    nonce, ts, mac = data.get("nonce"), data.get("ts"), data.get("mac")
    if not isinstance(nonce, str) or not nonce or not isinstance(mac, str) or not mac:
        raise ValueError("proof needs nonce and mac")
    if isinstance(ts, bool) or not isinstance(ts, int):
        raise ValueError("proof needs an integer ts")
    return JoinProof(nonce, ts, mac)


class NonceCache:
    """Nonces seen inside the skew window; older ones are refused by ts anyway."""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}

    def seen_or_add(self, nonce: str, now: float) -> bool:
        cutoff = now - 2 * JOIN_PROOF_SKEW_S
        for key in [k for k, t in self._seen.items() if t < cutoff]:
            del self._seen[key]
        if nonce in self._seen:
            return True
        self._seen[nonce] = now
        return False
