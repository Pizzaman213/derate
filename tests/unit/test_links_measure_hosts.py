"""A node id from the measure body never reaches ssh as an option.

``POST /api/links/measure`` used to take ``a`` and ``b`` verbatim; an id the
registry did not know fell through ``LinkService._endpoint`` as the ssh host,
and ``-oProxyCommand=<cmd>`` is parsed by ssh as an option -- command
execution on the coordinator. Three independent checks now stand in the way;
each is pinned here on its own, because each covers a path the others do not
(a rogue node's unvalidated ``hostname`` reaches ``_endpoint`` without the
route).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from control_plane.gateway import create_app
from control_plane.links.runner import CommandResult, SubprocessRunner
from control_plane.links.service import LinkService
from tests.unit.test_gateway import StubLinks, build_deps

INJECTION = "-oProxyCommand=touch /tmp/derate-pwned"


def test_the_route_refuses_a_node_the_registry_does_not_know():
    measured: list[tuple[str, str]] = []

    class Recording(StubLinks):
        def measure(self, a, b):
            measured.append((a, b))
            return None

    deps = build_deps()
    deps.links = Recording()
    with TestClient(create_app(deps)) as client:
        reply = client.post("/api/links/measure", json={"a": "spark-01", "b": INJECTION})
    assert reply.status_code == 404
    assert reply.json()["error"]["code"] == "unknown_node"
    assert measured == []


class _NoAddress:
    """A registry whose node carries a hostname that is an ssh option."""

    def __init__(self, hostname: str) -> None:
        from types import SimpleNamespace

        self._state = SimpleNamespace(profile=SimpleNamespace(address="", hostname=hostname))

    def get_node(self, node_id):
        return self._state


def test_endpoint_refuses_a_host_that_is_not_an_address_or_a_hostname(tmp_path):
    service = LinkService(tmp_path / "links.json", registry=None, local_node_id="spark-01")
    with pytest.raises(ValueError, match="no usable address"):
        service._endpoint(INJECTION)
    rogue = LinkService(tmp_path / "l2.json", registry=_NoAddress("-oProxyCommand=x"), local_node_id="a")
    with pytest.raises(ValueError, match="no usable address"):
        rogue._endpoint("spark-02")
    assert service._endpoint("spark-02").host == "spark-02"


def test_run_on_ends_ssh_options_before_the_host(monkeypatch):
    runner = SubprocessRunner(ssh="ssh", ssh_opts=("-oBatchMode=yes",))
    seen: list[list[str]] = []

    def fake_run(argv, timeout=60.0, env=None):
        seen.append(argv)
        return CommandResult(argv=tuple(argv), returncode=0, stdout="", stderr="", duration_s=0.0)

    monkeypatch.setattr(runner, "run", fake_run)
    runner.run_on("-oProxyCommand=x", ["true"])
    argv = seen[0]
    assert argv[:3] == ["ssh", "-oBatchMode=yes", "--"]
    assert argv[3] == "-oProxyCommand=x"


def test_subprocess_runner_reports_a_missing_binary_as_127():
    result = SubprocessRunner().run(["derate-no-such-binary-xyz"], timeout=5)
    assert result.returncode == 127
    assert not result.timed_out


def test_subprocess_runner_reports_a_timeout_rather_than_raising():
    result = SubprocessRunner().run(["sleep", "5"], timeout=0.2)
    assert result.timed_out is True
    assert result.duration_s < 4
