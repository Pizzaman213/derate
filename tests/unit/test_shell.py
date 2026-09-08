"""The node shell: a remote-exec hole, opened deliberately.

Every test here is about a gate, not a feature. The feature is four lines of
`pty.fork`; what earns its place in the product is that it is absent unless
asked for, refuses without a secret the network cannot obtain, refuses a
handshake from a page we did not serve, and cannot leave a root process behind.

`procs.py` opens with "three rules keep this a narrow verb rather than a
remote-exec hole". This is that hole. These are the rules that replace those.
"""

from __future__ import annotations

import os
import re
import signal
import time

import pytest
from fastapi.testclient import TestClient

from control_plane.contracts import DeviceClass, NodeProfile
from control_plane.registry import shell, shell_config
from control_plane.registry.agent import NodeAgent, create_agent_app


def profile(node_id: str = "spark-01") -> NodeProfile:
    return NodeProfile(
        node_id=node_id,
        hostname=node_id,
        address="127.0.0.1",
        device_class=DeviceClass.DISCRETE,
        gpu_name="Test GPU",
        gpu_count=1,
        total_memory=1 << 30,
        addressable_memory=1 << 30,
        memory_bandwidth_gbps=100.0,
        compute_capability="8.9",
        driver_version="999",
    )


@pytest.fixture
def shell_on(monkeypatch, tmp_path):
    monkeypatch.setenv("DERATE_SHELL", "1")
    monkeypatch.setenv("DERATE_SHELL_KEY", "s3cret")
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    # A shell rather than nsenter: the tests must not depend on the runner
    # being in a container with a host PID namespace, and what is being tested
    # is the plumbing around the session, not which namespaces it lands in.
    monkeypatch.setenv("DERATE_SHELL_BINARY", "/bin/sh")
    yield


# ---------------------------------------------------------------------------
# The route is absent unless asked for
# ---------------------------------------------------------------------------


def test_the_default_is_off(monkeypatch):
    """Pinned, because it drifted once.

    This is not a preference. The `/api` surface has no authentication, the
    gateway binds 0.0.0.0, and the container runs as root with --pid=host and
    the operator's SSH keys mounted -- so the env flag and the out-of-band key
    are two gates on a root prompt, and the argument for collapsing them to one
    ("the key is the real gate anyway") is exactly the argument that makes
    defence in depth not survive a mistake. If this needs to change, it is the
    cluster operator's call and not a refactor's.
    """
    monkeypatch.delenv("DERATE_SHELL", raising=False)
    assert shell_config.enabled() is False
    monkeypatch.setenv("DERATE_SHELL", "")
    assert shell_config.enabled() is False, "an empty value is not an opt-in"


def test_the_route_does_not_exist_by_default(monkeypatch):
    """Absent, not present-and-refusing.

    A route that exists and returns 403 tells an anonymous caller that this
    build has a shell in it. An absent one cannot be probed for, and cannot be
    switched on by anything arriving over the network.
    """
    monkeypatch.delenv("DERATE_SHELL", raising=False)
    app = create_agent_app(NodeAgent(profile()))
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/agent/shell" not in paths


def test_the_route_exists_when_enabled(shell_on):
    app = create_agent_app(NodeAgent(profile()))
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/agent/shell" in paths


# ---------------------------------------------------------------------------
# The key
# ---------------------------------------------------------------------------


def test_no_key_presented_is_refused(shell_on):
    app = create_agent_app(NodeAgent(profile()))
    with TestClient(app) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/agent/shell"):
                pass


def test_the_wrong_key_is_refused(shell_on):
    app = create_agent_app(NodeAgent(profile()))
    with TestClient(app) as client:
        with pytest.raises(Exception):
            with client.websocket_connect(
                "/agent/shell", subprotocols=["derate-shell", "wrong"]
            ):
                pass


def test_the_right_key_opens_a_session(shell_on):
    """The one that proves the gates are gates and not a wall.

    Every other test here asserts a refusal, and a route that refused
    everything would pass all of them. This is the counterweight: the correct
    key, through the real handshake, all the way to bytes off a pty.
    """
    app = create_agent_app(NodeAgent(profile()))
    with TestClient(app) as client:
        with client.websocket_connect(
            "/agent/shell", subprotocols=["derate-shell", "s3cret"]
        ) as ws:
            ws.send_text('{"i": "echo GATE-OPEN\\n"}')
            seen = b""
            for _ in range(80):
                message = ws.receive()
                chunk = message.get("bytes") or (message.get("text") or "").encode()
                seen += chunk
                # Past the echo of what was typed: a pty echoes input, so the
                # first hit is the command, not its output.
                if b"GATE-OPEN" in seen.split(b"\n", 1)[-1]:
                    break
            assert b"GATE-OPEN" in seen.split(b"\n", 1)[-1], seen


def test_a_shell_with_no_key_configured_fails_closed(monkeypatch, tmp_path):
    """Enabled but unconfigured is a refusal, not an open door.

    The tempting bug is to treat a missing key as "no check required", which is
    exactly backwards: an agent that cannot verify a credential must not act on
    an uncredentialed request. `procs.py` already takes this position for the
    cluster token and this follows it.
    """
    monkeypatch.setenv("DERATE_SHELL", "1")
    monkeypatch.delenv("DERATE_SHELL_KEY", raising=False)
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    with pytest.raises(shell.ShellRefused):
        shell.check_key("anything")


