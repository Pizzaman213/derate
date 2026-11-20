"""Per-node agent tokens, refusal records and the agent's LAN surface.

Plan steps 1.5, 3.4 and 4.6. Before this one fleet-wide bearer -- the cluster
token -- opened every agent's kill, cache delete and journal routes, crossed
the LAN in clear to every member on every pull, a non-ASCII header was a 500,
a refusal left no trace, an agent-direct kill or delete left no record at
all, and any LAN host could read a node's build, storage, telemetry and cached
models.
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from control_plane.contracts import DeviceClass, NodeProfile
from control_plane.registry.agent import NodeAgent, create_agent_app
from control_plane.telemetry.journal import Journal
from tests.unit.test_registry import FakeClient, make_profile, make_registry, run

_identity = pytest.importorskip(
    "control_plane.registry.identity",
    reason="needs agent_token from the registry branch",
)
if not hasattr(_identity, "agent_token"):
    pytest.skip("needs agent_token from the registry branch", allow_module_level=True)
agent_token = _identity.agent_token

CLUSTER = "cluster-secret"
NODE_ID = "spark-02"
AGENT = agent_token(CLUSTER, NODE_ID)


def profile() -> NodeProfile:
    return NodeProfile(
        node_id=NODE_ID,
        hostname=NODE_ID,
        address="10.0.0.12",
        device_class=DeviceClass.GB10,
        gpu_name="GB10",
        gpu_count=1,
        total_memory=128 * 1024**3,
        addressable_memory=96 * 1024**3,
        memory_bandwidth_gbps=273.0,
        compute_capability="12.1",
        driver_version="580",
    )


@pytest.fixture()
def journal(tmp_path):
    j = Journal(tmp_path / "j.db", node_id=NODE_ID)
    j.start()
    yield j
    j.close()


def _agent_events(journal: Journal) -> list[dict]:
    assert journal.sync(5.0)
    rows = journal.read(0, 5000)["rows"]
    bodies = [json.loads(r["body"]) for r in rows if r["kind"] == "event"]
    return [b for b in bodies if b.get("source") == "agent"]


def _client(journal=None, token=AGENT, **kw) -> TestClient:
    kwargs = {"token": token}
    if journal is not None:
        kwargs["sink"] = journal
    return TestClient(create_agent_app(NodeAgent(profile(), **kwargs)), **kw)


def test_the_agent_token_is_per_node_and_not_the_cluster_token():
    assert agent_token(CLUSTER, "spark-02") != agent_token(CLUSTER, "spark-03")
    assert agent_token(CLUSTER, "spark-02") != CLUSTER
    assert agent_token(CLUSTER, "spark-02") == AGENT  # derived, deterministic


def test_the_cluster_token_is_refused_and_the_refusal_journalled(journal, caplog):
    client = _client(journal, client=("10.0.0.66", 4242))
    with caplog.at_level(logging.WARNING, logger="control_plane.registry.agent"):
        res = client.get("/agent/journal", params={"since": 0},
                         headers={"X-Derate-Token": CLUSTER})
    assert res.status_code == 403
    assert any(
        r.getMessage() == "agent refused GET /agent/journal from 10.0.0.66"
        for r in caplog.records
    )
    refused = [e for e in _agent_events(journal) if e["type"] == "agent_refused"]
    assert len(refused) == 1
    assert refused[0]["peer"] == "10.0.0.66"
    assert refused[0]["path"] == "/agent/journal"
    assert refused[0]["node_id"] == NODE_ID


def test_the_agent_token_is_accepted(journal):
    client = _client(journal)
    res = client.get("/agent/journal", params={"since": 0}, headers={"X-Derate-Token": AGENT})
    assert res.status_code == 200
    assert "rows" in res.json()


def test_a_non_ascii_token_is_a_refusal_not_a_500(journal):
    client = _client(journal)
    res = client.get("/agent/journal", headers={"X-Derate-Token": "é".encode("latin-1")})
    assert res.status_code == 403


def test_a_refusal_flood_is_journalled_once_a_minute_per_peer(journal, caplog):
    client = _client(journal, client=("10.0.0.66", 4242))
    with caplog.at_level(logging.WARNING, logger="control_plane.registry.agent"):
        for _ in range(50):
            assert client.get("/agent/processes", headers={"X-Derate-Token": "x"}).status_code == 403
    refused = [e for e in _agent_events(journal) if e["type"] == "agent_refused"]
    assert len(refused) == 1, "a LAN host must not be able to fill the journal"
    # N-12: the WARNING joins the same per-peer, per-minute fold.
    assert sum("agent refused" in r.getMessage() for r in caplog.records) == 1


# ---------------------------------------------------------------------------
# 4.6: what the LAN can read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/agent/telemetry", "/agent/storage", "/agent/models/cache"])
def test_the_inventory_reads_need_the_agent_token(path, tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_HF_CACHE", str(tmp_path))
    client = TestClient(create_agent_app(NodeAgent(profile(), token=AGENT, data_root=tmp_path)))
    assert client.get(path).status_code == 403
    assert client.get(path, headers={"X-Derate-Token": CLUSTER}).status_code == 403
    assert client.get(path, headers={"X-Derate-Token": AGENT}).status_code == 200


def test_health_and_profile_stay_open_but_say_no_build_without_the_token():
    client = _client()
    health = client.get("/agent/health")
    prof = client.get("/agent/profile")
    assert health.status_code == prof.status_code == 200
    assert "build" not in health.json() and "build" not in prof.json()
    assert health.json()["node_id"] == NODE_ID and prof.json()["node_id"] == NODE_ID
    headers = {"X-Derate-Token": AGENT}
    assert "build" in client.get("/agent/health", headers=headers).json()
    assert "build" in client.get("/agent/profile", headers=headers).json()


# ---------------------------------------------------------------------------
# 3.4: agent-direct actions leave rows
# ---------------------------------------------------------------------------


def test_a_cache_delete_is_journalled_with_the_peer(journal, tmp_path, monkeypatch):
    from tests.unit.test_modelcache import make_repo

    monkeypatch.setenv("DERATE_HF_CACHE", str(tmp_path))
    make_repo(tmp_path, "models--org--m", blob_sizes=(1024,))
    client = _client(journal, client=("10.0.0.11", 5000))
    res = client.delete("/agent/models/cache/models--org--m", headers={"X-Derate-Token": AGENT})
    assert res.status_code == 200
    deleted = [e for e in _agent_events(journal) if e["type"] == "cache_deleted"]
    assert deleted == [
        {
            "source": "agent",
            "ts": deleted[0]["ts"],
            "node_id": NODE_ID,
            "type": "cache_deleted",
            "peer": "10.0.0.11",
            "path": "/agent/models/cache/models--org--m",
            "folder": "models--org--m",
            "bytes_freed": 1024,
        }
    ]


def test_a_kill_is_journalled_with_the_peer(journal, monkeypatch):
    from control_plane.registry import agent as agent_mod

    class Result:
        def as_dict(self):
            return {"pid": 4242, "name": "vllm", "signal_sent": "SIGTERM", "exited": True,
                    "gpu_memory_before": 1, "gpu_memory_reclaimed": 1, "detail": "ok"}

    async def fake_kill(pid):
        return Result()

    monkeypatch.setattr(agent_mod, "kill_gpu_process", fake_kill)
    client = _client(journal, client=("10.0.0.11", 5000))
    res = client.post("/agent/processes/4242/kill", headers={"X-Derate-Token": AGENT})
    assert res.status_code == 200
    killed = [e for e in _agent_events(journal) if e["type"] == "process_killed"]
    assert len(killed) == 1
    assert killed[0]["peer"] == "10.0.0.11"
    assert killed[0]["pid"] == 4242
    assert killed[0]["name"] == "vllm"


def test_a_journal_cursor_past_the_head_is_clamped_and_journalled(journal):
    for i in range(3):
        journal.append("sample", {"ts": 1000.0 + i})
    assert journal.sync(5.0)
    head = journal.stats()["head"]
    client = _client(journal)
    res = client.get("/agent/journal", params={"since": head + 10_000},
                     headers={"X-Derate-Token": AGENT})
    assert res.status_code == 200
    # The ack never runs past what was written, so the rows appended after
    # it stay until a real cursor collects them.
    assert journal.stats()["shipped_hwm"] <= head
    clamped = [e for e in _agent_events(journal) if e["type"] == "journal_cursor_clamped"]
    assert clamped and clamped[0]["since"] == head + 10_000


def test_journal_read_clamps_since_but_not_after_a_full_trim(journal):
    """The ceiling is the last sequence number ever handed out: an honest
    cursor at the end of a fully trimmed journal is not a cursor from the
    future, and must not be reported as one."""
    for i in range(3):
        journal.append("sample", {"ts": 1000.0 + i})
    assert journal.sync(5.0)
    head = journal.stats()["head"]
    assert "clamped_from" not in journal.read(head, 10)
    payload = journal.read(head + 5, 10)
    assert payload["clamped_from"] == head + 5
    assert payload["next"] == head


# ---------------------------------------------------------------------------
# the coordinator presents the derived token, never the cluster token
# ---------------------------------------------------------------------------


def test_the_coordinator_presents_each_node_its_own_token(tmp_path):
    seen: list[tuple[str, dict | None]] = []

    class Recording(FakeClient):
        async def get_json(self, url, timeout, headers=None):
            seen.append((url, headers))
            return await super().get_json(url, timeout, headers)

    client = Recording()
    client.serve("http://10.0.0.12:8081", make_profile("spark-02", address="10.0.0.12"))
    registry = make_registry(
        tmp_path, client=client, local=make_profile("spark-01", address="10.0.0.11"),
        token=CLUSTER,
    )
    run(registry.add_node("10.0.0.12"))
    run(registry.check_health("spark-02"))
    health = [h for u, h in seen if u.endswith("/agent/health")]
    own = registry.agent_token("spark-02")
    assert health and all(h == {"X-Derate-Token": own} for h in health)
    # Random and per member (N-2): nothing derivable from the cluster token
    # every worker holds.
    assert own not in (AGENT, CLUSTER)
    assert own != registry.agent_token("spark-01")


def test_forensics_reads_agent_events_as_their_own_source():
    import sqlite3

    from control_plane.forensics import agent_events

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE events(node_id TEXT, seq INTEGER, ts REAL, source TEXT, type TEXT, "
        "deployment_id TEXT, served_name TEXT, body TEXT)"
    )
    body = {"source": "agent", "type": "process_killed", "peer": "10.0.0.66",
            "path": "/agent/processes/7/kill", "pid": 7, "name": "vllm"}
    conn.execute(
        "INSERT INTO events VALUES('spark-02', 1, 1000.0, 'agent', 'process_killed', "
        "NULL, NULL, ?)", (json.dumps(body),),
    )
    conn.execute(
        "INSERT INTO events VALUES('spark-02', 2, 1001.0, 'shell', 'shell_opened', "
        "NULL, NULL, '{}')"
    )
    events = list(agent_events(conn))
    assert len(events) == 1
    e = events[0]
    assert (e.source, e.who, e.ip) == ("agent", "node:spark-02", "10.0.0.66")
    assert "process_killed" in e.what and "pid=7" in e.what


# ---------------------------------------------------------------------------
# second pass: a worker's cluster token no longer opens another member (N-2),
# reads by anyone but the coordinator leave a row (N-11), the refusal WARNING
# is folded (N-12)
# ---------------------------------------------------------------------------


def test_the_derived_token_is_refused_once_migrated(tmp_path, journal):
    """N-2: spark-03 holds the cluster token and derives spark-02's old agent
    token from it; once spark-02 has migrated that opens nothing."""
    from control_plane.registry import joinproof
    from control_plane.registry.roster import save_roster
    from control_plane.registry.serde import profile_to_dict
    import time

    client = FakeClient()
    url = "http://10.0.0.12:8081"
    client.serve(url, profile())
    save_roster(tmp_path, {NODE_ID: {"profile": profile_to_dict(profile()), "agent_url": url}}, {})
    registry = make_registry(
        tmp_path, client=client, local=make_profile("spark-01", address="10.0.0.11"),
        token=CLUSTER,
    )
    answer = run(registry.handle_join(
        joinproof.make(CLUSTER, NODE_ID, url, time.time()), profile(), url, peer="10.0.0.12"
    ))
    delivered = answer["agent_token"]
    agent = NodeAgent(profile(), token=delivered, sink=journal)
    http = TestClient(create_agent_app(agent), client=("10.0.0.13", 4000))
    for path in ("/agent/journal", "/agent/processes"):
        assert http.get(path, headers={"X-Derate-Token": AGENT}).status_code == 403
    assert http.get("/agent/telemetry", headers={"X-Derate-Token": delivered}).status_code == 200
    assert registry.agent_token(NODE_ID) == delivered != AGENT


def test_a_read_by_anyone_but_the_coordinator_is_journalled(journal):
    """N-11: the coordinator reading its member is routine; the same token
    presented from another address is somebody holding a stolen credential."""
    agent = NodeAgent(profile(), token=AGENT, sink=journal)
    agent.set_coordinator("10.0.0.11")
    app = create_agent_app(agent)
    coordinator = TestClient(app, client=("10.0.0.11", 4000))
    stranger = TestClient(app, client=("10.0.0.66", 4000))
    headers = {"X-Derate-Token": AGENT}
    for _ in range(3):
        assert coordinator.get("/agent/telemetry", headers=headers).status_code == 200
        assert stranger.get("/agent/telemetry", headers=headers).status_code == 200
    assert stranger.get("/agent/journal", headers=headers).status_code == 200
    reads = [e for e in _agent_events(journal) if e["type"] == "agent_read"]
    assert [(e["peer"], e["path"]) for e in reads] == [
        ("10.0.0.66", "/agent/telemetry"),
        ("10.0.0.66", "/agent/journal"),
    ]
    assert reads[0]["method"] == "GET"


def test_a_refusal_warning_carries_the_count_of_the_window_before(journal, caplog):
    clock = [1000.0]
    agent = NodeAgent(profile(), token=AGENT, sink=journal, clock=lambda: clock[0])
    http = TestClient(create_agent_app(agent), client=("10.0.0.66", 4242))
    with caplog.at_level(logging.WARNING, logger="control_plane.registry.agent"):
        for _ in range(5):
            http.get("/agent/processes", headers={"X-Derate-Token": "x"})
        clock[0] += 61
        http.get("/agent/processes", headers={"X-Derate-Token": "x"})
    messages = [r.getMessage() for r in caplog.records if "agent refused" in r.getMessage()]
    assert len(messages) == 2
    assert "4 more" in messages[1]


def test_a_runtime_api_key_never_reaches_the_process_list(tmp_path, monkeypatch):
    """N-2: SGLang's ``--api-key`` is the bearer that opens its port directly;
    the agent's process list is a token holder's read and must not hand it on."""
    from control_plane.registry import telemetry as reg_telemetry

    argv = ["python3", "-m", "sglang.launch_server", "--api-key", "s3cr3t-bearer",
            "--port", "8000"]
    proc = tmp_path / "4242"
    proc.mkdir()
    (proc / "cmdline").write_bytes("\x00".join(argv).encode() + b"\x00")
    real_path = reg_telemetry.Path
    monkeypatch.setattr(
        reg_telemetry, "Path",
        lambda p: real_path(str(p).replace("/proc", str(tmp_path), 1)),
    )
    line = reg_telemetry._proc_cmdline(4242)
    assert "s3cr3t-bearer" not in line
    assert "--api-key *** --port 8000" in line
    assert reg_telemetry.redact_api_key(["x", "--api-key=abc"]) == "x --api-key=***"
    assert "abc" not in reg_telemetry.redact_api_key(["sh", "-c", "serve --api-key abc"])
