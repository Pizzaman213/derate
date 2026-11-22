"""Background copies to other nodes (control_plane/transfer/replication.py).

The rule under test above all others: nothing is copied or deleted without an
approval, and an approval covers exactly the deletions the operator was shown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from control_plane.contracts import DeploymentState as S
from control_plane.contracts.deployment import Lifecycle
from control_plane.transfer.replication import (
    NodeView,
    PlanChanged,
    ProposalNotFound,
    ReplicaStore,
    ReplicationMonitor,
    Source,
    Wanted,
    node_floor,
    pair_key,
    plan_replication,
    wanted_pairs,
)

GIB = 1 << 30
COORD, PEER, OTHER = "spark-26af", "spark-4d38", "box-3"
F4B = "models--Qwen--Qwen3-4B"
F30B = "models--Qwen--Qwen3-30B-A3B"


def dep(model, nodes, *, name="m", state=S.STOPPED, idle=None, boot=False, started=1.0):
    return SimpleNamespace(
        served_name=name, shape=SimpleNamespace(model_id=model),
        plan=SimpleNamespace(node_ids=list(nodes)), state=state, started_at=started,
        lifecycle=Lifecycle(idle_unload_s=idle, start_at_boot=boot), deployment_id=f"d-{name}",
    )


def view(node=PEER, *, free=500 * GIB, cache=None, healthy=True, supported=True, memory=128 * GIB,
         total=None):
    return NodeView(node, healthy, memory, supported, {} if cache is None else cache, free,
                    total=total)


# -- what is wanted ----------------------------------------------------------


def test_deployment_plans_come_first_and_pins_cover_every_other_node():
    deps = [
        dep("Qwen/Qwen3-4B", [COORD, PEER], name="small", idle=600),
        dep("Qwen/Qwen3-30B-A3B", [COORD, PEER], name="big"),  # no lifecycle: not wanted
        dep("org/repo/file.gguf", [COORD, PEER], name="gguf", boot=True),  # not a hub repo
    ]
    got = wanted_pairs(deps, ["Qwen/Qwen3-4B", "Qwen/Qwen3-30B-A3B"], [PEER, OTHER], COORD)
    assert [(w.folder, w.node_id) for w in got] == [(F4B, PEER), (F4B, OTHER), (F30B, PEER), (F30B, OTHER)]
    assert got[0].reasons == ["deployment small unloads when idle and relaunches on spark-4d38", "pinned"]


def test_only_the_newest_record_of_a_served_name_counts():
    old = dep("Qwen/Qwen3-4B", [COORD, PEER], name="m", boot=True, started=1.0)
    new = dep("Qwen/Qwen3-4B", [COORD, OTHER], name="m", started=2.0)  # no lifecycle now
    assert wanted_pairs([old, new], [], [PEER, OTHER], COORD) == []


# -- the plan ----------------------------------------------------------------


def _plan(nodes, *, src=Source("complete", 8 * GIB), replicas=None, in_use=(), dismissed=None, folder=F4B):
    return plan_replication(
        [Wanted(folder, "Qwen/Qwen3-4B", PEER, ["pinned"])], {folder: src},
        {n.node_id: n for n in nodes}, replicas or {}, set(in_use), dismissed or {},
    )


@pytest.mark.parametrize(
    "src, node, fragment",
    [
        (Source("absent"), view(), "is not in the coordinator's cache"),
        (Source("incomplete"), view(), "still downloading on the coordinator"),
        (Source("complete", 8 * GIB), view(healthy=False), "not answering health checks"),
        (Source("complete", 8 * GIB), view(supported=False), "older build"),
        (Source("complete", 300 * GIB), view(memory=128 * GIB), "could never be served there"),
        (Source("complete", 8 * GIB), view(free=50 * GIB), "below its 100.0 GiB free-space floor"),
    ],
)
def test_each_reason_not_to_propose_is_one_sentence(src, node, fragment):
    proposals, skipped = _plan([node], src=src)
    assert proposals == []
    assert len(skipped) == 1 and fragment in skipped[0].sentence


def test_a_model_already_there_is_neither_proposed_nor_skipped():
    assert _plan([view(cache={F4B: 8 * GIB})]) == ([], [])


def test_a_partial_copy_of_the_same_revision_is_proposed_for_the_rest_only():
    src = Source("complete", 8 * GIB, "c" * 40)
    same = NodeView(PEER, True, 128 * GIB, True, {F4B: 3 * GIB}, 500 * GIB, revisions={F4B: ["c" * 40]})
    proposals, _ = _plan([same], src=src)
    assert proposals[0].bytes == 5 * GIB and proposals[0].evict == []
    # Whose commit the 3 GiB belong to is unknown: nothing is assumed shared.
    proposals, _ = _plan([view(cache={F4B: 3 * GIB})], src=src)
    assert proposals[0].bytes == 8 * GIB


def test_eviction_takes_only_derates_unused_copies_oldest_first():
    cache = {"models--a--old": 40 * GIB, "models--b--newer": 40 * GIB, "models--c--busy": 90 * GIB,
             "models--mine--download": 400 * GIB}
    replicas = {
        pair_key("models--a--old", PEER): {"folder": "models--a--old", "node_id": PEER, "bytes": 40 * GIB,
                                           "placed_at": 1.0, "used_at": None},
        pair_key("models--b--newer", PEER): {"folder": "models--b--newer", "node_id": PEER, "bytes": 40 * GIB,
                                             "placed_at": 2.0, "used_at": 5.0},
        pair_key("models--c--busy", PEER): {"folder": "models--c--busy", "node_id": PEER, "bytes": 90 * GIB,
                                            "placed_at": 0.5, "used_at": None},
    }
    # 8 GiB to send into 70 GiB free: 30 GiB short of the floor -> one old copy is enough.
    proposals, _ = _plan([view(free=70 * GIB, cache=cache)], replicas=replicas,
                         in_use={("models--c--busy", PEER)})
    assert [e["folder"] for e in proposals[0].evict] == ["models--a--old"]
    # Short by more than derate's own unused copies hold: refused, never touching the rest.
    proposals, skipped = _plan([view(free=20 * GIB, cache=cache)], replicas=replicas,
                               in_use={("models--c--busy", PEER)})
    assert proposals == [] and "would free only 80.0 GiB more" in skipped[0].sentence


def test_a_dismissed_proposal_returns_only_when_the_source_changes():
    key = pair_key(F4B, PEER)
    _, skipped = _plan([view()], dismissed={key: 8 * GIB})  # the older int form still holds
    assert "You chose not now" in skipped[0].sentence
    proposals, _ = _plan([view()], src=Source("complete", 9 * GIB), dismissed={key: 8 * GIB})
    assert proposals and proposals[0].bytes == 9 * GIB
    rec = {"bytes": 8 * GIB, "revision": "b" * 40}
    _, skipped = _plan([view()], src=Source("complete", 8 * GIB, "b" * 40), dismissed={key: rec})
    assert "You chose not now" in skipped[0].sentence
    proposals, _ = _plan([view()], src=Source("complete", 8 * GIB, "c" * 40), dismissed={key: rec})
    assert proposals


# -- the monitor -------------------------------------------------------------


@dataclass
class FakeReplicator:
    local_node_id: str = COORD
    jobs: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)
    revision: str | None = None

    def start(self, folder, node_id, *, streams=8):
        self.calls.append(("start", folder, node_id))
        tid = f"tr-{len(self.jobs)}"
        self.jobs[tid] = {"transfer_id": tid, "status": "running", "total": None, "error": None,
                          "revision": self.revision}
        return dict(self.jobs[tid])

    def get(self, tid):
        return dict(self.jobs[tid]) if tid in self.jobs else None

    def cancel(self, tid):
        self.calls.append(("cancel", tid))
        return self.jobs.pop(tid, None) is not None

    def list(self):
        return [dict(j) for j in self.jobs.values()]


@dataclass
class FakeAgents:
    caches: dict
    free: dict
    total: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)
    revs: dict = field(default_factory=dict)

    def cache(self, node_id):
        return dict(self.caches[node_id])

    def revisions(self, node_id):
        return self.revs.get(node_id)

    landings: dict = field(default_factory=dict)  # (node, folder) -> answer; absent = older agent

    def capacity(self, node_id):
        free = self.free[node_id]
        return None if free is None else {"free": free, "total": self.total.get(node_id)}

    asked: list = field(default_factory=list)

    def landing(self, node_id, folder, nbytes=0):
        self.asked.append((node_id, folder, nbytes))
        return self.landings.get((node_id, folder))

    def delete(self, node_id, folder):
        self.calls.append(("delete", node_id, folder))
        self.caches[node_id].pop(folder, None)


def node_state(node_id, *, healthy=True, build="b1"):
    return SimpleNamespace(profile=SimpleNamespace(node_id=node_id, total_memory=128 * GIB),
                           healthy=healthy, memory_total=0, build=build)


@pytest.fixture()
def rig(tmp_path):
    hub = tmp_path / "hub"
    for folder, size in ((F4B, 8 << 20),):
        (hub / folder / "snapshots" / "abc").mkdir(parents=True)
        (hub / folder / "blobs").mkdir()
        (hub / folder / "blobs" / ("a" * 64)).write_bytes(b"\0" * size)
    deployments = SimpleNamespace(rows=[])
    deployments.list = lambda: list(deployments.rows)
    registry = SimpleNamespace(nodes=[node_state(COORD), node_state(PEER)])
    registry.list_nodes = lambda: list(registry.nodes)
    replicator = FakeReplicator()
    agents = FakeAgents(caches={PEER: {}}, free={PEER: 500 * GIB})
    pins = ["Qwen/Qwen3-4B"]
    store = ReplicaStore(tmp_path / "replicas.json")
    monitor = ReplicationMonitor(
        deployments=deployments, registry=registry, replicator=replicator, pins=lambda: pins,
        store=store, agents=agents, cache_dir=lambda: hub, clock=lambda: 100.0,
    )
    return SimpleNamespace(m=monitor, deps=deployments, reg=registry, rep=replicator, agents=agents,
                           store=store, pins=pins)


def test_a_proposal_waits_for_approval_and_nothing_runs_before_it(rig):
    for _ in range(3):
        rig.m.step()
    state = rig.m.state()
    assert [(p["folder"], p["node_id"], p["status"]) for p in state["proposals"]] == [
        (F4B, PEER, "awaiting_approval")
    ]
    assert rig.rep.calls == [] and rig.agents.calls == []
    assert rig.m.awaiting() == 1


def test_an_approval_must_name_the_deletions_it_was_shown(rig):
    rig.m.step()
    with pytest.raises(PlanChanged):
        rig.m.approve(F4B, PEER, ["models--x--y"])
    with pytest.raises(ProposalNotFound):
        rig.m.approve(F4B, OTHER, [])
    assert rig.m.approve(F4B, PEER, [])["status"] == "approved"
    rig.m.step()
    assert rig.rep.calls == [("start", F4B, PEER)]
    assert rig.m.state()["proposals"][0]["status"] == "copying"


def test_approved_deletions_run_before_the_copy_and_only_those(rig):
    rig.agents.caches[PEER] = {"models--a--old": 450 * GIB, "models--mine--x": 10 * GIB}
    rig.agents.free[PEER] = 60 * GIB
    rig.store.add_replica("models--a--old", PEER, 450 * GIB, 1.0)
    rig.m.step()
    p = rig.m.state()["proposals"][0]
    assert [e["folder"] for e in p["evict"]] == ["models--a--old"]
    rig.m.approve(F4B, PEER, ["models--a--old"])
    rig.m.step()
    assert rig.agents.calls == [("delete", PEER, "models--a--old")]
    assert rig.rep.calls == [("start", F4B, PEER)]
    assert pair_key("models--a--old", PEER) not in rig.store.snapshot()["replicas"]


def test_a_changed_deletion_list_withdraws_the_approval(rig):
    rig.m.step()
    rig.m.approve(F4B, PEER, [])
    rig.agents.caches[PEER] = {"models--a--old": 450 * GIB}
    rig.agents.free[PEER] = 60 * GIB
    rig.store.add_replica("models--a--old", PEER, 450 * GIB, 1.0)
    with rig.m._lock:  # stop it starting before the plan changes, as a launch would
        rig.deps.rows = [dep("other/model", [COORD], state=S.LAUNCHING)]
        rig.m.step()
    row = rig.m.state()["proposals"][0]
    assert row["status"] == "awaiting_approval" and "review it again" in row["last_error"]
    rig.deps.rows = []
    rig.m.step()
    assert rig.rep.calls == [] and rig.agents.calls == []


def test_nothing_starts_while_a_launch_is_in_progress_and_a_launch_preempts(rig):
    rig.m.step()
    rig.m.approve(F4B, PEER, [])
    rig.deps.rows = [dep("other/model", [COORD, PEER], state=S.LAUNCHING)]
    rig.m.step()
    assert rig.rep.calls == []
    rig.deps.rows = []
    rig.m.step()
    assert rig.rep.calls == [("start", F4B, PEER)]
    rig.deps.rows = [dep("other/model", [COORD, PEER], state=S.LAUNCHING)]
    rig.m.step()
    assert rig.rep.calls[-1] == ("cancel", "tr-0")
    # The approval stands: once the launch is over the copy runs again.
    rig.deps.rows = []
    rig.m.step()
    assert rig.rep.calls[-1] == ("start", F4B, PEER)


def test_a_launch_never_preempts_the_copy_it_needs(rig):
    rig.m.step()
    rig.m.approve(F4B, PEER, [])
    rig.m.step()
    rig.deps.rows = [dep("Qwen/Qwen3-4B", [COORD, PEER], state=S.LAUNCHING)]
    rig.m.step()
    assert ("cancel", "tr-0") not in rig.rep.calls


def test_a_finished_copy_is_recorded_and_a_failed_one_must_be_approved_again(rig):
    rig.m.step()
    rig.m.approve(F4B, PEER, [])
    rig.m.step()
    rig.rep.jobs["tr-0"].update(status="error", error="spark-4d38: the node refused a chunk")
    rig.m.step()
    row = rig.m.state()["proposals"][0]
    assert row["status"] == "awaiting_approval" and row["last_error"].endswith("refused a chunk")
    rig.m.approve(F4B, PEER, [])
    rig.m.step()
    rig.rep.jobs["tr-1"].update(status="done", total=123)
    rig.agents.caches[PEER] = {F4B: 8 << 20}
    rig.m.step()
    replica = rig.store.snapshot()["replicas"][pair_key(F4B, PEER)]
    assert replica["bytes"] == 8 << 20  # the folder's size, not the bytes that run sent
    assert rig.m.state()["proposals"] == []


def test_dismiss_and_an_old_build_and_a_vanished_copy(rig):
    rig.m.step()
    assert rig.m.dismiss(F4B, PEER) is True
    rig.m.step()
    assert rig.m.state()["proposals"] == []
    assert "You chose not now" in rig.m.state()["skipped"][0]["sentence"]

    rig.store.add_replica("models--z--gone", PEER, GIB, 1.0)
    rig.agents.free[PEER] = None  # the agent answers 404: built before copies existed
    rig.m.step()
    assert "older build" in rig.m.state()["skipped"][0]["sentence"]
    rig.agents.free[PEER] = 500 * GIB
    rig.reg.nodes = [node_state(COORD), node_state(PEER, build="b2")]  # upgraded
    rig.m.step()
    assert pair_key("models--z--gone", PEER) not in rig.store.snapshot()["replicas"]


# -- routes --------------------------------------------------------------------


def test_routes_approve_dismiss_and_activity_count(rig):
    from fastapi.testclient import TestClient

    from control_plane.gateway import create_app
    from tests.unit.test_gateway import GIB as _G, _pull_setup

    deps, _, _ = _pull_setup(size=0, free_bytes=_G)
    app = create_app(deps)
    app.state.ctx.replication = rig.m
    rig.m._interval_s = 3600  # the test drives it
    with TestClient(app) as client:
        rig.m.step()
        state = client.get("/api/replication").json()
        assert state["proposals"][0]["status"] == "awaiting_approval"
        assert client.get("/api/activity").json()["replication"] == {"awaiting": 1}
        bad = client.post("/api/replication/approve",
                          json={"folder": F4B, "node_id": PEER, "evict": ["models--x--y"]})
        assert bad.status_code == 409 and bad.json()["error"]["code"] == "replication_plan_changed"
        ok = client.post("/api/replication/approve", json={"folder": F4B, "node_id": PEER, "evict": []})
        assert ok.status_code == 200 and ok.json()["status"] == "approved"
        gone = client.post("/api/replication/dismiss", json={"folder": F4B, "node_id": OTHER})
        assert gone.status_code == 404
        assert client.post("/api/replication/dismiss", json={"folder": F4B, "node_id": PEER}).status_code == 204


def test_a_gateway_without_copies_answers_503():
    from fastapi.testclient import TestClient

    from control_plane.gateway import create_app
    from tests.unit.test_gateway import GIB as _G, _pull_setup

    deps, _, _ = _pull_setup(size=0, free_bytes=_G)
    with TestClient(create_app(deps)) as client:
        r = client.get("/api/replication")
        assert r.status_code == 503 and r.json()["error"]["code"] == "replication_unavailable"
        assert "replication" not in client.get("/api/activity").json()


# -- found by the live run on spark-4d38 ----------------------------------------


def test_a_copy_that_is_still_wanted_is_never_deleted_to_make_room():
    """Deleting a pinned 4B copy to fit a pinned 30B proposed the 4B back on
    the next pass. Only copies nothing asks for any more may go."""
    replicas = {pair_key(F4B, PEER): {"folder": F4B, "node_id": PEER, "bytes": 8 * GIB,
                                      "placed_at": 1.0, "used_at": None}}
    wanted = [Wanted(F4B, "Qwen/Qwen3-4B", PEER, ["pinned"]),
              Wanted(F30B, "Qwen/Qwen3-30B-A3B", PEER, ["pinned"])]
    proposals, skipped = plan_replication(
        wanted, {F4B: Source("complete", 8 * GIB), F30B: Source("complete", 57 * GIB)},
        {PEER: view(free=150 * GIB, cache={F4B: 8 * GIB})}, replicas, set(), {},
    )
    assert proposals == []
    assert "would free only 0.0 GiB more" in skipped[0].sentence


def test_a_paused_copy_resumes_on_its_approval_when_its_deletion_is_already_done(rig):
    rig.agents.caches[PEER] = {"models--a--old": 450 * GIB}
    rig.agents.free[PEER] = 60 * GIB
    rig.store.add_replica("models--a--old", PEER, 450 * GIB, 1.0)
    rig.m.step()
    rig.m.approve(F4B, PEER, ["models--a--old"])
    rig.m.step()
    rig.agents.free[PEER] = 510 * GIB  # the deletion freed the room
    rig.deps.rows = [dep("other/model", [COORD, PEER], state=S.LAUNCHING)]
    rig.m.step()  # paused
    rig.deps.rows = []
    rig.m.step()
    assert rig.rep.calls[-1] == ("start", F4B, PEER)
    assert rig.m.state()["proposals"][0]["last_error"] is None


def test_a_copy_is_derates_from_the_moment_it_starts(rig):
    rig.m.step()
    rig.m.approve(F4B, PEER, [])
    rig.m.step()
    started = rig.store.snapshot()["replicas"][pair_key(F4B, PEER)]
    assert started["complete"] is False
    rig.rep.jobs["tr-0"].update(status="done", total=1)
    rig.agents.caches[PEER] = {F4B: 8 << 20}
    rig.m.step()
    assert rig.store.snapshot()["replicas"][pair_key(F4B, PEER)]["complete"] is True


def test_a_copy_whose_blobs_have_all_landed_stays_listed_until_its_job_ends(rig):
    """Blobs are placed as they verify, so the node lists the whole folder
    before the job commits. The row stays 'copying' and the record takes the
    folder's size, not the 30.8 of 56.9 GiB the resumed run happened to send."""
    rig.m.step()
    rig.m.approve(F4B, PEER, [])
    rig.m.step()
    rig.agents.caches[PEER] = {F4B: 8 << 20}  # everything placed; job still verifying
    rig.rep.jobs["tr-0"].update(status="verifying")
    rig.m.step()
    assert rig.m.state()["proposals"][0]["status"] == "copying"
    rig.rep.jobs["tr-0"].update(status="done", total=3)
    rig.m.step()
    assert rig.store.snapshot()["replicas"][pair_key(F4B, PEER)]["bytes"] == 8 << 20


