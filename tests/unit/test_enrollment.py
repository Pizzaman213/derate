"""Enrollment tokens and the curl installer.

The credential that turns "install this machine, then go and click Admit" into
one command. Three things are under test here and they fail in different ways,
so they are kept apart:

1. The store itself -- minting, expiry, use accounting, revocation, and the
   0600 file it persists to.
2. ``Registry.handle_join``, which now accepts two kinds of token that mean
   different things. The existing cluster-token and no-token behaviour must be
   bit-for-bit unchanged; that is asserted here as well as in test_registry.py,
   because this is the change that could break it.
3. The gateway routes and the script itself.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shutil
import stat
import pty
import select
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from control_plane.gateway import enroll_api
from control_plane.gateway.app import create_app
from control_plane.gateway.deps import GatewayDeps
from control_plane.registry import JoinRejected, Registry, RegistryConfig
from control_plane.registry.bootstrap import adopt_cluster_token
from control_plane.registry.enrollment import (
    DEFAULT_TTL_S,
    MAX_TTL_S,
    EnrollmentStore,
)
from control_plane.registry.identity import CLUSTER_FILE
from control_plane.registry.roster import ROSTER_FILE

from tests.fixtures import SPARK_01, SPARK_02
from tests.unit.test_registry import FakeClient, make_registry, run

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "install.sh"


class Clock:
    """A hand-cranked clock. Expiry is a time question, not a sleep question."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ----------------------------------------------------------------------
# 1. The store
# ----------------------------------------------------------------------


def test_minted_token_is_live_and_carries_its_bounds(tmp_path):
    clock = Clock()
    store = EnrollmentStore(tmp_path, clock=clock)
    token = store.mint()

    assert token.is_live(clock.now)
    assert token.uses_remaining == 1
    assert token.auto_admit is True
    assert token.expires_at == clock.now + DEFAULT_TTL_S
    assert token.token.startswith(f"ej_{token.token_id}_")


def test_two_tokens_never_collide(tmp_path):
    store = EnrollmentStore(tmp_path)
    tokens = [store.mint(uses=None) for _ in range(20)]
    assert len({t.token for t in tokens}) == 20
    assert len({t.token_id for t in tokens}) == 20


def test_verify_accepts_the_secret_and_nothing_else(tmp_path):
    store = EnrollmentStore(tmp_path)
    token = store.mint()

    assert store.verify(token.token) is not None
    assert store.verify(token.token_id) is None       # the handle is not the key
    assert store.verify(token.token + "x") is None
    assert store.verify("") is None
    assert store.verify(None) is None


def test_an_expired_token_stops_verifying_and_is_pruned(tmp_path):
    clock = Clock()
    store = EnrollmentStore(tmp_path, clock=clock)
    token = store.mint(ttl_s=60)

    clock.advance(59)
    assert store.verify(token.token) is not None
    clock.advance(2)
    assert store.verify(token.token) is None
    assert store.live() == []


def test_uses_are_spent_and_the_last_one_removes_the_token(tmp_path):
    store = EnrollmentStore(tmp_path)
    token = store.mint(uses=2)

    store.consume(token.token_id)
    assert store.verify(token.token).uses_remaining == 1
    store.consume(token.token_id)
    assert store.verify(token.token) is None


def test_unlimited_uses_never_spend(tmp_path):
    store = EnrollmentStore(tmp_path)
    token = store.mint(uses=None)
    for _ in range(5):
        store.consume(token.token_id)
    assert store.verify(token.token) is not None


def test_revoke_is_immediate_and_idempotent(tmp_path):
    store = EnrollmentStore(tmp_path)
    token = store.mint()

    assert store.revoke(token.token_id) is True
    assert store.verify(token.token) is None
    assert store.revoke(token.token_id) is False
    assert store.revoke("never-existed") is False


def test_tokens_survive_a_restart_but_expired_ones_do_not(tmp_path):
    clock = Clock()
    store = EnrollmentStore(tmp_path, clock=clock)
    live = store.mint(ttl_s=3600)
    short = store.mint(ttl_s=60)

    clock.advance(120)
    reopened = EnrollmentStore(tmp_path, clock=clock)

    assert reopened.verify(live.token) is not None
    assert reopened.verify(short.token) is None


def test_the_token_file_is_never_world_readable(tmp_path):
    """The same rule identity.py holds itself to, for the same reason."""
    store = EnrollmentStore(tmp_path)
    store.mint()
    mode = stat.S_IMODE(os.stat(tmp_path / "enrollments.json").st_mode)
    assert mode == 0o600


def test_an_unwritable_data_dir_still_mints(tmp_path):
    """A read-only volume loses persistence, not the ability to add a node."""
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    store = EnrollmentStore(blocked / "sub")
    token = store.mint()
    assert store.verify(token.token) is not None


def test_a_corrupt_token_file_is_not_fatal(tmp_path):
    (tmp_path / "enrollments.json").write_text("{ this is not json")
    store = EnrollmentStore(tmp_path)
    assert store.live() == []
    assert store.mint().token


def test_malformed_records_are_skipped_not_fatal(tmp_path):
    (tmp_path / "enrollments.json").write_text(
        json.dumps({"tokens": [{"token_id": "x"}, {"nonsense": 1}]})
    )
    assert EnrollmentStore(tmp_path).live() == []


@pytest.mark.parametrize("ttl", [0, -1, MAX_TTL_S + 1])
def test_out_of_range_ttl_is_refused(tmp_path, ttl):
    with pytest.raises(ValueError):
        EnrollmentStore(tmp_path).mint(ttl_s=ttl)