def test_the_key_is_never_read_from_a_route(shell_on):
    """The property the whole gate rests on.

    `POST /api/enroll` is unauthenticated, and its token buys the permanent
    cluster token through `POST /api/nodes/join` -- so anything gated on the
    cluster token is gated on a secret the LAN can mint for itself. This key is
    different only because nothing serves it. If that ever stops being true,
    this test is where it should fail.
    """
    app = create_agent_app(NodeAgent(profile()))
    with TestClient(app) as client:
        for path in (
            "/agent/profile",
            "/agent/telemetry",
            "/agent/health",
            "/agent/storage",
        ):
            body = client.get(path).text
            assert "s3cret" not in body, f"{path} leaked the shell key"


# ---------------------------------------------------------------------------
# Origin
# ---------------------------------------------------------------------------


def test_an_unlisted_origin_is_refused(monkeypatch):
    """The only Origin check in the product, and the reason it has to exist.

    A WebSocket handshake is exempt from CORS: no preflight, no
    Access-Control-Allow-Origin negotiation. Nothing here validates Host
    either, so without this any page the operator visits could open a socket to
    a coordinator on their LAN.
    """
    monkeypatch.setenv("DERATE_SHELL_ORIGINS", "http://spark-01:8080")
    with pytest.raises(shell.ShellRefused):
        shell.check_origin("http://evil.example")
    shell.check_origin("http://spark-01:8080")


def test_a_missing_origin_is_allowed(monkeypatch):
    """curl and native clients send none, and the key is what gates them.

    Refusing a missing Origin would break every non-browser caller and buy
    nothing: an attacker's page cannot suppress the header, so its absence is
    evidence the caller is not a browser rather than evidence of an attack.
    """
    monkeypatch.setenv("DERATE_SHELL_ORIGINS", "http://spark-01:8080")
    shell.check_origin(None)


def test_no_configured_origins_means_no_check(monkeypatch):
    monkeypatch.delenv("DERATE_SHELL_ORIGINS", raising=False)
    shell.check_origin("http://anything.example")


# ---------------------------------------------------------------------------
# One session at a time
# ---------------------------------------------------------------------------


def test_only_one_session_per_node(shell_on):
    agent = NodeAgent(profile())
    assert agent.claim_shell() is True
    assert agent.claim_shell() is False, "a second session must be refused"
    agent.release_shell()
    assert agent.claim_shell() is True


# ---------------------------------------------------------------------------
# The session itself
# ---------------------------------------------------------------------------


def test_a_session_runs_a_real_shell_and_answers(shell_on):
    """A real pty and a real child, not a mock.

    The thing worth checking is that what comes back is terminal output --
    which means the echo of what was typed as well as the answer, because a pty
    echoes by default and a caller that assumed otherwise would render every
    keystroke twice.
    """
    session = shell.open_session(cols=80, rows=24)
    try:
        session.write(b"echo derate-marker\n")
        deadline = time.time() + 10
        seen = b""
        while time.time() < deadline and b"derate-marker" not in seen.split(b"\n", 1)[-1]:
            try:
                seen += os.read(session.fd, 65536)
            except BlockingIOError:
                time.sleep(0.05)
            except OSError:
                break
        assert b"derate-marker" in seen
    finally:
        session.close()


def test_closing_a_session_kills_the_whole_process_group(shell_on):
    """A dropped tab must not leave a root process behind.

    The group, not the pid: a shell that ran `sleep 300 &` leaves a child
    holding the pty, and killing only the shell would leave that child running
    with no session left to reach it from. This is the test that would have
    caught shipping `kill` instead of `killpg`.
    """
    session = shell.open_session()
    session.write(b"sleep 300 &\necho started $!\n")
    deadline = time.time() + 10
    seen = b""
    child = None
    while time.time() < deadline:
        try:
            seen += os.read(session.fd, 65536)
        except BlockingIOError:
            time.sleep(0.05)
        except OSError:
            break
        # Searched rather than line-matched: a pty echoes what was typed and
        # carries the prompt on the same line, so the marker arrives as
        # something like "$ $ started 2791369\r\n" -- never at the start.
        if (found := re.search(rb"started (\d+)", seen)) is not None:
            child = int(found.group(1))
            break
    assert child, f"could not find the background child in {seen!r}"

    session.close()

    for _ in range(100):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
        pytest.fail(f"pid {child} survived the session closing")


def test_resize_does_not_raise_on_a_dead_pty(shell_on):
    """Resize arrives from a browser on its own schedule, including after the
    shell has exited. It is not the thing that should report that."""
    session = shell.open_session()
    session.close()
    session.resize(120, 40)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_a_generated_key_is_persisted_private(monkeypatch, tmp_path):
    monkeypatch.delenv("DERATE_SHELL_KEY", raising=False)
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    key = shell_config.ensure_key()
    assert key
    path = shell_config.key_path()
    assert path.read_text().strip() == key
    # 0600. The key is the whole gate, and a world-readable one on a node whose
    # container mounts the operator's SSH keys is not a gate.
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert shell_config.ensure_key() == key, "a second call must not rotate it"


def test_the_host_shell_is_used_only_when_it_would_work(monkeypatch):
    """nsenter needs both halves, and neither implies the other.

    A container started WITHOUT --pid=host still has a PID 1 -- its own
    entrypoint -- and entering its namespaces would be a loop back to where we
    already are. The check compares namespace inodes rather than trusting the
    binary's presence.
    """
    monkeypatch.setattr(shell.shutil, "which", lambda _n: None)
    assert shell.host_reachable() is False
    assert shell.command()[0] != "nsenter"
