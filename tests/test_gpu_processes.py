"""Killing a model that is holding GPU memory, end to end.

The gap this covers: the fit gate plans against
``nvidia-smi --query-compute-apps``, but until now nothing could say what was
behind that number or end it. Three layers are exercised here -- the node
agent that can actually signal a process, the coordinator route that decides
whether it may, and the deployment manager's force tier when ``sparkrun stop``
will not confirm.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from control_plane.contracts import DeploymentState, DeviceClass, GpuProcess, NodeProfile
from control_plane.gateway import gpu_procs
from control_plane.procmatch import matches_deployment, port_in
from control_plane.registry import procs
from control_plane.registry.agent import NodeAgent, create_agent_app
from control_plane.registry.serde import processes_to_dict
from control_plane.registry.telemetry import read_gpu_processes

GIB = 1024**3


def profile(node_id="spark-01"):
    return NodeProfile(
        node_id=node_id,
        hostname=node_id,
        address="127.0.0.1",
        device_class=DeviceClass.GB10,
        gpu_name="GB10",
        gpu_count=1,
        total_memory=128 * GIB,
        addressable_memory=119 * GIB,
        memory_bandwidth_gbps=273.0,
        compute_capability="12.1",
        driver_version="580.95",
    )


# ---------------------------------------------------------------------------
# reading the processes
# ---------------------------------------------------------------------------


def test_rows_parse_and_sort_by_memory():
    rows = [
        ["4242", "/usr/bin/vllm", "8192"],
        ["1111", "/home/c/llama.cpp/build/bin/llama-server", "74274"],
    ]
    out = asyncio.run(read_gpu_processes(rows=rows))
    assert [p.pid for p in out] == [1111, 4242]  # largest first
    assert out[0].name == "llama-server"  # basename, for display
    assert out[0].gpu_memory == 74274 * 1024**2


def test_an_unreadable_cell_drops_its_row_rather_than_the_query():
    rows = [["1", "a", "[N/A]"], ["2", "b", "512"], ["notapid", "c", "9"]]
    out = asyncio.run(read_gpu_processes(rows=rows))
    assert [p.pid for p in out] == [2]


def test_unreadable_nvidia_smi_is_none_not_empty(monkeypatch):
    """The one distinction an operator must not lose: a broken driver is not
    an idle GPU. Collapsing them would report a full pool as free."""

    async def broken(*a, **k):
        return None

    monkeypatch.setattr("control_plane.registry.telemetry.run_nvidia_smi_async", broken)
    assert asyncio.run(read_gpu_processes()) is None


def test_payload_distinguishes_unreadable_from_idle():
    assert processes_to_dict("n", None)["available"] is False
    idle = processes_to_dict("n", [])
    assert idle["available"] is True and "pid=host" in idle["reason"]
    live = processes_to_dict("n", [GpuProcess(1, "x", "x", "c", 5)])
    assert live["reason"] is None and live["processes"][0]["pid"] == 1


# ---------------------------------------------------------------------------
# the guards
# ---------------------------------------------------------------------------


def test_guard_refuses_init_self_and_our_own_ancestors():
    for pid in (0, 1, os.getpid()):
        with pytest.raises(procs.KillRefused) as exc:
            procs.guard(pid)
        assert exc.value.code == "protected_pid"
    parent = procs._ppid(os.getpid())
    if parent and parent > 1:
        with pytest.raises(procs.KillRefused):
            procs.guard(parent)


def test_ppid_survives_a_process_name_containing_spaces_and_parens(tmp_path):
    """Field 2 of /proc/<pid>/stat is `(comm)` and may itself hold `) (`.
    Splitting from the left gets the wrong field for exactly the processes
    most worth looking at."""
    fake = tmp_path / "stat"
    fake.write_text("77 (weird ) name) S 42 77 77 0 -1 4194304 1 0\n")
    import control_plane.registry.procs as m

    original = m.Path
    try:
        m.Path = lambda _: fake  # type: ignore[assignment]
        assert m._ppid(77) == 42
    finally:
        m.Path = original


def test_a_pid_not_on_the_gpu_is_refused_without_being_signalled(monkeypatch):
    signalled = []
    monkeypatch.setattr(os, "kill", lambda *a: signalled.append(a))

    async def only_other(*a, **k):
        return [GpuProcess(999, "vllm", "vllm", "root", GIB)]

    monkeypatch.setattr(procs, "read_gpu_processes", only_other)
    with pytest.raises(procs.KillRefused) as exc:
        asyncio.run(procs.kill_gpu_process(1234))
    assert exc.value.code == "not_a_gpu_process"
    assert signalled == []


def test_an_unreadable_gpu_refuses_rather_than_signalling_blind(monkeypatch):
    signalled = []
    monkeypatch.setattr(os, "kill", lambda *a: signalled.append(a))

    async def unreadable(*a, **k):
        return None

    monkeypatch.setattr(procs, "read_gpu_processes", unreadable)
    with pytest.raises(procs.KillRefused) as exc:
        asyncio.run(procs.kill_gpu_process(1234))
    assert exc.value.code == "gpu_unreadable"
    assert signalled == []


# ---------------------------------------------------------------------------
# actually killing something
# ---------------------------------------------------------------------------


def _spawn(ignore_term: bool) -> subprocess.Popen:
    """A child that says when it is ready.

    The readiness line is not ceremony: without it the SIGTERM can land before
    the interpreter has installed SIG_IGN, and the escalation test passes for
    the wrong reason -- it would be asserting that SIGTERM works, which the
    previous test already covers.
    """
    setup = "import signal;signal.signal(signal.SIGTERM,signal.SIG_IGN);" if ignore_term else ""
    body = setup + "import sys,time;print('up',flush=True);time.sleep(300)"
    child = subprocess.Popen(
        [sys.executable, "-c", body], stdout=subprocess.PIPE, text=True
    )
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "up"
    return child


def _as_gpu_process(monkeypatch, pid: int, alive_bytes: int = GIB):
    """Report *pid* as a compute context for as long as it is running.

    Uses ``procs._alive`` rather than a raw ``os.kill(pid, 0)`` because a
    reaped-later child is a zombie, and nvidia-smi does not report a context
    for one. A fake that did would make this test assert the opposite of what
    the driver does.
    """

    async def fake(*a, **k):
        if not procs._alive(pid):
            return []
        return [GpuProcess(pid, "sleeper", "python -c sleep", "tester", alive_bytes)]

    monkeypatch.setattr(procs, "read_gpu_processes", fake)


def test_sigterm_is_enough_and_the_reclaim_is_reported(monkeypatch):
    child = _spawn(ignore_term=False)
    try:
        _as_gpu_process(monkeypatch, child.pid)
        result = asyncio.run(procs.kill_gpu_process(child.pid, grace_s=10.0, force_s=5.0))
        assert result.signal_sent == "SIGTERM"
        assert result.exited is True
        assert result.gpu_memory_reclaimed == GIB
    finally:
        child.kill()
        child.wait(timeout=10)


def test_a_process_that_ignores_sigterm_is_escalated_to_sigkill(monkeypatch):
    child = _spawn(ignore_term=True)
    try:
        _as_gpu_process(monkeypatch, child.pid)
        result = asyncio.run(procs.kill_gpu_process(child.pid, grace_s=1.5, force_s=5.0))
        assert result.signal_sent == "SIGKILL"
        assert result.exited is True
        assert "SIGKILL" in result.detail
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def test_success_means_the_memory_came_back_not_that_the_signal_landed(monkeypatch):
    """A process can exit while the driver still holds its context. Reporting
    that as a reclaim hands the operator a number they will plan against."""
    child = _spawn(ignore_term=False)
    held = GpuProcess(child.pid, "sleeper", "cmd", "tester", 40 * GIB)

    async def never_releases(*a, **k):
        return [held]

    try:
        monkeypatch.setattr(procs, "read_gpu_processes", never_releases)
        result = asyncio.run(procs.kill_gpu_process(child.pid, grace_s=1.0, force_s=1.0))
        assert result.exited is False
        assert result.gpu_memory_reclaimed == 0
        assert "still holding" in result.detail
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


# ---------------------------------------------------------------------------
# the agent route
# ---------------------------------------------------------------------------


def test_the_token_is_checked_before_anything_else(monkeypatch):
    """An uncredentialled caller must not learn which PIDs exist or are
    protected by reading which refusal comes back."""
    client = TestClient(create_agent_app(NodeAgent(profile(), token="right")))
    assert client.post("/agent/processes/1/kill").status_code == 403
    assert (
        client.post("/agent/processes/1/kill", headers={"X-Derate-Token": "wrong"}).status_code
        == 403
    )


def test_an_agent_with_no_token_configured_refuses_rather_than_waving_through():
    client = TestClient(create_agent_app(NodeAgent(profile(), token=None)))
    assert client.post("/agent/processes/1/kill", headers={"X-Derate-Token": ""}).status_code == 403


def test_a_late_admission_gives_the_agent_its_token():
    agent = NodeAgent(profile(), token=None)
    assert agent.token_matches("later") is False
    agent.set_token("later")
    assert agent.token_matches("later") is True


def test_processes_route_serves_the_annotated_shape(monkeypatch):
    async def one(*a, **k):
        return [GpuProcess(7, "llama-server", "llama-server -m x", "connor", 72 * GIB)]

    monkeypatch.setattr("control_plane.registry.agent.read_gpu_processes", one)
    client = TestClient(create_agent_app(NodeAgent(profile(), token="t")))
    body = client.get("/agent/processes").json()
    assert body["available"] is True
    assert body["processes"][0]["name"] == "llama-server"


# ---------------------------------------------------------------------------
# attribution
# ---------------------------------------------------------------------------


class _Dep:
    def __init__(self, deployment_id, served_name, state, node_ids):
        self.deployment_id = deployment_id
        self.served_name = served_name
        self.state = state
        self.plan = type("P", (), {"node_ids": node_ids})()


def test_a_port_is_matched_as_a_number_not_a_substring():
    assert port_in("--port 8100", 8100)
    assert not port_in("--port 81000", 8100)
    assert not port_in("--port 810", 8100)
    assert matches_deployment("run sparkrun_ab12 now", "sparkrun_ab12", None)


def test_a_managed_backend_is_not_killable_and_a_stray_is():
    deps = [_Dep("d-1", "gpt-oss-120b", DeploymentState.READY, ["spark-01"])]
    handles = {"d-1": {"cluster_id": "sparkrun_ab", "port": 8100}}
    rows = [
        {"pid": 1, "name": "vllm", "command": "vllm serve --port 8100"},
        {"pid": 2, "name": "llama-server", "command": "llama-server --port 8080"},
    ]
    out = gpu_procs.attribute(rows, deps, handles, "spark-01")
    managed, stray = out[0], out[1]
    assert managed["served_name"] == "gpt-oss-120b" and managed["killable"] is False
    assert "Stop the deployment" in managed["not_killable_reason"]
    assert stray["deployment_id"] is None and stray["killable"] is True


def test_a_process_under_a_stopped_record_is_exactly_the_orphan_to_clear():
    deps = [_Dep("d-1", "gpt-oss-120b", DeploymentState.STOPPED, ["spark-01"])]
    handles = {"d-1": {"cluster_id": None, "port": 8100}}
    rows = [{"pid": 1, "name": "vllm", "command": "vllm serve --port 8100"}]
    out = gpu_procs.attribute(rows, deps, handles, "spark-01")
    assert out[0]["killable"] is True
    assert out[0]["served_name"] == "gpt-oss-120b"


def test_a_deployment_on_another_node_never_claims_this_nodes_process():
    deps = [_Dep("d-1", "m", DeploymentState.READY, ["spark-02"])]
    handles = {"d-1": {"cluster_id": None, "port": 8100}}
    rows = [{"pid": 1, "name": "vllm", "command": "vllm serve --port 8100"}]
    out = gpu_procs.attribute(rows, deps, handles, "spark-01")
    assert out[0]["deployment_id"] is None


def test_no_handles_degrades_to_unattributed_rather_than_refusing():
    """The stub deployment ports have no handles(). Everything is then a stray,
    which is the safe direction: a confirm dialog still stands in the way."""
    deps = [_Dep("d-1", "m", DeploymentState.READY, ["spark-01"])]
    rows = [{"pid": 1, "name": "vllm", "command": "vllm serve --port 8100"}]
    out = gpu_procs.attribute(rows, deps, None, "spark-01")
    assert out[0]["deployment_id"] is None and out[0]["killable"] is True


# ---------------------------------------------------------------------------
# a node agent on a socket, for the coordinator and manager tiers
# ---------------------------------------------------------------------------


class FakeAgent:
    """A real HTTP node agent, so the coordinator's httpx hop is exercised."""

    def __init__(self, processes, *, kill_status=200):
        self.processes = processes
        self.kill_status = kill_status
        self.killed: list[int] = []
        self.tokens: list[str | None] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/agent/processes":
                    self._send(
                        200,
                        {
                            "node_id": "spark-01",
                            "processes": outer.processes,
                            "available": True,
                            "reason": None,
                        },
                    )
                else:
                    self._send(404, {"detail": "no"})

            def do_POST(self):
                outer.tokens.append(self.headers.get("X-Derate-Token"))
                pid = int(self.path.split("/")[3])
                if outer.kill_status != 200:
                    self._send(outer.kill_status, {"detail": {"message": "nope"}})
                    return
                outer.killed.append(pid)
                outer.processes = [p for p in outer.processes if p["pid"] != pid]
                self._send(
                    200,
                    {
                        "pid": pid,
                        "name": "x",
                        "signal_sent": "SIGTERM",
                        "exited": True,
                        "gpu_memory_before": GIB,
                        "gpu_memory_reclaimed": GIB,
                        "detail": "SIGTERM ended x (pid %d); 1.0 GiB released." % pid,
                    },
                )

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