# -- the free-space floor scales with the disk -----------------------------------


@pytest.mark.parametrize(
    ("total", "floor"),
    [(256 * GIB, int(256 * GIB * 0.2)), (4 * 10**12, 100 * GIB), (None, 100 * GIB), (0, 100 * GIB)],
)
def test_the_floor_is_a_fifth_of_the_disk_capped_at_100_gib(total, floor):
    assert node_floor(total) == floor


def test_a_small_disk_is_judged_against_its_own_floor_not_the_cap():
    # 256 GiB disk, 70 GiB free, 8 GiB to send: 62 GiB left clears 51.2, not 100.
    proposals, skipped = _plan([view(free=70 * GIB, total=256 * GIB)])
    assert skipped == []
    assert proposals[0].floor_bytes == int(256 * GIB * 0.2)
    # 55 GiB free leaves 47: below its own floor, and the sentence names that floor.
    proposals, skipped = _plan([view(free=55 * GIB, total=256 * GIB)])
    assert proposals == []
    assert "below its 51.2 GiB free-space floor" in skipped[0].sentence
    assert skipped[0].floor_bytes == int(256 * GIB * 0.2)


def test_a_large_disk_keeps_the_100_gib_floor():
    proposals, skipped = _plan([view(free=105 * GIB, total=4 * 10**12)])
    assert proposals == [] and skipped[0].floor_bytes == 100 * GIB


