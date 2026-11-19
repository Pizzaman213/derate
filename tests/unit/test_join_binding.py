"""A join is bound to the machine that sent it, and every change is audited.

Plan steps 1.3, 1.4 and 5.15. Before this a cluster-token holder could re-point
any member -- the coordinator's own row included -- at a host of its choosing,
the open join route would make the coordinator GET loopback or the metadata
address, a re-poll from a known candidate dodged the candidate cap, and a
successful join (or a member's traffic moving) left only a log line.
"""

from __future__ import annotations

import dataclasses
import logging
import time

import pytest
from fastapi.testclient import TestClient

from control_plane.contracts.auth import AuditAction
from control_plane.gateway.app import create_app
from control_plane.registry import JoinRejected, ProbeFailed, joinproof
from control_plane.registry.identity import agent_token
from control_plane.registry.serde import profile_from_dict, profile_to_dict
from tests.unit.test_gateway import build_deps
from tests.unit.test_registry import FakeClient, make_profile, make_registry, run

TOKEN = "tok-123"
COORD = make_profile("spark-01", address="10.0.0.11")
NODE = make_profile("spark-02", address="10.0.0.12")
URL = "http://10.0.0.12:8081"


def _registry(tmp_path, client: FakeClient):
    return make_registry(tmp_path, client=client, local=COORD, token=TOKEN)


def _body(profile, agent_url: str, token: str | None = TOKEN) -> dict:
    body = {"profile": profile_to_dict(profile), "agent_url": agent_url}
    if token:
        body["proof"] = joinproof.make(
            token, profile.node_id, agent_url, time.time()
        ).to_dict()
    return body


def _rows(app, action: AuditAction):
    rows, _ = app.state.ctx.auth_stores.auth.audit_page(action=action.value, page_size=100)
    return rows


# ---------------------------------------------------------------------------
# the route: agent_url must name the requester (N2, N4)
# ---------------------------------------------------------------------------






_NUMERIC_METADATA = ("2852039166", "0xa9fea9fe", "0251.0376.0251.0376")


@pytest.mark.parametrize("host", _NUMERIC_METADATA)
def test_a_numeric_literal_host_is_not_a_peer_address(host):
    """``inet_aton`` reads each of these as 169.254.169.254 (K3r)."""
    from control_plane.registry.net import is_peer_address

    assert is_peer_address(host) is False








# ---------------------------------------------------------------------------
# handle_join: the coordinator row, the profile address
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("token", [None, TOKEN])
def test_the_coordinators_own_row_is_not_joinable(tmp_path, token):
    client = FakeClient()
    client.serve("http://10.6.6.6:8081", make_profile("spark-01", address="10.6.6.6"))
    registry = _registry(tmp_path, client)
    before = registry.agent_url("spark-01")
    proof = joinproof.make(token, "spark-01", "http://10.6.6.6:8081", time.time()) if token else None
    with pytest.raises(JoinRejected, match="the coordinator's own row is not joinable"):
        run(registry.handle_join(proof, COORD, "http://10.6.6.6:8081", peer="10.6.6.6"))
    assert registry.agent_url("spark-01") == before


def test_a_credentialed_join_must_describe_the_requester(tmp_path):
    client = FakeClient()
    # The machine at the requester's address answers with a profile that
    # says the cluster should ssh somewhere else.
    client.serve(URL, make_profile("spark-02", address="10.0.0.99"))
    registry = _registry(tmp_path, client)
    proof = joinproof.make(TOKEN, "spark-02", URL, time.time())
    with pytest.raises(JoinRejected, match="profile address does not match the requester"):
        run(registry.handle_join(proof, NODE, URL, peer="10.0.0.12"))
    assert registry.candidates() == []


def test_every_verified_answer_carries_an_ack_and_a_member_its_agent_token(tmp_path):
    client = FakeClient()
    client.serve(URL, NODE)
    registry = _registry(tmp_path, client)
    proof = joinproof.make(TOKEN, "spark-02", URL, time.time())
    candidate = run(registry.handle_join(proof, NODE, URL, peer="10.0.0.12"))
    assert candidate["status"] == "candidate"
    assert joinproof.ack_matches(candidate["ack"], TOKEN, "spark-02", URL, proof, candidate)
    assert "agent_token" not in candidate

    # Admitted by a human: its next join on the cluster token collects the
    # token minted for it, once.
    registry.admit("spark-02")
    again = joinproof.make(TOKEN, "spark-02", URL, time.time())
    member = run(registry.handle_join(again, NODE, URL, peer="10.0.0.12"))
    assert joinproof.ack_matches(member["ack"], TOKEN, "spark-02", URL, again, member)
    assert member["agent_token"] == registry.agent_token("spark-02")
    assert member["agent_token"] != agent_token(TOKEN, "spark-02")