# ---------------------------------------------------------------------------
# the coordinator route
# ---------------------------------------------------------------------------


def _gateway(agent_url, deployments, handles=None):
    """A gateway whose registry points at a real HTTP agent."""
    from tests.test_gateway import FakeDeployments, FakeRegistry, build_deps
    from control_plane.gateway.app import create_app

    class Reg(FakeRegistry):
        def agent_url(self, node_id):
            return agent_url if node_id == "spark-01" else None

        def cluster_token(self):
            return "cluster-secret"

    class Deps(FakeDeployments):
        def handles(self):
            return handles or {}

    return create_app(build_deps(registry=Reg(), deployments=Deps(deployments)))


def test_route_annotates_a_stray_and_leaves_it_killable():
    procs_wire = [
        {"pid": 5, "name": "llama-server", "command": "llama-server --port 8080",
         "user": "connor", "gpu_memory": 72 * GIB}
    ]
    with FakeAgent(procs_wire) as agent:
        with TestClient(_gateway(agent.url, [])) as client:
            body = client.get("/api/nodes/spark-01/processes").json()
    assert body["processes"][0]["killable"] is True
    assert body["processes"][0]["deployment_id"] is None


def test_route_refuses_to_kill_a_backend_the_router_is_still_using():
    from tests.test_gateway import make_deployment

    dep = make_deployment(
        "d-1",
        "gpt-oss-120b",
        backend_url="http://127.0.0.1:8100/v1",
        state=DeploymentState.READY,
        node_ids=("spark-01",),
    )
    procs_wire = [
        {"pid": 9, "name": "vllm", "command": "vllm serve --port 8100",
         "user": "root", "gpu_memory": 60 * GIB}
    ]
    with FakeAgent(procs_wire) as agent:
        app = _gateway(agent.url, [dep], handles={"d-1": {"cluster_id": None, "port": 8100}})
        with TestClient(app) as client:
            res = client.delete("/api/nodes/spark-01/processes/9")
    assert res.status_code == 409
    assert "gpt-oss-120b" in res.json()["error"]["message"]
    assert agent.killed == []  # never reached the machine


