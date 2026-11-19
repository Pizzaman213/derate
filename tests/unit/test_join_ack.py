"""A join response is believed only when it proves knowledge of our token (N1,
N-3), and what a worker keeps from it is its own agent token (N-2).

Before the first pass, ``adopt_cluster_token`` persisted any ``cluster_token``
in any join response, so a rogue mDNS "coordinator" could evict a worker. Every
response to a proof-bearing join now carries ``ack``, an HMAC keyed by the
token the node presented over the very nonce and timestamp it sent -- and,
since the second pass, over every other field of the response, so an on-path
host cannot swap the credential inside an acknowledged answer. No response
carries the cluster token any more; a member is handed its own random agent
token, and ``cluster.json`` on a worker keeps that instead of the cluster token.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time

import pytest

from control_plane.contracts import DeviceClass, NodeProfile
from control_plane.registry import JoinRejected, joinproof
from control_plane.registry.agent import NodeAgent
from control_plane.registry.bootstrap import (
    RoleDecision,
    post_join,
    reannounce,
    rejoin_until_admitted,
    resolve_role,
)
from control_plane.registry.config import ROLE_WORKER, RegistryConfig
from control_plane.registry.discovery import Advertiser, DiscoveredPeer
from control_plane.registry.identity import (
    ClusterIdentity,
    agent_token,
    load_or_create_identity,
)
from control_plane.registry.startup import NodeRuntime

NODE = NodeProfile(
    node_id="spark-02",
    hostname="spark-02",
    address="10.0.0.12",
    device_class=DeviceClass.GB10,
    gpu_name="NVIDIA GB10",
    gpu_count=1,
    total_memory=128 * 1024**3,
    addressable_memory=96 * 1024**3,
    memory_bandwidth_gbps=273.0,
    compute_capability="12.1",
    driver_version="580.173.02",
)
AGENT_URL = "http://10.0.0.12:8081"
HELD = "held-cluster-token"
DELIVERED = "delivered-agent-token"


def run(coro):
    return asyncio.run(coro)


class Coordinator:
    """Answers a join. ``ack`` picks what it proves: nothing, a wrong key, or
    the token the node really holds, over the body it sends."""

    def __init__(self, ack: str | None, agent_token: str = "evil") -> None:
        self.ack = ack
        self.agent_token = agent_token
        self.calls = 0

    async def post_json(self, url, payload, timeout=None, headers=None):
        self.calls += 1
        body = {
            "node_id": payload["profile"]["node_id"],
            "cluster_id": "c-ours",
            "status": "member",
            "agent_token": self.agent_token,
        }
        key = {"right": HELD, "wrong": "some-other-token"}.get(self.ack or "")
        if key and "proof" in payload:
            proof = payload["proof"]
            body["ack"] = joinproof.ack(
                key, payload["profile"]["node_id"], payload["agent_url"],
                proof["nonce"], proof["ts"], body,
            )
        return body


def _join_via(coordinator):
    async def join(url, token, profile, agent_url, **_):
        return await post_join(url, token, profile, agent_url, client=coordinator)

    return join


async def _no_peers(*_a, **_k):
    return []


def _runtime(tmp_path) -> NodeRuntime:
    """A worker from before per-member tokens: ``cluster.json`` holds the
    cluster token and its agent accepts the token derived from it."""
    load_or_create_identity(tmp_path, cluster_id="c-ours", token=HELD)
    config = RegistryConfig(token=HELD, cluster_id="c-ours", data_dir=tmp_path)
    agent = NodeAgent(profile=NODE, role=ROLE_WORKER, cluster_id="c-ours")
    runtime = NodeRuntime(
        config=config,
        profile=NODE,
        decision=RoleDecision(
            ROLE_WORKER, "http://10.0.0.10:8080", True, "test", status="member"
        ),
        identity=ClusterIdentity("c-ours", HELD),
        node_agent=agent,
        advertiser=Advertiser("spark-02", ROLE_WORKER, "c-ours", NODE.address, 8081),
    )
    agent.set_token(agent_token(HELD, NODE.node_id))
    return runtime


def _stored(tmp_path) -> dict:
    return json.loads((tmp_path / "cluster.json").read_text())


def _announce(runtime: NodeRuntime, coordinator):
    result = run(
        reannounce(
            runtime.config, NODE, AGENT_URL, "http://10.6.6.6:8080",
            browse=_no_peers, join=_join_via(coordinator),
        )
    )
    if result.ok:
        runtime._apply_announcement(result)
    return result


@pytest.mark.parametrize("ack", [None, "wrong"])
def test_a_reannouncement_answered_without_a_valid_ack_adopts_nothing(tmp_path, ack):
    runtime = _runtime(tmp_path)
    before = agent_token(HELD, NODE.node_id)

    result = _announce(runtime, Coordinator(ack))

    assert result.ok is False
    assert _stored(tmp_path) == {"cluster_id": "c-ours", "token": HELD}
    assert runtime.config.token == HELD
    assert runtime.node_agent.token_matches(before)
    assert not runtime.node_agent.token_matches("evil")
    assert runtime.decision.coordinator_url == "http://10.0.0.10:8080"
    assert runtime.identity.cluster_id == "c-ours"


def test_the_right_ack_adopts_the_delivered_agent_token(tmp_path):
    """The migration on the worker side: the delivered token replaces the
    cluster token on disk, and the derived token stops opening the agent."""
    runtime = _runtime(tmp_path)

    result = _announce(runtime, Coordinator("right", agent_token=DELIVERED))

    assert result.ok is True
    stored = _stored(tmp_path)
    assert stored == {"cluster_id": "c-ours", "agent_token": DELIVERED}
    assert "token" not in stored
    assert runtime.config.agent_token == DELIVERED
    assert runtime.node_agent.token_matches(DELIVERED)
    assert not runtime.node_agent.token_matches(agent_token(HELD, NODE.node_id))
    assert runtime.decision.coordinator_url == "http://10.6.6.6:8080"


def test_cluster_json_is_private_after_adoption(tmp_path):
    import stat

    runtime = _runtime(tmp_path)
    _announce(runtime, Coordinator("right", agent_token=DELIVERED))
    assert stat.S_IMODE((tmp_path / "cluster.json").stat().st_mode) == 0o600


def test_an_acked_answer_with_a_swapped_credential_is_refused(tmp_path):
    """N-3: the coordinator's real, acknowledged answer, with one field swapped
    on the way back by a host on the path."""
    from tests.unit.test_registry import FakeClient, make_profile, make_registry

    client = FakeClient()
    client.serve(AGENT_URL, NODE)
    registry = make_registry(
        tmp_path, client=client, local=make_profile("spark-01", address="10.0.0.11"),
        token=HELD,
    )
    enrollment = registry.mint_enrollment()

    class OnPath:
        async def post_json(self, url, payload, timeout=None, headers=None):
            proof = joinproof.from_dict(payload["proof"])
            answer = await registry.handle_join(
                proof, NODE, payload["agent_url"], peer="10.0.0.12"
            )
            assert answer["status"] == "member"
            return {**answer, "agent_token": "attacker-chosen"}

    with pytest.raises(JoinRejected, match="no valid ack"):
        run(post_join("http://10.0.0.11:8080", enrollment.token, NODE, AGENT_URL, client=OnPath()))


def test_an_untouched_acked_answer_is_believed(tmp_path):
    from tests.unit.test_registry import FakeClient, make_profile, make_registry

    client = FakeClient()
    client.serve(AGENT_URL, NODE)
    registry = make_registry(
        tmp_path, client=client, local=make_profile("spark-01", address="10.0.0.11"),
        token=HELD,
    )
    enrollment = registry.mint_enrollment()

    class Direct:
        async def post_json(self, url, payload, timeout=None, headers=None):
            proof = joinproof.from_dict(payload["proof"])
            # Through JSON, as on the wire.
            return json.loads(json.dumps(await registry.handle_join(
                proof, NODE, payload["agent_url"], peer="10.0.0.12"
            )))

    answer = run(post_join(
        "http://10.0.0.11:8080", enrollment.token, NODE, AGENT_URL, client=Direct()
    ))
    assert answer["agent_token"] == registry.agent_token("spark-02")


@pytest.mark.parametrize("ack", [None, "wrong"])
def test_boot_role_resolution_never_takes_a_token_from_an_unacked_coordinator(ack):
    async def rogue(*_a, **_k):
        return [DiscoveredPeer("rogue", "coordinator", "", "10.6.6.6", 8081)]

    decision = run(
        resolve_role(
            RegistryConfig(token=HELD), NODE, AGENT_URL,
            browse=rogue, join=_join_via(Coordinator(ack)),
        )
    )

    assert decision.agent_token != "evil"
    assert decision.joined is False


def test_the_rejoin_loop_never_adopts_from_an_unacked_answer(tmp_path):
    load_or_create_identity(tmp_path, cluster_id="c-ours", token=HELD)
    config = RegistryConfig(token=HELD, data_dir=tmp_path)
    decision = RoleDecision(
        ROLE_WORKER, "http://10.6.6.6:8080", False, "test", status="candidate"
    )
    rounds = iter([True, False])

    async def sleep(_s):
        return None

    result = run(
        rejoin_until_admitted(
            decision, config, NODE, AGENT_URL,
            join=_join_via(Coordinator(None)),
            sleep=sleep, jitter=lambda: 0.0,
            should_continue=lambda: next(rounds),
        )
    )

    assert result is None
    assert _stored(tmp_path) == {"cluster_id": "c-ours", "token": HELD}


def test_a_held_agent_token_is_presented_first(tmp_path):
    """A migrated worker leads with its own token; the environment's token is
    only the fallback (a fresh enrollment token carried here to re-home it)."""
    presented: list[str] = []

    async def join(url, token, profile, agent_url, **_):
        presented.append(token)
        return {"node_id": "spark-02", "status": "member", "cluster_id": "c-ours"}

    config = RegistryConfig(token="ej_env", agent_token=DELIVERED, data_dir=tmp_path)
    decision = run(resolve_role(
        dataclasses.replace(config, join_address="10.0.0.10"), NODE, AGENT_URL, join=join,
    ))
    assert presented == [DELIVERED]
    assert decision.token_used == DELIVERED


def test_a_tokenless_join_adopts_nothing_even_with_an_ack_field(tmp_path):
    class Answer:
        async def post_json(self, url, payload, timeout=None, headers=None):
            assert "proof" not in payload
            return {
                "node_id": "spark-02", "status": "member", "cluster_id": "c-evil",
                "agent_token": "evil", "ack": "x",
            }

    result = run(post_join("http://10.6.6.6:8080", None, NODE, AGENT_URL, client=Answer()))

    assert "agent_token" not in result and "ack" not in result
    assert not (tmp_path / "cluster.json").exists()


def test_time_is_not_a_field_the_ack_can_ignore():
    """The ack covers the body, so a re-ordered dict still verifies and a
    changed value does not."""
    proof = joinproof.make("k", "spark-02", AGENT_URL, time.time())
    body = {"status": "member", "node_id": "spark-02", "cluster_id": "c"}
    mac = joinproof.ack("k", "spark-02", AGENT_URL, proof.nonce, proof.ts, body)
    shuffled = {"cluster_id": "c", "node_id": "spark-02", "status": "member", "ack": mac}
    assert joinproof.ack_matches(mac, "k", "spark-02", AGENT_URL, proof, shuffled)
    assert not joinproof.ack_matches(
        mac, "k", "spark-02", AGENT_URL, proof, {**shuffled, "status": "candidate"}
    )