def test_the_monitor_threads_the_reported_disk_size_into_each_proposal(rig):
    rig.agents.free[PEER] = 70 * GIB
    rig.agents.total[PEER] = 256 * GIB
    rig.m.step()
    state = rig.m.state()
    assert state["floor_bytes"] == 100 * GIB  # the cap
    assert [p["floor_bytes"] for p in state["proposals"]] == [int(256 * GIB * 0.2)]


# -- revisions -----------------------------------------------------------------


NEW, OLD = "c" * 40, "b" * 40


def test_a_node_without_the_coordinators_commit_is_proposed_again():
    src = Source("complete", 8 * GIB, NEW)
    stale = NodeView(PEER, True, 128 * GIB, True, {F4B: 8 * GIB}, 500 * GIB, revisions={F4B: [OLD]})
    proposals, skipped = _plan([stale], src=src)
    assert skipped == [] and len(proposals) == 1
    assert proposals[0].reasons[-1] == "the coordinator has a newer revision"
    # Another commit's blobs may share nothing with this one and stay put.
    assert proposals[0].bytes == 8 * GIB
    fresh = NodeView(PEER, True, 128 * GIB, True, {F4B: 8 * GIB}, 500 * GIB,
                     revisions={F4B: [OLD, NEW]})
    assert _plan([fresh], src=src) == ([], [])
    # Unknown on either side is never stale.
    assert _plan([view(cache={F4B: 8 * GIB})], src=src) == ([], [])
    assert _plan([stale], src=Source("complete", 8 * GIB)) == ([], [])