def test_zero_uses_is_refused(tmp_path):
    with pytest.raises(ValueError):
        EnrollmentStore(tmp_path).mint(uses=0)


def test_public_view_never_carries_the_secret(tmp_path):
    store = EnrollmentStore(tmp_path)
    token = store.mint()
    rows = store.public_list()
    assert len(rows) == 1
    assert "token" not in rows[0]
    assert rows[0]["token_id"] == token.token_id
    assert token.token not in json.dumps(rows)


# ----------------------------------------------------------------------
# 2. handle_join
# ----------------------------------------------------------------------


def joining_registry(tmp_path, token="cluster-token"):
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    return make_registry(tmp_path, client=client, token=token)


def test_enrollment_token_admits_straight_to_member(tmp_path):
    registry = joining_registry(tmp_path)
    minted = registry.mint_enrollment()

    result = run(registry.handle_join(minted.token, SPARK_02, "http://10.0.0.12:8081"))

    assert result["status"] == "member"
    assert result["cluster_id"] == registry.cluster_id()
    assert registry.get_node("spark-02") is not None
    # It never sat in the candidate list waiting for a click.
    assert registry.candidates() == []


def test_the_admission_hands_over_the_permanent_cluster_token(tmp_path):
    """Without this the node locks itself out when the enrollment token dies."""
    registry = joining_registry(tmp_path)
    minted = registry.mint_enrollment()

    result = run(registry.handle_join(minted.token, SPARK_02, "http://10.0.0.12:8081"))

    assert result["cluster_token"] == registry.cluster_token()


def test_a_member_admitted_this_way_survives_the_token_expiring(tmp_path):
    """The regression this whole hand-over exists to prevent.

    handle_join checks the token before it checks membership, so a node that
    kept presenting a spent enrollment token would be 403'd out of a cluster
    it is already a member of.
    """
    registry = joining_registry(tmp_path)
    minted = registry.mint_enrollment()
    first = run(registry.handle_join(minted.token, SPARK_02, "http://10.0.0.12:8081"))

    # The enrollment token is now spent (uses=1 by default).
    with pytest.raises(JoinRejected):
        run(registry.handle_join(minted.token, SPARK_02, "http://10.0.0.12:8081"))

    # But the node kept what it was handed, and rejoining with that works.
    again = run(
        registry.handle_join(first["cluster_token"], SPARK_02, "http://10.0.0.12:8081")
    )
    assert again["status"] == "member"


def test_the_enrollment_token_is_spent_by_the_join(tmp_path):
    registry = joining_registry(tmp_path)
    minted = registry.mint_enrollment(uses=1)

    run(registry.handle_join(minted.token, SPARK_02, "http://10.0.0.12:8081"))

    assert registry.enrollments() == []


def test_a_two_use_token_admits_two_machines(tmp_path):
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    client.serve("http://10.0.0.11:8081", SPARK_01)
    registry = make_registry(tmp_path, client=client, token="cluster-token")
    minted = registry.mint_enrollment(uses=2)

    run(registry.handle_join(minted.token, SPARK_02, "http://10.0.0.12:8081"))
    run(registry.handle_join(minted.token, SPARK_01, "http://10.0.0.11:8081"))

    assert registry.get_node("spark-02") is not None
    assert registry.get_node("spark-01") is not None
    assert registry.enrollments() == []


def test_an_expired_enrollment_token_is_rejected_like_any_other_wrong_one(tmp_path):
    """And with the same sentence: this must not be a token oracle."""
    clock = Clock()
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    config = RegistryConfig(data_dir=tmp_path, token="cluster-token", agent_port=8081)
    registry = Registry(config=config, client=client, clock=clock)
    minted = registry.mint_enrollment(ttl_s=60)
    clock.advance(61)

    with pytest.raises(JoinRejected) as expired:
        run(registry.handle_join(minted.token, SPARK_02, "http://10.0.0.12:8081"))
    with pytest.raises(JoinRejected) as garbage:
        run(registry.handle_join("nonsense", SPARK_02, "http://10.0.0.12:8081"))

    assert str(expired.value) == str(garbage.value)
    assert registry.candidates() == []
    assert registry.list_nodes() == []


def test_a_revoked_token_no_longer_admits(tmp_path):
    registry = joining_registry(tmp_path)
    minted = registry.mint_enrollment()
    assert registry.revoke_enrollment(minted.token_id) is True

    with pytest.raises(JoinRejected):
        run(registry.handle_join(minted.token, SPARK_02, "http://10.0.0.12:8081"))


def test_auto_admit_false_still_only_makes_a_candidate(tmp_path):
    registry = joining_registry(tmp_path)
    minted = registry.mint_enrollment(auto_admit=False)

    result = run(registry.handle_join(minted.token, SPARK_02, "http://10.0.0.12:8081"))

    assert result["status"] == "candidate"
    assert "cluster_token" not in result
    assert [c["node_id"] for c in registry.candidates()] == ["spark-02"]


def test_an_enrollment_admission_is_persisted_like_any_other(tmp_path):
    """It goes through `admit`, so it lands in the roster and survives a restart."""
    registry = joining_registry(tmp_path)
    minted = registry.mint_enrollment()
    run(registry.handle_join(minted.token, SPARK_02, "http://10.0.0.12:8081"))

    roster = json.loads((tmp_path / ROSTER_FILE).read_text())
    assert "spark-02" in roster["members"]
    assert roster["members"]["spark-02"]["agent_url"] == "http://10.0.0.12:8081"
    assert roster["candidates"] == {}


