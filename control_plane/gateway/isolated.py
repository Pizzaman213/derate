"""The stub gateway, sealed off from this machine, for the agents' stub-UI tool.

``python -m control_plane.gateway.main --isolated ROOT --ui-dir DIR`` lands
here. The parent is ``.omp/stubui/server.mjs``; see ``.omp/stubui/README.md``.
What is served is the same day-0 stub surface ``main.py`` runs (fixtures from
``stubs.py``); what this module adds is that nothing it does can reach the
live coordinator's data, its audit chain, docker, the hub, or the network.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
from collections.abc import MutableMapping
from pathlib import Path

import uvicorn

from control_plane import paths
from control_plane.contracts.auth import SESSION_COOKIE

from .app import create_app
from .settings import GatewaySettings

# Printed by both the plain stub (main.py) and this one: the real system is
# python3 -m control_plane.node, and a fixture gateway that looks real is the
# failure the gateway package guards against.
BANNER = "STUB SURFACE -- fixture data; the real system is python3 -m control_plane.node"

# Nothing a stub may reach: every sparkrun call fails, docker has no daemon,
# the hub never downloads, and anything not on loopback (the stub deployments'
# fixture backends are LAN addresses) goes to a port nothing listens on. Sent
# in the handshake so the browser is fenced to the same address.
DEAD_PROXY = "http://127.0.0.1:9"

# Every variable family that can name live state or a credential. Removed, not
# defaulted, before the fenced values are set.
_STRIPPED = ("DERATE_", "HF_", "HUGGINGFACE_", "HUGGING_FACE_")

# What a derate data dir or its logs dir holds. A ROOT holding any of these is
# somebody's state, not a fresh directory.
_STATE = ("logs/audit", "data/auth.db", "auth.db", "audit")


def isolate(root: Path, env: MutableMapping[str, str] = os.environ) -> None:
    """Seal this process off from the machine's derate state, under *root*.

    An agent's shell keeps its exports, so the caller's environment is hostile
    input: an inherited ``DERATE_LOG_DIR`` would fork the live audit chain,
    ``DERATE_AUDIT_SINK`` would ship stub rows to the operator's sink,
    ``HUGGINGFACE_HUB_CACHE`` (read before ``HF_HOME``) names the real cache,
    and ``DERATE_API_TOKEN`` would lock the stub's own screens. Then only the
    fenced values are set, and the resolved paths are checked, not trusted.

    *root* must hold no derate state already: a hand-typed live data or logs
    dir as ROOT would put this stub's files beside the real ones.
    """
    root = root.resolve()
    if any((root / p).exists() for p in _STATE):
        raise SystemExit(f"isolated: {root} already holds derate state; give a fresh directory")
    for name in [n for n in env if n.startswith(_STRIPPED) or n.lower().endswith("_proxy")]:
        del env[name]
    env.update(
        {
            "DERATE_DATA_DIR": str(root / "data"),
            "DERATE_LOG_DIR": str(root / "logs"),
            "DERATE_LOG_LEVEL": "WARNING",
            "DERATE_AUTO_ADOPT_CONTAINERS": "0",
            "DERATE_AUTO_ADOPT_RUNTIMES": "0",
            "DERATE_GEOIP_DOWNLOAD": "0",
            "DERATE_GEOIP_TAILSCALE": "0",
            "DERATE_OFFLINE": "1",
            "DERATE_SPARKRUN_BIN": "/bin/false",
            "HF_HOME": str(root / "hf"),
            "HF_HUB_OFFLINE": "1",
            "DOCKER_HOST": "unix:///nonexistent/docker.sock",
            "http_proxy": DEAD_PROXY,
            "https_proxy": DEAD_PROXY,
            "HTTP_PROXY": DEAD_PROXY,
            "HTTPS_PROXY": DEAD_PROXY,
            "no_proxy": "127.0.0.1,localhost",
            "NO_PROXY": "127.0.0.1,localhost",
        }
    )
    for label, where in (("data", paths.data_dir(env)), ("logs", paths.logs_dir(env))):
        if not where.resolve().is_relative_to(root):
            raise SystemExit(f"isolated: the {label} dir resolved to {where}, outside {root}")


def _first_admin(app) -> str:
    """The session cookie of a fresh admin, made through the real setup route
    BEFORE anything binds (the pattern of tests/load/gateway_app.py), so no
    other local process can claim the open route first. The password is
    random and dropped: the cookie is the only credential that leaves. The
    loopback client address is the one the browser will use, so the session
    is not flagged as having moved."""
    from fastapi.testclient import TestClient

    body = {"username": "agent", "email": "agent@stub.invalid", "password": secrets.token_urlsafe(24)}
    client = TestClient(app, client=("127.0.0.1", 0))
    response = client.post("/api/setup/admin", json=body, headers={"host": "localhost"})
    cookie = response.cookies.get(SESSION_COOKIE)
    if response.status_code != 200 or not cookie:
        raise SystemExit(f"isolated: setup answered {response.status_code}: {response.text[:300]}")
    return cookie


def serve_isolated(root: Path, ui_dir: str) -> None:
    """Serve the stub on an OS-chosen loopback port and say where on stdout.

    stdout carries exactly one JSON line -- ``port``, ``cookie_name``,
    ``cookie`` and the ``proxy`` the browser must use -- and only once the
    socket is LISTENING, so the parent never dials a port that refuses. The
    banner and every log line go to stderr. The process exits when its stdin
    closes, so a parent that dies any way at all, ``kill -9`` included, takes
    this one with it.
    """
    from control_plane.node import _bind, _config

    isolate(root)
    print(BANNER, file=sys.stderr, flush=True)
    app = create_app(settings=GatewaySettings(host="127.0.0.1", port=0, ui_dir=ui_dir))
    cookie = _first_admin(app)
    sock = _bind("127.0.0.1", 0)
    sock.listen(2048)
    server = uvicorn.Server(_config(app, host="127.0.0.1", port=0))

    def _exit_on_eof() -> None:
        while sys.stdin.buffer.read(4096):
            pass
        server.should_exit = True

    threading.Thread(target=_exit_on_eof, name="stdin-eof", daemon=True).start()
    handshake = {"port": sock.getsockname()[1], "cookie_name": SESSION_COOKIE, "cookie": cookie, "proxy": DEAD_PROXY}
    print(json.dumps(handshake), flush=True)
    server.run(sockets=[sock])