def test_a_replica_recorded_at_another_commit_is_stale():
    src = Source("complete", 8 * GIB, NEW)
    key = pair_key(F4B, PEER)
    rec = {"folder": F4B, "node_id": PEER, "bytes": 8 * GIB, "placed_at": 1.0, "used_at": None,
           "complete": True, "revision": OLD}
    proposals, _ = _plan([view(cache={F4B: 8 * GIB})], src=src, replicas={key: rec})
    assert proposals and "newer revision" in proposals[0].reasons[-1]
    assert _plan([view(cache={F4B: 8 * GIB})], src=src, replicas={key: {**rec, "revision": NEW}}) == ([], [])


def test_the_store_keeps_the_revision_a_copy_started_at(tmp_path):
    store = ReplicaStore(tmp_path / "r.json")
    store.add_replica(F4B, PEER, GIB, 1.0, complete=False, revision=NEW)
    store.add_replica(F4B, PEER, 2 * GIB, 2.0, complete=True)  # None keeps the recorded one
    assert store.snapshot()["replicas"][pair_key(F4B, PEER)]["revision"] == NEW


def test_the_monitor_records_and_reproposes_on_a_revision_change(rig):
    hub = rig.m._cache_dir()
    (hub / F4B / "refs").mkdir()
    (hub / F4B / "refs" / "main").write_text("abc")
    rig.rep.revision = "abc"
    rig.m.step()
    rig.m.approve(F4B, PEER, [])
    rig.m.step()
    assert rig.store.snapshot()["replicas"][pair_key(F4B, PEER)]["revision"] == "abc"
    rig.rep.jobs["tr-0"].update(status="done", total=8 << 20)
    rig.agents.caches[PEER] = {F4B: 8 << 20}
    rig.agents.revs[PEER] = {F4B: ["abc"]}
    rig.m.step()
    assert rig.m.state()["proposals"] == []
    # The hub moved on: the coordinator now names a commit the peer lacks.
    (hub / F4B / "refs" / "main").write_text(NEW)
    rig.m.step()
    (p,) = rig.m.state()["proposals"]
    assert p["reasons"][-1] == "the coordinator has a newer revision"