def test_an_enrollment_token_still_has_to_answer_the_probe_back(tmp_path):
    """A token is permission to join, never permission to be believed."""
    client = FakeClient()  # nothing served at that URL
    registry = make_registry(tmp_path, client=client, token="cluster-token")
    minted = registry.mint_enrollment()

    with pytest.raises(JoinRejected):
        run(registry.handle_join(minted.token, SPARK_02, "http://10.0.0.12:8081"))
    assert registry.list_nodes() == []
    # And it was not spent by an attempt that never got anywhere.
    assert len(registry.enrollments()) == 1


# --- the three unchanged paths ----------------------------------------------


def test_the_cluster_token_still_only_makes_a_candidate(tmp_path):
    registry = joining_registry(tmp_path, token="cluster-token")
    result = run(registry.handle_join("cluster-token", SPARK_02, "http://10.0.0.12:8081"))
    assert result["status"] == "candidate"
    assert "cluster_token" not in result


def test_no_token_still_makes_a_candidate(tmp_path):
    registry = joining_registry(tmp_path)
    result = run(registry.handle_join(None, SPARK_02, "http://10.0.0.12:8081"))
    assert result == {"node_id": "spark-02", "status": "candidate"}


def test_a_wrong_token_is_still_a_flat_rejection(tmp_path):
    registry = joining_registry(tmp_path)
    with pytest.raises(JoinRejected):
        run(registry.handle_join("wrong", SPARK_02, "http://10.0.0.12:8081"))
    assert registry.candidates() == []
    assert registry.list_nodes() == []


# ----------------------------------------------------------------------
# 3. The worker keeps what it was handed
# ----------------------------------------------------------------------


def test_adopt_persists_the_token_and_switches_to_it(tmp_path):
    config = RegistryConfig(data_dir=tmp_path, token="ej_short_lived")
    updated = adopt_cluster_token(
        config, {"cluster_token": "the-permanent-one", "cluster_id": "c-abcd"}
    )

    assert updated.token == "the-permanent-one"
    stored = json.loads((tmp_path / CLUSTER_FILE).read_text())
    assert stored["token"] == "the-permanent-one"
    assert stored["cluster_id"] == "c-abcd"


def test_adopt_is_a_no_op_without_a_handover(tmp_path):
    config = RegistryConfig(data_dir=tmp_path, token="mine")
    assert adopt_cluster_token(config, {"status": "candidate"}) is config
    assert adopt_cluster_token(config, None) is config
    assert adopt_cluster_token(config, {"cluster_token": "mine"}) is config
    assert not (tmp_path / CLUSTER_FILE).exists()


def test_rejoin_loop_adopts_before_it_returns(tmp_path):
    """The loop must present the permanent token on its next pass, not the
    enrollment token it started with."""
    from control_plane.registry.bootstrap import RoleDecision, rejoin_until_admitted

    config = RegistryConfig(data_dir=tmp_path, token="ej_one_shot")
    presented = []

    async def fake_join(url, token, profile, agent_url):
        presented.append(token)
        if len(presented) == 1:
            return {"node_id": "spark-02", "cluster_id": "c-abcd", "status": "candidate"}
        return {
            "node_id": "spark-02",
            "cluster_id": "c-abcd",
            "status": "member",
            "cluster_token": "the-permanent-one",
        }

    async def no_sleep(_seconds):
        return None

    decision = RoleDecision("worker", "http://10.0.0.11:8080", False, "test")
    result = run(
        rejoin_until_admitted(
            decision, config, SPARK_02, "http://10.0.0.12:8081",
            join=fake_join, sleep=no_sleep, jitter=lambda: 0.0,
        )
    )

    assert result["status"] == "member"
    assert presented == ["ej_one_shot", "ej_one_shot"]
    # Persisted, so the next process life starts with the right credential.
    assert json.loads((tmp_path / CLUSTER_FILE).read_text())["token"] == "the-permanent-one"


# ----------------------------------------------------------------------
# 4. The gateway routes
# ----------------------------------------------------------------------


def coordinator_client(tmp_path) -> TestClient:
    """A gateway whose registry is the real one, with a fake network."""
    client = FakeClient()
    client.serve("http://10.0.0.12:8081", SPARK_02)
    config = RegistryConfig(data_dir=tmp_path, token="cluster-token", agent_port=8081)
    registry = Registry(config=config, local_profile=SPARK_01, client=client)
    return TestClient(create_app(deps=GatewayDeps(registry=registry)))


def test_install_script_is_served_as_a_shell_script(tmp_path):
    api = coordinator_client(tmp_path)
    response = api.get("/install.sh")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/x-shellscript")
    assert response.text.startswith("#!/bin/sh")
    assert response.text == INSTALL_SH.read_text()


def test_the_served_script_carries_no_credential(tmp_path):
    """It is a static asset. The token is only ever an argv value.

    This is the property that makes serving it on an unauthenticated surface
    acceptable, so it is asserted rather than assumed.
    """
    api = coordinator_client(tmp_path)
    body = api.get("/install.sh").text
    assert "cluster-token" not in body
    assert "ej_" not in body.replace("ej_...", "")


