"""Naming a node, and finding out whether two nodes can reach each other.

Two features that arrived together because they answer the same operator
question -- "which machine am I looking at, and is it actually talking to the
others?" -- from opposite ends: one makes the plate unambiguous, the other
makes the line between two plates checkable in a second rather than a minute.

The rule both share, and what most of these tests pin down: a name is never an
identity, and an absence is never a zero. A rename must not move `node_id`
(deployments, links and routing on disk are keyed by it), and a leg that did
not answer must not carry a millisecond figure (0 ms beside "unreachable"
reads as a fast link, which is the same error `links/measure.py` refuses to
make with bandwidth).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from control_plane.gateway import GatewayDeps, GatewaySettings, create_app
from control_plane.registry import NodeAgent, NodeNotFound
from control_plane.registry.agent import create_agent_app
from control_plane.registry.labels import MAX_LABEL_LEN, normalize_label
from control_plane.registry.reach import (
    ReachLeg,
    UnusableTarget,
    dial,
    summarize,
    unknown_leg,
    validate_target,
)
from control_plane.registry.stub import StubRegistry
from tests.fixtures import SPARK_01


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class NamedRegistry(StubRegistry):
    """A registry that can be renamed and asked about reachability.

    Subclasses the stub rather than the real Registry so these tests exercise
    the routes -- what they map, what they refuse -- without a network.
    """

    def __init__(self, *, reach=None):
        super().__init__()
        self.labels: dict[str, str] = {}
        self.renames: list[tuple[str, object]] = []
        self._reach = reach

    def node_labels(self):
        return dict(self.labels)

    def node_label(self, node_id):
        return self.labels.get(node_id)

    def set_node_label(self, node_id, label):
        self.renames.append((node_id, label))
        if self.get_node(node_id) is None:
            raise NodeNotFound(f"no member {node_id!r}")
        clean = normalize_label(label)
        if clean is None:
            self.labels.pop(node_id, None)
        else:
            self.labels[node_id] = clean
        return clean

    async def check_reach(self, a, b):
        if self.get_node(a) is None or self.get_node(b) is None:
            raise NodeNotFound(f"no member {a!r} or {b!r}")
        if self._reach is not None:
            return self._reach
        legs = [ReachLeg(source="coordinator", target=b, url="http://x:8081", ok=True, ms=1.0)]
        ok, summary = summarize(legs)
        return {
            "a": a, "b": b, "ok": ok, "summary": summary,
            "checked_at": 1.0, "legs": [leg.as_dict() for leg in legs],
        }


def app(registry=None):
    return create_app(
        GatewayDeps(registry=registry or NamedRegistry()),
        settings=GatewaySettings(cluster_id="c-test"),
    )


def a_node(registry) -> str:
    return registry.list_nodes()[0].profile.node_id


# ---------------------------------------------------------------------------
# the name itself
# ---------------------------------------------------------------------------


def test_a_name_is_normalized_to_one_line():
    assert normalize_label("  Rack  2   box \n") == "Rack 2 box"


def test_blank_and_absent_both_mean_no_name():
    """Select-all, delete, save is how a human asks for the default back.
    Answering that with 'a name is required' leaves no way to undo a rename."""
    assert normalize_label(None) is None
    assert normalize_label("") is None
    assert normalize_label("   ") is None


def test_a_name_that_cannot_be_rendered_is_refused_with_a_reason():
    with pytest.raises(ValueError) as caught:
        normalize_label("x" * (MAX_LABEL_LEN + 1))
    assert str(MAX_LABEL_LEN) in str(caught.value)
    with pytest.raises(ValueError):
        normalize_label("a\x00b")
    with pytest.raises(ValueError):
        normalize_label(17)


# ---------------------------------------------------------------------------
# the rename route
# ---------------------------------------------------------------------------


def test_renaming_a_node_echoes_the_stored_name():
    registry = NamedRegistry()
    node_id = a_node(registry)
    with TestClient(app(registry)) as client:
        reply = client.put(f"/api/nodes/{node_id}/label", json={"label": "  Rack 2 "})
    assert reply.status_code == 200
    assert reply.json() == {"node_id": node_id, "label": "Rack 2"}


def test_a_renamed_node_keeps_its_node_id_everywhere_it_appears():
    """The whole safety property. A rename changes the caption; every payload
    that keys off node_id keeps keying off the same one."""
    registry = NamedRegistry()
    node_id = a_node(registry)
    with TestClient(app(registry)) as client:
        client.put(f"/api/nodes/{node_id}/label", json={"label": "Rack 2"})
        nodes = client.get("/api/nodes").json()
        topology = client.get("/api/topology").json()
        cluster = client.get("/api/cluster").json()

    for row in (
        next(n for n in nodes if n["node_id"] == node_id),
        next(n for n in topology["nodes"] if n["node_id"] == node_id),
        next(n for n in cluster["nodes"] if n["node_id"] == node_id),
    ):
        assert row["node_id"] == node_id
        assert row["label"] == "Rack 2"


def test_a_node_nobody_renamed_carries_a_null_label_not_its_node_id():
    """The UI has to tell 'renamed' from 'never renamed' -- only one of those
    should follow the node_id if the id itself ever changes."""
    with TestClient(app()) as client:
        rows = client.get("/api/nodes").json()
    assert rows and all(row["label"] is None for row in rows)


def test_clearing_a_name_puts_the_node_id_back():
    registry = NamedRegistry()
    node_id = a_node(registry)
    with TestClient(app(registry)) as client:
        client.put(f"/api/nodes/{node_id}/label", json={"label": "Rack 2"})
        reply = client.put(f"/api/nodes/{node_id}/label", json={"label": ""})
        rows = client.get("/api/nodes").json()
    assert reply.json()["label"] is None
    assert next(r for r in rows if r["node_id"] == node_id)["label"] is None


def test_renaming_an_unknown_node_is_a_404():
    with TestClient(app()) as client:
        reply = client.put("/api/nodes/spark-99/label", json={"label": "Ghost"})
    assert reply.status_code == 404
    assert reply.json()["error"]["code"] == "node_not_found"


def test_an_unusable_name_is_a_400_carrying_the_registry_sentence():
    registry = NamedRegistry()
    node_id = a_node(registry)
    with TestClient(app(registry)) as client:
        reply = client.put(f"/api/nodes/{node_id}/label", json={"label": "x" * 400})
    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == "invalid_label"
    assert str(MAX_LABEL_LEN) in reply.json()["error"]["message"]


def test_a_body_without_a_label_key_is_refused_rather_than_read_as_a_clear():
    """{} and {"label": null} are different requests: one is malformed, the
    other deliberately removes the name."""
    registry = NamedRegistry()
    node_id = a_node(registry)
    with TestClient(app(registry)) as client:
        assert client.put(f"/api/nodes/{node_id}/label", json={}).status_code == 400
        assert client.put(f"/api/nodes/{node_id}/label", content="not json").status_code == 400
        assert registry.renames == []
        assert client.put(f"/api/nodes/{node_id}/label", json={"label": None}).status_code == 200


def test_a_registry_that_cannot_rename_says_so_rather_than_pretending():
    with TestClient(create_app(GatewayDeps(registry=StubRegistry()))) as client:
        reply = client.put("/api/nodes/spark-01/label", json={"label": "Rack 2"})
    assert reply.status_code == 501
    assert reply.json()["error"]["code"] == "not_implemented"


def test_a_registry_without_labels_still_serves_every_node_payload():
    """Labels are read through a getattr for exactly this: a build without
    them is a cluster where nothing was renamed, not a broken /api/nodes."""
    with TestClient(create_app(GatewayDeps(registry=StubRegistry()))) as client:
        assert client.get("/api/nodes").status_code == 200
        assert client.get("/api/topology").status_code == 200
        assert client.get("/api/cluster").status_code == 200


# ---------------------------------------------------------------------------
# the reachability probe
# ---------------------------------------------------------------------------


def test_only_an_http_address_can_be_dialled():
    """/agent/reach makes a node dial whatever it is handed. file:// and
    friends are not peers."""
    assert validate_target(" http://10.0.0.5:8081/ ") == "http://10.0.0.5:8081"
    for bad in ("ftp://10.0.0.5", "http://", "not a url"):
        with pytest.raises(UnusableTarget):
            validate_target(bad)


def test_a_failed_leg_carries_the_error_and_no_millisecond_figure():
    async def refused(url, timeout):
        raise RuntimeError("connection refused")

    leg = _run(dial(refused, "coordinator", "spark-02", "http://10.0.0.12:8081"))
    assert leg.ok is False
    assert leg.ms is None
    assert "connection refused" in leg.error


def test_a_successful_leg_records_who_answered():
    """Reaching *something* at an address is not the same as reaching the node
    you meant, so the name the far side gives is kept."""

    async def answers(url, timeout):
        return {"status": "ok", "node_id": "spark-02"}

    leg = _run(dial(answers, "coordinator", "spark-02", "http://10.0.0.12:8081"))
    assert leg.ok is True
    assert leg.answered_as == "spark-02"
    assert leg.ms is not None


def test_the_verdict_names_the_direction_that_failed():
    good = ReachLeg(source="coordinator", target="spark-02", url="u", ok=True, ms=1.0)
    bad = ReachLeg(source="spark-02", target="spark-01", url="u", ok=False, error="x")
    ok, sentence = summarize([good, bad])
    assert ok is False
    assert "spark-02 → spark-01" in sentence
    assert summarize([good, good])[0] is True
    assert summarize([bad, bad]) == (False, "Neither direction answered.")


# ---------------------------------------------------------------------------
# the reach routes
# ---------------------------------------------------------------------------


def test_reach_route_returns_every_leg():
    registry = NamedRegistry()
    a, b = [n.profile.node_id for n in registry.list_nodes()[:2]]
    with TestClient(app(registry)) as client:
        reply = client.post("/api/links/reach", json={"a": a, "b": b})
    body = reply.json()
    assert reply.status_code == 200
    assert body["ok"] is True
    assert body["legs"][0]["target"] == b


def test_reach_route_404s_for_a_node_that_is_not_a_member():
    """A typo comes back as a typo, not as an unreachable machine."""
    with TestClient(app()) as client:
        reply = client.post("/api/links/reach", json={"a": "spark-99", "b": "spark-98"})
    assert reply.status_code == 404
    assert reply.json()["error"]["code"] == "node_not_found"


def test_reach_route_refuses_a_body_without_a_pair():
    with TestClient(app()) as client:
        assert client.post("/api/links/reach", json={"a": "spark-01"}).status_code == 400


def test_a_registry_that_cannot_check_reachability_says_so():
    with TestClient(create_app(GatewayDeps(registry=StubRegistry()))) as client:
        reply = client.post("/api/links/reach", json={"a": "spark-01", "b": "spark-02"})
    assert reply.status_code == 501


# ---------------------------------------------------------------------------
# the agent route the coordinator asks through
# ---------------------------------------------------------------------------


def test_agent_reach_is_token_gated_before_the_body_is_read():
    """Uncredentialed, this route is a port scanner that runs inside the
    operator's network and reports its findings."""
    client = TestClient(create_agent_app(NodeAgent(SPARK_01, token="right")))
    body = {"url": "http://10.0.0.12:8081"}
    assert client.post("/agent/reach", json=body).status_code == 403
    assert (
        client.post("/agent/reach", json=body, headers={"X-Derate-Token": "wrong"}).status_code
        == 403
    )