def test_route_kills_a_stray_and_presents_the_cluster_token():
    procs_wire = [
        {"pid": 5, "name": "llama-server", "command": "llama-server --port 8080",
         "user": "connor", "gpu_memory": 72 * GIB}
    ]
    with FakeAgent(procs_wire) as agent:
        with TestClient(_gateway(agent.url, [])) as client:
            res = client.delete("/api/nodes/spark-01/processes/5")
    assert res.status_code == 200
    assert res.json()["gpu_memory_reclaimed"] == GIB
    assert agent.killed == [5]
    assert agent.tokens == ["cluster-secret"]


def test_route_404s_a_pid_that_is_not_holding_gpu_memory():
    with FakeAgent([]) as agent:
        with TestClient(_gateway(agent.url, [])) as client:
            res = client.delete("/api/nodes/spark-01/processes/4242")
    assert res.status_code == 404


def test_a_node_with_no_agent_url_says_so_rather_than_looking_idle():
    """An empty list here would read as "nothing is holding the GPU", which is
    the one wrong answer: it is the basis for deciding the memory is free."""
    with FakeAgent([]) as agent:
        with TestClient(_gateway(agent.url, [])) as client:
            res = client.get("/api/nodes/spark-02/processes")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "node_agent_unreachable"


def test_an_unreachable_agent_is_a_502_not_an_empty_gpu():
    with TestClient(_gateway("http://127.0.0.1:1", [])) as client:
        res = client.get("/api/nodes/spark-01/processes")
    assert res.status_code == 502