def test_minting_returns_a_command_that_runs_on_another_machine(tmp_path):
    api = coordinator_client(tmp_path)
    body = api.post("/api/enroll", json={}).json()

    assert body["command"].startswith("curl -fsSL http://")
    assert "--join" in body["command"] and "--token" in body["command"]
    # Never localhost: the command is meant for a different box.
    assert "localhost" not in body["command"]
    assert "127.0.0.1" not in body["command"]
    assert SPARK_01.address in body["command"]


def test_the_minted_token_actually_admits(tmp_path):
    """End to end through the wire, not just the store."""
    api = coordinator_client(tmp_path)
    command = api.post("/api/enroll", json={}).json()["command"]
    token = command.rsplit("--token ", 1)[1].strip()

    registry = api.app.state.ctx.deps.registry
    result = run(registry.handle_join(token, SPARK_02, "http://10.0.0.12:8081"))

    assert result["status"] == "member"


def test_listing_enrollments_never_returns_the_secret(tmp_path):
    api = coordinator_client(tmp_path)
    command = api.post("/api/enroll", json={}).json()["command"]
    token = command.rsplit("--token ", 1)[1].strip()

    rows = api.get("/api/enroll").json()

    assert len(rows) == 1
    assert token not in json.dumps(rows)
    assert "token" not in rows[0]
    assert rows[0]["expires_in_s"] > 0


def test_revoking_over_the_wire(tmp_path):
    api = coordinator_client(tmp_path)
    token_id = api.post("/api/enroll", json={}).json()["token_id"]

    assert api.delete(f"/api/enroll/{token_id}").status_code == 204
    assert api.get("/api/enroll").json() == []
    assert api.delete(f"/api/enroll/{token_id}").status_code == 404


def test_an_empty_body_is_the_default_token_not_an_error(tmp_path):
    api = coordinator_client(tmp_path)
    response = api.post("/api/enroll", content=b"")
    assert response.status_code == 200
    assert response.json()["uses_remaining"] == 1


def test_an_out_of_range_ttl_is_refused_with_a_sentence(tmp_path):
    api = coordinator_client(tmp_path)
    response = api.post("/api/enroll", json={"ttl_s": MAX_TTL_S * 2})
    assert response.status_code == 400
    assert "seconds" in response.json()["error"]["message"]


def test_a_registry_without_enrollment_says_so_rather_than_crashing():
    """The day-0 stub registry, and any port that predates this."""
    api = TestClient(create_app())
    response = api.post("/api/enroll", json={})
    assert response.status_code == 501
    assert "not wired up" in response.json()["error"]["message"]
    assert api.get("/api/enroll").json() == []


def test_install_route_sits_above_the_static_mount(tmp_path):
    """Registration order: below the UI mount, `curl | sh` gets index.html."""
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>derate</title>")

    from control_plane.gateway.settings import GatewaySettings

    client = FakeClient()
    config = RegistryConfig(data_dir=tmp_path, token="t", agent_port=8081)
    registry = Registry(config=config, local_profile=SPARK_01, client=client)
    api = TestClient(
        create_app(
            deps=GatewayDeps(registry=registry, settings=GatewaySettings(ui_dir=str(ui))),
            settings=GatewaySettings(ui_dir=str(ui)),
        )
    )

    assert api.get("/").text.startswith("<!doctype html")
    assert api.get("/install.sh").text.startswith("#!/bin/sh")


def test_index_is_never_cached_but_hashed_assets_are(tmp_path):
    """The blank-page failure, pinned.

    Vite deletes the previous bundle on every build and `ui_dir` is served off
    disk, so a tab holding a cached index.html asks for a hash that is gone,
    gets a 404, and renders nothing -- while the server logs a 200 for `/` and
    no /api/* request is ever made to explain it. Starlette sends an ETag and
    no Cache-Control, and without one the browser may skip revalidation
    entirely. Lives beside the mount-order test above because it is the same
    mount and the same class of silent failure.
    """
    from control_plane.gateway.settings import GatewaySettings

    ui = tmp_path / "ui"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text('<!doctype html><script src="/assets/index-abc.js">')
    (ui / "assets" / "index-abc.js").write_text("console.log(1)")

    settings = GatewaySettings(ui_dir=str(ui))
    api = TestClient(create_app(deps=GatewayDeps(settings=settings), settings=settings))

    assert api.get("/").headers["cache-control"] == "no-store, must-revalidate"
    asset = api.get("/assets/index-abc.js")
    assert "immutable" in asset.headers["cache-control"]


# ----------------------------------------------------------------------
# 5. The script
# ----------------------------------------------------------------------


def sh(*args, path=None, home="/nonexistent"):
    env = {**os.environ, "HOME": home}
    if path is not None:
        env["PATH"] = path
    return subprocess.run(
        ["/bin/sh", str(INSTALL_SH), *args],
        capture_output=True, text=True, cwd=REPO_ROOT, env=env,
    )