def test_agent_reach_rejects_an_address_it_will_not_dial():
    client = TestClient(create_agent_app(NodeAgent(SPARK_01, token="t")))
    for bad in ({"url": "ftp://10.0.0.12"}, {"url": ""}, {}):
        reply = client.post("/agent/reach", json=bad, headers={"X-Derate-Token": "t"})
        assert reply.status_code == 400


def test_agent_reach_reports_what_it_found_without_naming_the_target():
    """The agent was handed an address, not a node_id. Which node that address
    was supposed to be is the coordinator's knowledge, and it fills it in."""
    agent = NodeAgent(SPARK_01, token="t")

    class Answers:
        async def get_json(self, url, timeout):
            return {"status": "ok", "node_id": "spark-02"}

    leg = _run(agent.reach_payload("http://10.0.0.12:8081/", client=Answers()))
    assert leg["ok"] is True
    assert leg["source"] == "spark-01"
    assert leg["target"] == ""
    assert leg["answered_as"] == "spark-02"


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_a_direction_that_could_not_be_tested_is_neither_a_pass_nor_a_failure():
    """The third outcome. A worker running an older build has no /agent/reach,
    so the coordinator cannot ask it to dial back -- that is not the worker
    failing to answer, and it is not a clean bill of health either."""
    good = ReachLeg(source="coordinator", target="spark-02", url="u", ok=True, ms=1.0)
    unchecked = unknown_leg("spark-02", "spark-01", "u", why="never tested", error="404")

    ok, sentence = summarize([good, unchecked])

    # Nothing that was tested came back bad...
    assert ok is True
    # ...but the sentence must never claim "both ways" for a direction nobody
    # dialled.
    assert "both ways" not in sentence
    assert "spark-02 → spark-01 could not be checked" in sentence