# -- review findings ---------------------------------------------------------------


def test_a_stale_copy_of_equal_size_is_charged_in_full_against_the_floor():
    """Another commit of the same size used to cost 0 bytes, so a node 5 GiB
    above its floor was proposed a whole new 8 GiB checkpoint."""
    src = Source("complete", 8 * GIB, NEW)
    stale = NodeView(PEER, True, 128 * GIB, True, {F4B: 8 * GIB}, 105 * GIB, revisions={F4B: [OLD]})
    proposals, skipped = _plan([stale], src=src)
    assert proposals == []
    assert "Copying Qwen/Qwen3-4B (8.0 GiB) would leave" in skipped[0].sentence
    roomy = NodeView(PEER, True, 128 * GIB, True, {F4B: 8 * GIB}, 500 * GIB, revisions={F4B: [OLD]})
    (p,), _ = _plan([roomy], src=src)
    assert p.bytes == 8 * GIB


def test_a_deployments_plan_is_not_judged_against_one_nodes_memory():
    big = "models--org--big"
    deps = [dep("org/big", [COORD, PEER], name="tp", idle=600)]
    srcs = {big: Source("complete", 300 * GIB)}
    nodes = {PEER: view(free=2000 * GIB, memory=128 * GIB)}
    proposals, skipped = plan_replication(wanted_pairs(deps, [], [PEER], COORD), srcs, nodes, {}, set(), {})
    assert [p.folder for p in proposals] == [big] and skipped == []
    proposals, skipped = plan_replication(wanted_pairs([], ["org/big"], [PEER], COORD), srcs, nodes, {}, set(), {})
    assert proposals == [] and "could never be served there" in skipped[0].sentence