def gpu_path(tmp_path, present: bool) -> str:
    """A PATH holding only what --dry-run shells out to, plus or minus nvidia-smi.

    The GPU branch is selected by ``command -v nvidia-smi``, so a test that
    inherited the real PATH would assert one thing on a machine with a driver
    and the opposite on one without -- and the branch that matters most is the
    one the machine running the test does not have. ``uname`` is here because
    the Linux check shells out to it before --dry-run returns; nothing else on
    that path leaves the shell.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    real_uname = shutil.which("uname")
    assert real_uname, "install.sh calls uname"
    (bin_dir / "uname").symlink_to(real_uname)
    if present:
        stub = bin_dir / "nvidia-smi"
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    return str(bin_dir)


def fake_ports(tmp_path, *listening):
    """An `ss` on PATH that reports exactly these ports as listening.

    Before it pulls an image or destroys a running node, install.sh asks this
    machine whether $PORT and $AGENT_PORT are free -- and the machine running
    the tests has its own opinion about :8080. On the development box
    something else holds it, which turns every install test into a refusal
    that is correct about the box and says nothing about the script. So the
    answer is stubbed like docker's and curl's, empty by default, and the
    tests about a conflict are the ones that name a port.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    rows = "".join(
        f"LISTEN 0      4096              0.0.0.0:{port}          0.0.0.0:*\n"
        for port in listening
    )
    # netstat too: it is the fallback for a machine without ss, and leaving it
    # real would make this stub depend on which of the two the box happens to
    # have -- the exact thing the docstring above is about.
    for name in ("ss", "netstat"):
        (bin_dir / name).write_text(f"#!/bin/sh\ncat <<'EOF'\n{rows}EOF\n")
        (bin_dir / name).chmod(0o755)
    return bin_dir