def test_a_tokenless_answer_carries_nothing_to_adopt(tmp_path):
    client = FakeClient()
    client.serve(URL, NODE)
    registry = _registry(tmp_path, client)
    result = run(registry.handle_join(None, NODE, URL, peer="10.0.0.12"))
    assert result == {"node_id": "spark-02", "status": "candidate"}


def test_a_member_move_is_logged(tmp_path, caplog):
    client = FakeClient()
    client.serve(URL, NODE)
    moved = make_profile("spark-02", address="10.0.0.13")
    client.serve("http://10.0.0.13:8081", moved)
    registry = _registry(tmp_path, client)
    run(registry.add_node("10.0.0.12"))
    # A move is the member's own act: on its own agent token.
    own = registry.agent_token("spark-02")
    proof = joinproof.make(own, "spark-02", "http://10.0.0.13:8081", time.time())
    with caplog.at_level(logging.WARNING, logger="control_plane.registry.registry"):
        run(registry.handle_join(proof, moved, "http://10.0.0.13:8081", peer="10.0.0.13"))
    assert any("member spark-02 moved" in r.getMessage() for r in caplog.records)
    assert registry.agent_url("spark-02") == "http://10.0.0.13:8081"


def test_a_profile_that_is_not_a_json_object_is_an_unusable_profile(tmp_path):
    class ListClient(FakeClient):
        async def get_json(self, url, timeout, headers=None):
            return ["not", "a", "profile"]

    registry = _registry(tmp_path, ListClient())
    with pytest.raises(ProbeFailed, match="unusable profile"):
        run(registry.probe_remote(URL))


# ---------------------------------------------------------------------------
# node_joined (F4)
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# node_id grammar (N5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "node_id", ["a/b", "spark-01\nforged", "Spark-01", "spark_01", "x" * 64, "-lead", ""]
)
def test_a_node_id_that_is_not_a_hostname_slug_is_refused(node_id):
    data = profile_to_dict(dataclasses.replace(NODE, node_id="spark-02")) | {"node_id": node_id}
    with pytest.raises(ValueError, match="invalid node_id"):
        profile_from_dict(data)


def test_a_slug_node_id_is_accepted():
    data = profile_to_dict(NODE) | {"node_id": "x" * 63}
    assert profile_from_dict(data).node_id == "x" * 63


def test_the_join_route_refuses_a_bad_node_id_before_dialling(tmp_path):
    client = FakeClient()
    registry = _registry(tmp_path, client)
    body = _body(NODE, URL, None)
    body["profile"]["node_id"] = "a/b"
    with TestClient(create_app(build_deps(registry=registry)), client=("10.0.0.12", 5000)) as http:
        res = http.post("/api/nodes/join", json=body)
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "invalid_join_body"
    assert registry.candidates() == []


# ---------------------------------------------------------------------------
# second pass: a member re-joins only with its own agent token (N-1, N-2,
# CT-1), candidates are not displaced (N-7), joins are windowed per address
# (N-8)
# ---------------------------------------------------------------------------


def _unmigrated_member(tmp_path, profile=NODE, agent_url=URL) -> None:
    """A roster written before per-member tokens existed: no ``agent_token``."""
    from control_plane.registry.roster import save_roster

    save_roster(
        tmp_path,
        {profile.node_id: {"profile": profile_to_dict(profile), "agent_url": agent_url}},
        {},
    )


def _migrated(tmp_path, client: FakeClient):
    """A registry whose spark-02 has re-joined once and holds its own token."""
    _unmigrated_member(tmp_path)
    registry = _registry(tmp_path, client)
    first = run(registry.handle_join(
        joinproof.make(TOKEN, "spark-02", URL, time.time()), NODE, URL, peer="10.0.0.12"
    ))
    return registry, first