# ---------------------------------------------------------------------------
# the deployment manager's force tier
# ---------------------------------------------------------------------------


def _agent_backed_manager(tmp_path, agent):
    """A manager whose nodes are served by *agent*, and whose sparkrun will
    not confirm a stop.

    ``check_job`` is answered from the agent's own process list rather than a
    flag, because that is what the second tier actually changes: sparkrun keeps
    saying "running" until the process is gone from the machine.
    """
    from tests import test_deploy as td

    class Reg(td.FakeRegistry):
        def agent_urls(self, include_local=False):
            return {"spark-01": agent.url, "spark-02": agent.url}

        def cluster_token(self):
            return "cluster-secret"

    registry = Reg(td.fx.SPARK_01, td.fx.SPARK_02)

    class StubbornAdapter(td.FakeAdapter):
        def stop(self, cluster_id, *, hosts=None):
            self.stops.append(cluster_id)
            return False, "stop failed"  # never confirms on its own

        def check_job(self, cluster_id, *, hosts=None, timeout=60.0):
            return {"running": bool(agent.processes), "cluster_id": cluster_id}

    from control_plane.deploy.manager import DeploymentManager

    adapter = StubbornAdapter(registry, recipe_dir=tmp_path / "recipes")
    return DeploymentManager(
        adapter,
        registry,
        state_dir=tmp_path,
        probe_fn=td.FakeProbe(),
        autostart=False,
        ready_poll_interval_s=0.01,
        ready_timeout_s=5.0,
        stop_confirm_timeout_s=0.5,
    )