def fake_docker(tmp_path, responses=None, listening=(), fail_runs=0):
    """A `docker` on PATH that records its argv and answers canned queries.

    Prepended to the real PATH rather than replacing it: install.sh shells out
    to sed, awk, sort and hostname on the success path, and stubbing all of
    those would be testing the stubs. `curl` is stubbed too so the health probe
    returns immediately instead of waiting out HEALTH_TIMEOUT.

    ``responses`` maps a substring of the joined argv to the stdout for it. The
    first match wins, so put the more specific key first.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "docker.log"
    table = json.dumps(list((responses or {}).items()))

    (bin_dir / "docker").write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"log = {str(log)!r}\n"
        f"table = json.loads({table!r})\n"
        f"fail_runs = {fail_runs}\n"
        "argv = ' '.join(sys.argv[1:])\n"
        "open(log, 'a').write(argv + chr(10))\n"
        # `docker run` refusing the first N times, with something on stderr to
        # be quoted back. The count is read off the log rather than kept in the
        # stub, because each call is its own process.
        "if fail_runs and argv.startswith('run '):\n"
        "    prior = sum(1 for line in open(log) if line.startswith('run '))\n"
        "    if prior <= fail_runs:\n"
        "        sys.stderr.write('docker: Error response from daemon: boom' + chr(10))\n"
        "        sys.exit(125)\n"
        "for key, out in table:\n"
        "    if key in argv:\n"
        "        sys.stdout.write(out)\n"
        "        break\n"
        "sys.exit(0)\n"
    )
    (bin_dir / "docker").chmod(0o755)
    (bin_dir / "curl").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "curl").chmod(0o755)
    fake_ports(tmp_path, *listening)
    return f"{bin_dir}:{os.environ['PATH']}", log


def docker_calls(log) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def test_install_replaces_a_node_it_finds_by_its_label(tmp_path):
    """Re-running the command upgrades in place rather than colliding.

    A second node on one machine fights the first for :8080, :8081 and the
    data volume, and the loser's failure is a port bind error three layers
    down from anything the operator typed.
    """
    path, log = fake_docker(tmp_path, {"ps -aq --filter label=": "c0ffee\n"})
    result = sh("--join", "http://10.0.0.1:8080", "--token", "ej_x", path=path)

    calls = docker_calls(log)
    assert result.returncode == 0, result.stderr
    assert any(c.startswith("rm -f c0ffee") for c in calls), calls
    # The volume holds the cluster token and the node registry. An upgrade that
    # forgot which cluster the machine was in would be a reinstall.
    assert not any("volume rm" in c for c in calls), calls


def test_install_pulls_before_it_destroys_anything(tmp_path):
    """The other order leaves a machine with nothing running when the pull fails."""
    path, log = fake_docker(tmp_path, {"ps -aq --filter label=": "c0ffee\n"})
    sh("--join", "http://10.0.0.1:8080", "--token", "ej_x", path=path)

    calls = docker_calls(log)
    pulled = next(i for i, c in enumerate(calls) if c.startswith("pull "))
    removed = next(i for i, c in enumerate(calls) if c.startswith("rm -f c0ffee"))
    assert pulled < removed, calls


def test_install_replaces_a_node_someone_started_by_hand(tmp_path):
    """Named anything, or nothing -- if it came from this image it is a node.

    The name filter alone missed a container started with a plain `docker run`,
    which is how the worker on the development box was running.
    """
    path, log = fake_docker(tmp_path, {
        "ps -aq --filter label=": "",
        "ps -aq --filter name=": "",
        "images -q ghcr.io": "sha256:aaa\n",
        "ps -aq --filter ancestor=sha256:aaa": "beefbeef\n",
    })
    result = sh("--join", "http://10.0.0.1:8080", "--token", "ej_x", path=path)

    assert result.returncode == 0, result.stderr
    assert any(c.startswith("rm -f beefbeef") for c in docker_calls(log))


def test_install_reclaims_superseded_images_but_never_the_running_one(tmp_path):
    path, log = fake_docker(tmp_path, {
        "images --no-trunc -q ghcr.io": "sha256:new\nsha256:old\n",
        "inspect -f {{.Image}}": "sha256:new\n",
    })
    sh("--join", "http://10.0.0.1:8080", "--token", "ej_x", path=path)

    calls = docker_calls(log)
    assert "rmi sha256:old" in calls, calls
    assert "rmi sha256:new" not in calls, calls


def test_keep_images_leaves_superseded_images_alone(tmp_path):
    path, log = fake_docker(tmp_path, {
        "images --no-trunc -q ghcr.io": "sha256:new\nsha256:old\n",
        "inspect -f {{.Image}}": "sha256:new\n",
    })
    sh("--keep-images", "--join", "http://10.0.0.1:8080", "--token", "ej_x", path=path)

    assert not any(c.startswith("rmi ") for c in docker_calls(log))


def test_install_refuses_a_port_something_else_already_holds(tmp_path):
    """Refuse before the pull, rather than crash-loop after it.

    The container is on host networking, so :8080 is the machine's own port and
    whatever already has it wins. The node then dies in uvicorn with EADDRINUSE
    -- and because `--restart unless-stopped` puts it back immediately, a crash
    loop reads as a running container, so the wait at the end of the script sat
    out its whole timeout before printing a traceback whose only useful line
    had already scrolled past the tail. This is that whole failure, asked as a
    question first: nothing is pulled, nothing is removed.
    """
    path, log = fake_docker(tmp_path, listening=(8080,))
    result = sh(path=path)

    calls = docker_calls(log)
    assert result.returncode != 0
    assert not any(c.startswith("pull ") for c in calls), calls
    assert not any(c.startswith("rm -f") for c in calls), calls
    assert "port 8080 is already in use" in result.stderr, result.stderr
    assert "--port" in result.stderr, result.stderr


def test_a_busy_agent_port_names_the_flag_that_moves_it(tmp_path):
    """--port would not have helped, and being sent back twice is the failure."""
    path, _ = fake_docker(tmp_path, listening=(8081,))
    result = sh(path=path)

    assert result.returncode != 0
    assert "port 8081 is already in use" in result.stderr, result.stderr
    assert "--agent-port" in result.stderr, result.stderr


def test_both_busy_ports_are_named_in_one_run(tmp_path):
    path, _ = fake_docker(tmp_path, listening=(8080, 8081))
    result = sh(path=path)

    assert "port 8080 is already in use" in result.stderr, result.stderr
    assert "port 8081 is already in use" in result.stderr, result.stderr


def test_the_pull_is_not_silent(tmp_path):
    """Docker's own output is the only thing that says why a pull failed.

    It was going to /dev/null along with the progress, which made a slow pull
    and a hung one identical on screen and hid "manifest unknown" entirely.
    """
    path, _ = fake_docker(tmp_path, {"pull ": "latest: Pulling from pizzaman213/derate/node\n"})
    result = sh(path=path)

    assert "Pulling from pizzaman213/derate/node" in result.stdout + result.stderr


def test_a_restarting_node_is_diagnosed_instead_of_waited_out(tmp_path):
    """A crash loop is not a slow start, and docker reports both as running.

    The restart counter is what separates them. The bind error is then named
    rather than shown: uvicorn's OSError is some ninety frames of asyncio
    traceback above the end of the log it produces, so the tail this used to
    print carried every frame and none of the cause.
    """
    bind = (
        "OSError: [Errno 98] error while attempting to bind on address "
        "('0.0.0.0', 8080): [errno 98] address already in use"
    )
    path, _ = fake_docker(tmp_path, {
        "inspect -f {{.RestartCount}}": "4\n",
        "logs ": bind + "\n" + "\n".join("  File asyncio" for _ in range(120)) + "\n",
    })
    # The health probe never answers: that is the state this is about.
    (tmp_path / "bin" / "curl").write_text("#!/bin/sh\nexit 1\n")

    started = time.monotonic()
    result = sh(path=path)
    elapsed = time.monotonic() - started

    assert result.returncode != 0
    assert "restarting in a loop" in result.stderr, result.stderr
    assert "cannot bind port 8080" in result.stderr, result.stderr
    assert "--port" in result.stderr, result.stderr
    # HEALTH_TIMEOUT is 60s. Waiting it out for a node that is already dead is
    # the bug, so the speed is the assertion.
    assert elapsed < 20, elapsed


def test_the_script_is_valid_posix_sh():
    assert subprocess.run(["sh", "-n", str(INSTALL_SH)]).returncode == 0


def test_the_script_is_executable():
    assert os.access(INSTALL_SH, os.X_OK)


def test_dry_run_composes_the_main_node_command(tmp_path):
    result = sh("--dry-run", path=gpu_path(tmp_path, present=False))
    assert result.returncode == 0, result.stderr
    line = result.stdout.strip()
    assert line.startswith("docker run --gpus all --pid=host -d --name derate")
    # Not optional: mDNS is multicast and does not cross a bridge.
    assert "--network host" in line
    assert "-v derate:/data" in line
    assert "--restart unless-stopped" in line
    # A main node names no coordinator and holds no token.
    assert "DERATE_JOIN" not in line
    assert "DERATE_TOKEN" not in line


def test_the_gpu_is_requested_even_with_no_nvidia_smi_on_the_host_path(tmp_path):
    """Absence of a host nvidia-smi does not withhold the request.

    The container toolkit injects nvidia-smi into the container from the
    driver, so whether the host has that binary on its PATH does not determine
    whether the container can get a GPU -- a working driver behind a non-login
    shell's PATH would have been silently downgraded to an unidentified node,
    which is the quiet failure the flag exists to prevent. Docker is asked and
    Docker answers; install.sh retries without the flags only when the run
    actually fails. Only --no-gpu withholds the request.
    """
    line = sh("--dry-run", path=gpu_path(tmp_path, present=False)).stdout.strip()
    assert "--gpus all" in line
    assert "--pid=host" in line


def test_dry_run_passes_the_gpu_when_the_host_has_a_driver(tmp_path):
    """The probe is nvidia-smi, so a container without one is an UNKNOWN node.

    It joins, reports healthy, and shows zeros for power, temperature and
    utilisation while the coordinator records "device class is not recognized".
    Nothing raises anywhere along that path, which is exactly why the flag has
    to be asserted rather than trusted.
    """
    line = sh("--dry-run", path=gpu_path(tmp_path, present=True)).stdout.strip()
    assert "--gpus all" in line
    # GB10 reports [N/A] for every aggregate FB memory field, leaving
    # --query-compute-apps as the only way to tell a model from the desktop --
    # and nvidia-smi only counts processes in its own PID namespace.
    assert "--pid=host" in line


def test_no_gpu_withholds_the_flags_on_a_machine_that_has_one(tmp_path):
    line = sh(
        "--dry-run", "--no-gpu", path=gpu_path(tmp_path, present=True)
    ).stdout.strip()
    assert "--gpus" not in line
    assert "--pid=host" not in line
    assert line.startswith("docker run -d --name derate")


def test_dry_run_composes_the_join_command():
    result = sh(
        "--dry-run", "--join", "http://10.0.0.11:8080",
        "--token", "ej_abcd1234_secret", "--name", "spark-02",
    )
    assert result.returncode == 0, result.stderr
    line = result.stdout.strip()
    assert "-e DERATE_JOIN=http://10.0.0.11:8080" in line
    assert "-e DERATE_TOKEN=ej_abcd1234_secret" in line
    assert "-e DERATE_NODE_ID=spark-02" in line


def test_equals_form_flags_work_too():
    result = sh("--dry-run", "--join=http://10.0.0.11:8080", "--token=abc")
    assert "-e DERATE_JOIN=http://10.0.0.11:8080" in result.stdout


def test_a_token_with_nowhere_to_present_it_is_refused():
    result = sh("--token", "abc")
    assert result.returncode != 0
    assert "--join" in result.stderr


def test_an_unknown_flag_does_not_silently_install_the_wrong_thing():
    result = sh("--jion", "http://x")
    assert result.returncode != 0
    assert "unknown option" in result.stderr


def test_missing_docker_names_the_command_to_fix_it(tmp_path):
    """Refusing beats silently installing a daemon from a piped script."""
    # A PATH with everything the script needs to reach the docker check, and
    # no docker. Emptying PATH outright would fail earlier, at `uname`, and
    # prove nothing about this branch.
    fake_path = tmp_path / "bin"
    fake_path.mkdir()
    for tool in ("uname", "id", "sh"):
        found = shutil.which(tool)
        if found:
            os.symlink(found, fake_path / tool)
    assert shutil.which("docker", path=str(fake_path)) is None

    # start_new_session, so the child has no controlling terminal. Without it
    # this test inherits pytest's, install.sh finds a /dev/tty to ask on, and
    # the whole suite blocks on a question nobody is there to answer -- for
    # every developer who runs it from a terminal, and for nobody who runs it
    # from CI. It is also the property being asserted: nobody to ask is a no.
    result = subprocess.run(
        ["/bin/sh", str(INSTALL_SH)],
        capture_output=True, text=True, start_new_session=True,
        env={"PATH": str(fake_path), "HOME": "/nonexistent"},
    )
    assert result.returncode != 0
    assert "get.docker.com" in result.stderr
    assert "--install-docker" in result.stderr
    assert "[y/N]" not in result.stderr, "asked a question with no terminal to ask on"


def docker_less_path(tmp_path):
    """A PATH that reaches the docker check with no docker on it, and a curl
    that records rather than installs.

    Symlinks rather than an empty PATH: the script runs `uname` before it looks
    for docker, so emptying PATH fails earlier and proves nothing about this
    branch.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in ("sh", "uname", "id", "sed", "grep", "cat", "sort", "rm"):
        found = shutil.which(tool)
        if found and not (bin_dir / tool).exists():
            os.symlink(found, bin_dir / tool)
    log = tmp_path / "curl.log"
    (bin_dir / "curl").write_text(
        "#!/bin/sh\n"
        f"echo \"$*\" >> {log}\n"
        # What get.docker.com would hand to `sh`. Saying so out loud is how the
        # yes-path proves it ran the installer and not something else.
        "echo 'echo FAKE-DOCKER-INSTALLER-RAN'\n"
    )
    (bin_dir / "curl").chmod(0o755)
    assert shutil.which("docker", path=str(bin_dir)) is None
    return bin_dir, log