def test_a_cluster_token_holder_cannot_move_another_member(tmp_path):
    """N-1: spark-03's machine, holding the cluster token every worker holds,
    claims to be spark-02 and asks for spark-02's traffic."""
    client = FakeClient()
    client.serve(URL, NODE)
    impostor = make_profile("spark-02", address="10.0.0.13")
    client.serve("http://10.0.0.13:8081", impostor)
    registry, _ = _migrated(tmp_path, client)
    proof = joinproof.make(TOKEN, "spark-02", "http://10.0.0.13:8081", time.time())
    with pytest.raises(JoinRejected, match="re-join with its own agent token"):
        run(registry.handle_join(proof, impostor, "http://10.0.0.13:8081", peer="10.0.0.13"))
    assert registry.agent_url("spark-02") == URL


def test_a_pre_upgrade_member_migrates_once_on_the_cluster_token(tmp_path):
    client = FakeClient()
    client.serve(URL, NODE)
    registry, first = _migrated(tmp_path, client)

    assert first["status"] == "member"
    delivered = first["agent_token"]
    assert delivered and delivered != agent_token(TOKEN, "spark-02")
    assert registry.agent_token("spark-02") == delivered
    assert "cluster_token" not in first

    again = joinproof.make(TOKEN, "spark-02", URL, time.time())
    with pytest.raises(JoinRejected, match="re-join with its own agent token"):
        run(registry.handle_join(again, NODE, URL, peer="10.0.0.12"))

    own = joinproof.make(delivered, "spark-02", URL, time.time())
    facts: dict = {}
    answer = run(registry.handle_join(own, NODE, URL, peer="10.0.0.12", facts=facts))
    assert answer["status"] == "member"
    assert answer["agent_token"] == delivered
    assert facts["source"] == "agent_token"
    assert joinproof.ack_matches(answer["ack"], delivered, "spark-02", URL, own, answer)


def test_the_roster_round_trips_each_members_agent_token(tmp_path):
    import json
    import stat

    client = FakeClient()
    client.serve(URL, NODE)
    registry, first = _migrated(tmp_path, client)
    roster = tmp_path / "registry.json"
    assert json.loads(roster.read_text())["members"]["spark-02"]["agent_token"] == first["agent_token"]
    assert stat.S_IMODE(roster.stat().st_mode) == 0o600

    reborn = _registry(tmp_path, client)
    assert reborn.agent_token("spark-02") == first["agent_token"]
    reborn.remove_node("spark-02")
    assert "spark-02" not in json.loads(roster.read_text())["members"]
    assert reborn.agent_token("spark-02") is None


def test_no_join_response_ever_carries_the_cluster_token(tmp_path):
    client = FakeClient()
    client.serve(URL, NODE)
    registry = _registry(tmp_path, client)
    enrollment = registry.mint_enrollment()
    admitted = run(registry.handle_join(
        joinproof.make(enrollment.token, "spark-02", URL, time.time()), NODE, URL,
        peer="10.0.0.12",
    ))
    assert admitted["status"] == "member"
    assert "cluster_token" not in admitted
    assert TOKEN not in str(admitted)
    assert admitted["agent_token"] == registry.agent_token("spark-02")


def _rotate(http, password=None):
    from tests.unit.conftest import BOOT_PASSWORD

    return http.post(
        "/api/cluster/token/rotate",
        json={"current_password": password or BOOT_PASSWORD},
    )






def test_a_waiting_candidate_cannot_be_displaced(tmp_path):
    """N-7: a LAN host names spark-04 while the real one waits for admission."""
    real = make_profile("spark-04", address="10.0.0.14")
    fake = make_profile("spark-04", address="10.0.0.66")
    client = FakeClient()
    client.serve("http://10.0.0.14:8081", real)
    client.serve("http://10.0.0.66:8081", fake)
    registry = _registry(tmp_path, client)
    app = create_app(build_deps(registry=registry))
    with TestClient(app, client=("10.0.0.14", 5000)) as http:
        first = http.post("/api/nodes/join", json=_body(real, "http://10.0.0.14:8081", None))
    with TestClient(app, client=("10.0.0.66", 5000)) as http:
        second = http.post("/api/nodes/join", json=_body(fake, "http://10.0.0.66:8081", None))
    assert first.status_code == 200
    assert second.status_code == 403
    assert "already waiting from another address" in second.json()["error"]["message"]
    [record] = registry.candidates()
    assert record["agent_url"] == "http://10.0.0.14:8081"


