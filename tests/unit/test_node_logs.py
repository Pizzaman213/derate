"""Reading a node's own node.log/proxy.log over HTTP, end to end.

Distinct from tests/unit/test_gpu_processes.py's subject: that is a
deployment's serving log via sparkrun; this is the control plane's own
process log, written by logfiles.py. Two layers, mirroring that file's
structure: the node agent that can actually read the files, and the
coordinator route that proxies to it.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from fastapi.testclient import TestClient

from control_plane import logfiles
from control_plane.contracts import DeviceClass, NodeProfile
from control_plane.registry.agent import NodeAgent, create_agent_app

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
# the agent route
# ---------------------------------------------------------------------------


def test_the_agent_route_serves_a_tail_of_the_named_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_LOG_DIR", str(tmp_path))
    (tmp_path / logfiles.NODE_LOG).write_text("a\nb\nc\n", encoding="utf-8")

    client = TestClient(create_agent_app(NodeAgent(profile())))
    body = client.get("/agent/logs", params={"which": "node"}).json()

    assert body["available"] is True
    assert body["lines"] == ["a", "b", "c"]
    assert body["node_id"] == "spark-01"
    assert body["which"] == "node"


def test_the_agent_route_reports_a_file_that_does_not_exist_yet(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_LOG_DIR", str(tmp_path))

    client = TestClient(create_agent_app(NodeAgent(profile())))
    body = client.get("/agent/logs", params={"which": "proxy"}).json()

    assert body["available"] is False
    assert body["lines"] == []


def test_the_agent_route_422s_a_which_it_does_not_recognise(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_LOG_DIR", str(tmp_path))

    client = TestClient(create_agent_app(NodeAgent(profile())))
    res = client.get("/agent/logs", params={"which": "bogus"})
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# a node agent on a socket, for the coordinator route
# ---------------------------------------------------------------------------


class FakeAgent:
    """A real HTTP node agent, so the coordinator's httpx hop is exercised."""

    def __init__(self, lines, *, status=200):
        self.lines = lines
        self.status = status
        self.requested = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requested.append(self.path)
                if self.path.startswith("/agent/logs"):
                    raw = json.dumps(
                        {
                            "node_id": "spark-01",
                            "which": "node",
                            "lines": outer.lines,
                            "truncated": False,
                            "available": True,
                            "reason": None,
                        }
                    ).encode()
                    self.send_response(outer.status)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                else:
                    self.send_response(404)
                    self.end_headers()

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


def _gateway(agent_url):
    """A gateway whose registry points at a real HTTP agent."""
    from tests.unit.test_gateway import FakeDeployments, FakeRegistry, build_deps
    from control_plane.gateway.app import create_app

    class Reg(FakeRegistry):
        def agent_url(self, node_id):
            return agent_url if node_id == "spark-01" else None

    return create_app(build_deps(registry=Reg(), deployments=FakeDeployments([])))


def test_route_passes_which_and_tail_through_to_the_agent():
    with FakeAgent(["a", "b", "c"]) as agent:
        with TestClient(_gateway(agent.url)) as client:
            res = client.get("/api/nodes/spark-01/logs", params={"which": "proxy", "tail": 10})
    assert res.status_code == 200
    assert res.json()["lines"] == ["a", "b", "c"]
    assert "which=proxy" in agent.requested[0]
    assert "tail=10" in agent.requested[0]


def test_a_node_with_no_agent_url_is_a_404_not_an_empty_log():
    with FakeAgent([]) as agent:
        with TestClient(_gateway(agent.url)) as client:
            res = client.get("/api/nodes/spark-02/logs")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "node_agent_unreachable"


def test_an_unreachable_agent_is_a_502():
    with TestClient(_gateway("http://127.0.0.1:1")) as client:
        res = client.get("/api/nodes/spark-01/logs")
    assert res.status_code == 502


def test_an_invalid_which_is_a_400_without_ever_reaching_the_agent():
    with FakeAgent([]) as agent:
        with TestClient(_gateway(agent.url)) as client:
            res = client.get("/api/nodes/spark-01/logs", params={"which": "bogus"})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "invalid_which"
    assert agent.requested == []