def sh_on_a_tty(*args, path, answer, timeout=20.0):
    """Run install.sh with a controlling terminal, and answer its question.

    `curl ... | sh` leaves stdin as the pipe carrying the script, so the
    question goes to /dev/tty -- which a process only has when it has a
    controlling terminal. subprocess.run never gives it one, so this branch is
    invisible to a plain runner; pty.fork does, and it is stdlib, so this does
    not depend on util-linux's `script` being installed on whatever runs it.
    """
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover -- the child is replaced immediately
        try:
            os.execve("/bin/sh", ["/bin/sh", str(INSTALL_SH), *args],
                      {"PATH": str(path), "HOME": "/nonexistent"})
        except BaseException:
            os._exit(127)
    os.write(fd, answer.encode())  # the line discipline holds it until read
    out, deadline = b"", time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not select.select([fd], [], [], 0.5)[0]:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:  # the child exited and closed the slave
            break
        if not chunk:
            break
        out += chunk
    os.close(fd)
    _, status = os.waitpid(pid, 0)
    return out.decode(errors="replace"), status


@pytest.mark.filterwarnings("ignore:This process .*is multi-threaded")
def test_declining_the_docker_question_installs_nothing(tmp_path):
    """Asked, and no is an answer. The refusal is the one it prints unasked."""
    bin_dir, curl_log = docker_less_path(tmp_path)

    out, status = sh_on_a_tty(path=bin_dir, answer="n\n")

    assert status != 0
    assert "[y/N]" in out, out
    assert not curl_log.exists(), curl_log.read_text()
    assert "--install-docker" in out, out