def test_an_unchecked_direction_does_not_hide_a_real_failure():
    bad = ReachLeg(source="coordinator", target="spark-02", url="u", ok=False, error="x")
    unchecked = unknown_leg("spark-02", "spark-01", "u", why="never tested", error="404")

    ok, sentence = summarize([bad, unchecked])

    assert ok is False
    assert "coordinator → spark-02 did not answer" in sentence
    assert "spark-02 → spark-01 could not be checked" in sentence


def test_reaching_two_workers_from_the_coordinator_is_not_reaching_each_other():
    """The trap this guards: a check between two workers dials each of them
    from the coordinator, both answer, and a naive count prints "reachable both
    ways" over two directions nobody tested."""
    from_coordinator = [
        ReachLeg(source="coordinator", target="w1", url="u", ok=True, ms=1.0, pair=False),
        ReachLeg(source="coordinator", target="w2", url="u", ok=True, ms=1.0, pair=False),
    ]
    untested = [
        unknown_leg("w1", "w2", "u", why="never tested", error="404"),
        unknown_leg("w2", "w1", "u", why="never tested", error="404"),
    ]

    ok, sentence = summarize([*from_coordinator, *untested])

    assert ok is True
    assert "both ways" not in sentence
    assert "No direction between them could be tested" in sentence
    assert "does not establish that they can reach each other" in sentence