def test_a_stop_sparkrun_will_not_confirm_is_killed_on_its_nodes(tmp_path):
    """Today's behaviour was to give up and record the orphan. The workload
    then holds the pool forever and the fit gate plans around it."""
    from control_plane.deploy import events as ev
    from tests import test_deploy as td

    with FakeAgent([]) as agent:
        manager = _agent_backed_manager(tmp_path, agent)
        try:
            dep = manager.launch(td.fx.GPT_OSS_120B, td.fx.pp2_plan(), td.fx.fits(),
                                 "vllm", 65536, 256)
            assert td.wait_for(
                lambda: manager.get(dep.deployment_id).state is DeploymentState.READY
            )
            port = manager.handles()[dep.deployment_id]["port"]
            agent.processes = [
                {"pid": 31337, "name": "vllm", "gpu_memory": 60 * GIB,
                 "command": "vllm serve --port %d" % port}
            ]

            manager.stop(dep.deployment_id)

            assert agent.killed == [31337]
            assert agent.tokens == ["cluster-secret"]
            live = manager.get(dep.deployment_id)
            assert live.state is DeploymentState.STOPPED
            assert "killed on its nodes" in live.last_error
            assert "may still be running" not in live.last_error
            assert td.drain(manager.bus, ev.STOP_ESCALATED)
        finally:
            manager.close()


def test_a_process_belonging_to_another_deployment_is_not_swept_up(tmp_path):
    """The tier signals this deployment's processes, not everything on the
    node. A second model serving from the same box must survive."""
    from tests import test_deploy as td

    with FakeAgent([]) as agent:
        manager = _agent_backed_manager(tmp_path, agent)
        try:
            dep = manager.launch(td.fx.GPT_OSS_120B, td.fx.pp2_plan(), td.fx.fits(),
                                 "vllm", 65536, 256)
            assert td.wait_for(
                lambda: manager.get(dep.deployment_id).state is DeploymentState.READY
            )
            port = manager.handles()[dep.deployment_id]["port"]
            agent.processes = [
                {"pid": 1, "name": "vllm", "gpu_memory": GIB,
                 "command": "vllm serve --port %d" % (port + 1)},
                {"pid": 2, "name": "vllm", "gpu_memory": GIB,
                 "command": "vllm serve --port %d" % port},
            ]
            manager.stop(dep.deployment_id)
            assert agent.killed == [2]
        finally:
            manager.close()


def test_the_tier_is_skipped_when_the_registry_cannot_be_asked(tmp_path):
    """Unit wiring and the stub ports have no agent_urls/cluster_token. The
    old behaviour has to survive that untouched -- it is what every existing
    stop test exercises."""
    from tests import test_deploy as td

    manager = td.make_manager(tmp_path)  # plain FakeRegistry: neither method
    try:
        dep = manager.launch(td.fx.GPT_OSS_120B, td.fx.pp2_plan(), td.fx.fits(),
                             "vllm", 65536, 256)
        assert td.wait_for(
            lambda: manager.get(dep.deployment_id).state is DeploymentState.READY
        )
        record = manager._records[dep.deployment_id]
        assert manager._kill_through_agents(record, "sparkrun_x") == (False, None)
        manager.registry = None
        assert manager._kill_through_agents(record, "sparkrun_x") == (False, None)
    finally:
        manager.close()