@pytest.mark.filterwarnings("ignore:This process .*is multi-threaded")
def test_answering_yes_runs_the_docker_installer(tmp_path):
    """The question is the whole point: this is a daemon, on their machine."""
    bin_dir, curl_log = docker_less_path(tmp_path)

    out, status = sh_on_a_tty(path=bin_dir, answer="y\n")

    assert "[y/N]" in out, out
    assert "get.docker.com" in curl_log.read_text()
    assert "FAKE-DOCKER-INSTALLER-RAN" in out, out
    # The fake install leaves no daemon behind, so the script still stops --
    # having said which of the two things went wrong.
    assert status != 0
    assert "not responding yet" in out, out


def test_the_model_cache_is_mounted_at_the_path_the_host_calls_it(tmp_path):
    """One path, agreed on both sides of the socket.

    sparkrun resolves the cache as ${HF_HOME:-$HOME/.cache/huggingface} and
    hands the answer to the HOST's daemon as a bind mount for the runtime
    container. Mounting the operator's cache at /root/.cache/huggingface in
    here -- which is what this did -- means sparkrun answers
    /root/.cache/huggingface, the host creates that empty directory, and every
    weight is downloaded again into a place the Storage tab cannot see.
    """
    cache = tmp_path / ".cache" / "huggingface"
    cache.mkdir(parents=True)

    out = sh("--dry-run", home=str(tmp_path)).stdout

    assert f"-v {cache}:{cache}" in out, out
    assert f"-e HF_HOME={cache}" in out, out
    assert ":/root/.cache/huggingface" not in out, out


def test_no_model_cache_on_the_host_mounts_nothing(tmp_path):
    """A bind mount of a missing path creates a root-owned directory."""
    out = sh("--dry-run", home=str(tmp_path)).stdout

    assert "huggingface" not in out, out
    assert "HF_HOME" not in out, out


def test_the_docker_socket_is_mounted_or_the_refusal_is_explained():
    """Without the socket a node plans, writes a recipe and dies in sparkrun.

    The client in the image has no daemon of its own -- it is the CLI only --
    so this mount is what separates a node that can serve a model from one that
    joins, reports its hardware and fails at [3/6] Distributing resources.

    Both branches are asserted rather than skipping on a machine without a
    socket: the message is the whole value of the branch that has none.
    """
    result = sh("--dry-run")

    if pathlib.Path("/var/run/docker.sock").is_socket():
        assert "-v /var/run/docker.sock:/var/run/docker.sock" in result.stdout
    else:
        assert "not be able" in result.stderr and "launch a model" in result.stderr


def test_a_failed_start_is_retried_with_the_gpu_flags_still_on(tmp_path):
    """A first `docker run` fails for reasons that are not the GPU.

    This ladder used to drop --gpus in the very step that removed the leftover
    container, so the removal fixed the run and the GPU took the blame -- and
    the machine was recorded as unidentified hardware, which the planner will
    not place work on, under a message about the container toolkit. spark-26af
    came up exactly that way on an install whose host ran `docker run --gpus
    all` correctly a minute later.
    """
    path, log = fake_docker(tmp_path, fail_runs=1)

    result = sh(path=path)

    runs = [c for c in docker_calls(log) if c.startswith("run ")]
    assert result.returncode == 0, result.stderr
    assert len(runs) == 2, runs
    assert all("--gpus all" in r for r in runs), runs
    assert "could not pass the GPU" not in result.stderr, result.stderr


def test_the_gpu_is_only_dropped_after_two_tries_and_docker_is_quoted(tmp_path):
    """When it really is the GPU, say so -- and say what Docker said.

    The error was written to a temp file and deleted unread on the fallback
    path, so a node downgraded itself and nothing on the machine recorded why.
    """
    path, log = fake_docker(tmp_path, fail_runs=2)

    result = sh(path=path)

    runs = [c for c in docker_calls(log) if c.startswith("run ")]
    assert result.returncode == 0, result.stderr
    assert len(runs) == 3, runs
    assert "--gpus all" in runs[0] and "--gpus all" in runs[1], runs
    assert "--gpus all" not in runs[2], runs[2]
    assert "Docker said" in result.stderr, result.stderr
    assert "Error response from daemon: boom" in result.stderr, result.stderr


def test_help_documents_both_shapes():
    result = sh("--help")
    assert result.returncode == 0
    assert "--join" in result.stdout and "--token" in result.stdout
    assert "main node" in result.stdout


def test_the_public_url_names_the_repo_the_ui_will_show():
    assert enroll_api.PUBLIC_INSTALL_URL.endswith("/install.sh")
    assert enroll_api.INSTALL_REPO in enroll_api.PUBLIC_INSTALL_URL
    assert enroll_api.INSTALL_BRANCH in enroll_api.PUBLIC_INSTALL_URL
