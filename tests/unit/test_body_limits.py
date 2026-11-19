"""An open route stops reading a body at 64 KiB.

``POST /api/auth/login``, ``POST /api/setup/admin`` and ``POST /api/nodes/join``
answer anyone. They used to ``await request.json()``, which buffers whatever
arrives before any size check, and uvicorn sets no body limit of its own.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from control_plane.gateway import create_app

pytestmark = pytest.mark.raw_http

CHUNK = 16 * 1024
TOTAL_CHUNKS = 64  # 1 MiB offered


def _post(app, path: str, headers: list[tuple[bytes, bytes]] = ()) -> tuple[int, int, dict]:
    """Drive *app* with a 1 MiB body in 16 KiB chunks; return the status, how
    many chunks the app pulled, and the JSON answer."""
    pulled = 0
    sent: list[dict] = []

    async def receive():
        nonlocal pulled
        pulled += 1
        more = pulled < TOTAL_CHUNKS
        return {"type": "http.request", "body": b" " * CHUNK, "more_body": more}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"host", b"localhost"), (b"content-type", b"application/json"), *headers],
        "client": ("127.0.0.1", 50000),
        "server": ("localhost", 80),
    }

    async def run():
        async with app.router.lifespan_context(app):
            await app(scope, receive, send)

    asyncio.run(run())
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], pulled, json.loads(body)


@pytest.mark.parametrize("path", ["/api/auth/login", "/api/setup/admin", "/api/nodes/join"])
def test_an_oversized_body_on_an_open_route_is_413_and_not_read_in_full(tmp_path, monkeypatch, path):
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    status, pulled, answer = _post(create_app(), path)
    assert status == 413
    assert answer["error"]["code"] == "payload_too_large"
    # Stopped at the limit: 64 KiB is four chunks, plus the one that crossed it.
    assert pulled <= 5 < TOTAL_CHUNKS


def _signed_in(app) -> tuple[list[tuple[bytes, bytes]], dict]:
    from fastapi.testclient import TestClient

    client = TestClient(app, client=("127.0.0.1", 50001))
    r = client.post(
        "/api/setup/admin",
        json={"username": "bodyadmin", "email": "b@derate.test", "password": "quiet-harbor-lamp-7719"},
    )
    assert r.status_code == 200, r.text
    name, value = next((k, v) for k, v in r.cookies.items() if k.endswith("derate_session"))
    return [(b"cookie", f"{name}={value}".encode())], r.json()


@pytest.mark.parametrize(
    "path",
    ["/api/keys/{key_id}/disable", "/api/orgs/{org_id}/keys/disable", "/api/providers"],
)
def test_an_oversized_body_on_an_admin_route_is_413_too(tmp_path, monkeypatch, path):
    """GA-6: a session does not license an unbounded body either."""
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    app = create_app()
    cookie, setup = _signed_in(app)
    path = path.format(key_id=setup["key"]["key_id"], org_id=setup["org"]["org_id"])
    status, pulled, answer = _post(app, path, cookie)
    assert status == 413, answer
    assert answer["error"]["code"] == "payload_too_large"
    assert pulled <= 5 < TOTAL_CHUNKS