def _revise(rig, commit):
    refs = rig.m._cache_dir() / F4B / "refs"
    refs.mkdir(exist_ok=True)
    (refs / "main").write_text(commit)


def test_a_dismissal_lapses_when_the_revision_changes_at_the_same_size(rig):
    _revise(rig, OLD)
    rig.m.step()
    assert rig.m.dismiss(F4B, PEER) is True
    rig.m.step()
    assert rig.m.state()["proposals"] == []
    _revise(rig, NEW)  # same bytes, another commit
    rig.m.step()
    assert [p["folder"] for p in rig.m.state()["proposals"]] == [F4B]


def test_an_approval_lapses_when_the_source_changes_before_the_copy_starts(rig):
    _revise(rig, OLD)
    rig.m.step()
    rig.m.approve(F4B, PEER, [])
    _revise(rig, NEW)
    rig.m.step()
    assert rig.rep.calls == []
    row = rig.m.state()["proposals"][0]
    assert row["status"] == "awaiting_approval" and "changed since you approved it" in row["last_error"]
    rig.m.approve(F4B, PEER, [])
    rig.m.step()
    assert rig.rep.calls == [("start", F4B, PEER)]


def test_a_copy_is_planned_on_the_filesystem_it_lands_on(rig):
    """The roomiest disk has 500 GiB; this folder lands on an extra disk of
    256 GiB with 4 MiB above its 51.2 GiB floor, and the copy is 8 MiB."""
    tight = int(256 * GIB * 0.2) + (4 << 20)
    rig.agents.landings[(PEER, F4B)] = {"free": tight, "total": 256 * GIB, "landing": "/extra"}
    rig.m.step()
    assert rig.m.state()["proposals"] == []
    (s,) = rig.m.state()["skipped"]
    assert "51.2 GiB free-space floor" in s["sentence"] and "in /extra" in s["sentence"]
    # Only a copy of derate's on that same disk is room there.
    rig.agents.caches[PEER] = {"models--a--hub": 40 * GIB, "models--b--extra": 20 * GIB}
    rig.store.add_replica("models--a--hub", PEER, 40 * GIB, 1.0)
    rig.store.add_replica("models--b--extra", PEER, 20 * GIB, 2.0)
    rig.agents.landings[(PEER, "models--a--hub")] = {"free": 500 * GIB, "total": 4 * 10**12, "landing": "/hub"}
    rig.agents.landings[(PEER, "models--b--extra")] = {"free": tight, "total": 256 * GIB,
                                                       "landing": "/extra"}
    rig.m.step()
    (p,) = rig.m.state()["proposals"]
    assert [e["folder"] for e in p["evict"]] == ["models--b--extra"]
    assert p["free_bytes"] == tight and p["floor_bytes"] == int(256 * GIB * 0.2)
    # A new folder's disk depends on its size, so the whole source is named;
    # a copy already placed lands where it is.
    assert (PEER, F4B, 8 << 20) in rig.agents.asked
    assert (PEER, "models--b--extra", 0) in rig.agents.asked


