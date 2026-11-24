"""A raw ``derate-`` key appears nowhere it was not handed out.

A real key is sent over every /v1 route against an app whose data and log
directories are this test's own, with the log files installed and telemetry
on. Then every byte the gateway wrote -- ``node.log``, ``proxy.log``, the
telemetry journal and archive, every store -- is searched for the raw key and
for its secret half, and so is every ``/api/keys*`` answer. Each route is
listed by ``.omp/work/auth-keys/sets/v1_routes.json``, never by hand.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from control_plane import logfiles
from control_plane.gateway import create_app
from tests.unit.test_gateway import (
    FakeDeployments,
    FakeProviders,
    build_deps,
    make_deployment,
    make_provider,
)

pytestmark = pytest.mark.raw_http

SETS = Path(__file__).resolve().parents[2] / ".omp" / "work" / "auth-keys" / "sets"
V1_ROUTES = list(json.loads((SETS / "v1_routes.json").read_text())["members"])
ADMIN = {"username": "leak-admin", "email": "leak-admin@derate.test", "password": "leak-harbor-lamp-123456"}


def _echo(request: httpx.Request) -> httpx.Response:
    """A backend that echoes every header it got -- the way a careless
    upstream would leak whatever it was handed."""
    request.read()
    payload = {
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        "echo": dict(request.headers),
    }
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        stream=httpx.ByteStream(json.dumps(payload).encode()),
    )


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    data, logs = tmp_path / "data", tmp_path / "logs"
    data.mkdir()
    monkeypatch.setenv("DERATE_DATA_DIR", str(data))
    monkeypatch.setenv("DERATE_LOG_DIR", str(logs))
    root = logging.getLogger()
    logfiles.uninstall(root)
    before = root.level
    root.setLevel(logging.DEBUG)
    assert logfiles.install(level="DEBUG", directory=logs, root=root) == logs
    yield data, logs
    logfiles.uninstall(root)
    root.setLevel(before)


def test_a_raw_key_is_written_nowhere_and_listed_nowhere(isolated):
    data, logs = isolated
    deps = build_deps(
        deployments=FakeDeployments(
            [make_deployment("d-1", "local-m", backend_url="http://local-backend/v1")]
        ),
        providers=FakeProviders(
            [make_provider("openrouter", base_url="http://cloud/v1", served_name="cloud-m")]
        ),
    )
    app = create_app(deps, http_client=httpx.AsyncClient(transport=httpx.MockTransport(_echo)))
    assert app.state.ctx.telemetry.enabled, "telemetry must be on for this test to mean anything"

    with TestClient(app) as client:
        setup = client.post("/api/setup/admin", json=ADMIN)
        assert setup.status_code == 200
        raw = setup.json()["secret_once"]
        secret = raw.split("_", 1)[1]  # derate-<id>_<secret>; the secret may hold "_"
        key_id = setup.json()["key"]["key_id"]
        cookie = {"Cookie": f"derate_session={setup.cookies['derate_session']}"}
        client.cookies.clear()
        bearer = {"Authorization": f"Bearer {raw}"}

        answers: list[str] = []
        for route in V1_ROUTES:
            method, path = route.split(" ", 1)
            if method == "WEBSOCKET":
                # Accepted by the key gate, then refused by the relay (the
                # model is local); either way the key went through the gate.
                try:
                    with client.websocket_connect(f"{path}?model=local-m", headers=bearer):
                        pass
                except Exception:
                    pass
                continue
            kwargs: dict = {"headers": bearer}
            if path == "/v1/audio/transcriptions":
                kwargs["files"] = {"file": ("a.wav", b"RIFF0000", "audio/wav")}
                kwargs["data"] = {"model": "local-m"}
            elif path == "/v1/audio/voices":
                kwargs["params"] = {"model": "local-m"}
            elif method == "POST":
                kwargs["json"] = {
                    "model": "local-m",
                    "messages": [{"role": "user", "content": "hi"}],
                    "input": "hi",
                    "prompt": "hi",
                }
            r = client.request(method, path, **kwargs)
            assert r.status_code != 401, (route, r.text)
            answers.append(r.text)
        # The chat answer echoes what the backend received: no key in it.
        for text in answers:
            assert raw not in text and secret not in text

        # A key that leaks through an exception message is scrubbed on its
        # way into the files, whoever logs it.
        logging.getLogger("gateway.proxy").warning("upstream said: invalid key %s", raw)

        listings = [
            client.get("/api/keys", headers=cookie),
            client.get(f"/api/keys/{key_id}", headers=cookie),
            client.get(f"/api/keys/{key_id}/requests", headers=cookie),
            client.get("/api/keys?q=default", headers=cookie),
            client.get("/api/audit", headers=cookie),
            client.get("/api/usage?group_by=key_id", headers=cookie),
            client.get("/api/requests", headers=cookie),
            client.get("/v1/key", headers=bearer),
        ]
        for r in listings:
            assert raw not in r.text and secret not in r.text, r.url

    for handler in logging.getLogger().handlers:
        handler.flush()
    written = [p for root in (data, logs) for p in root.rglob("*") if p.is_file()]
    names = {p.name for p in written}
    assert {"node.log", "proxy.log", "journal.db", "archive.db", "keys.db", "auth.db"} <= names, names
    for path in written:
        blob = path.read_bytes()
        assert raw.encode() not in blob, path
        assert secret.encode() not in blob, path
    node_log = (logs / "node.log").read_text()
    assert re.search(r"upstream said: invalid key \*\*\*", node_log)