def test_an_agent_that_cannot_name_a_landing_keeps_the_node_answer(rig):
    rig.agents.free[PEER] = 70 * GIB
    rig.agents.total[PEER] = 256 * GIB
    rig.m.step()  # no landing answers at all: an older agent
    (p,) = rig.m.state()["proposals"]
    assert p["free_bytes"] == 70 * GIB


@pytest.mark.parametrize(
    ("status", "body", "want"),
    [
        (400, {"detail": "bad folder"}, None),
        (404, {"detail": "Not Found"}, None),
        (200, {"free": 5, "total": 9}, None),  # an agent that ignores ?folder=
        (200, {"free": 5, "total": 9, "landing": "/x"}, {"free": 5, "total": 9, "landing": "/x"}),
    ],
)
def test_the_reader_asks_where_a_folder_lands_and_falls_back(monkeypatch, status, body, want):
    import httpx

    from control_plane.transfer.replication import AgentReader

    seen = {}

    def get(url, *, params=None, headers=None, timeout=None):
        seen.update(url=url, params=params, headers=headers)
        return httpx.Response(status, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    reg = SimpleNamespace(agent_url=lambda n: "http://peer:18081/", agent_token=lambda n: "tok")
    assert AgentReader(reg).landing(PEER, F4B, 7) == want
    assert seen["url"] == "http://peer:18081/agent/transfers/capacity"
    assert seen["params"] == {"folder": F4B, "bytes": 7} and seen["headers"] == {"X-Derate-Token": "tok"}


def test_a_launch_leaves_a_verifying_copy_alone(rig):
    from control_plane.transfer.jobs import CancelRefused

    def cancel(tid):
        if rig.rep.jobs.get(tid, {}).get("status") == "verifying":
            raise CancelRefused("it is committing")
        return rig.rep.jobs.pop(tid, None) is not None

    rig.rep.cancel = cancel
    rig.m.step()
    rig.m.approve(F4B, PEER, [])
    rig.m.step()
    rig.rep.jobs["tr-0"].update(status="verifying")
    rig.deps.rows = [dep("other/model", [COORD, PEER], state=S.LAUNCHING)]
    rig.m.step()
    assert rig.m.state()["proposals"][0]["status"] == "copying"
    rig.rep.jobs["tr-0"].update(status="done", total=8 << 20)
    rig.agents.caches[PEER] = {F4B: 8 << 20}
    rig.m.step()
    assert rig.store.snapshot()["replicas"][pair_key(F4B, PEER)]["complete"] is True
