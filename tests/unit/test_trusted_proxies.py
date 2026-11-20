"""2.2: ``DERATE_TRUSTED_PROXIES`` is parsed strictly, and an ignored
``X-Forwarded-For`` is said out loud once per peer."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from control_plane.gateway import create_app
from control_plane.gateway.settings import trusted_proxies, uvicorn_proxy_kwargs

pytestmark = pytest.mark.raw_http


@pytest.mark.parametrize("value", ["*", "0.0.0.0/0", "::/0", "10.0.0.1, *"])
def test_a_catch_all_is_refused(value):
    with pytest.raises(ValueError, match="DERATE_TRUSTED_PROXIES: a catch-all network is not allowed"):
        uvicorn_proxy_kwargs({"DERATE_TRUSTED_PROXIES": value})


@pytest.mark.parametrize("value", ["garbage", "10.0.0.1/33", "10.0.0.1/8", "proxy.lan"])
def test_a_malformed_entry_is_refused(value):
    with pytest.raises(ValueError, match="DERATE_TRUSTED_PROXIES: .* is not an IP address or CIDR"):
        trusted_proxies({"DERATE_TRUSTED_PROXIES": value})


def test_valid_entries_pass():
    env = {"DERATE_TRUSTED_PROXIES": " 10.0.0.0/8 , 192.168.1.5,fd00::/8 "}
    assert trusted_proxies(env) == ("10.0.0.0/8", "192.168.1.5", "fd00::/8")
    # uvicorn never rewrites the client address: derate resolves it itself.
    assert uvicorn_proxy_kwargs(env) == {"proxy_headers": False}
    assert uvicorn_proxy_kwargs({}) == {"proxy_headers": False}


@pytest.mark.parametrize(
    "peer, xff, expected",
    [
        ("10.0.0.5", "198.51.100.7", "198.51.100.7"),
        # Right to left: the first hop outside the trusted networks wins.
        ("10.0.0.5", "127.0.0.1, 198.51.100.7, 10.0.0.9", "198.51.100.7"),
        # Every hop trusted: the header chose nothing; the socket peer stands.
        ("10.0.0.5", "127.0.0.1, 10.0.0.7", "10.0.0.5"),
        ("10.0.0.5", "", "10.0.0.5"),
        ("10.0.0.5", None, "10.0.0.5"),
        ("10.0.0.5", "garbage, 198.51.100.7", "10.0.0.5"),
        # An untrusted peer's header is ignored entirely.
        ("203.0.113.4", "127.0.0.1", "203.0.113.4"),
        (None, "127.0.0.1", None),
    ],
)
def test_forwarded_client_walks_right_to_left(peer, xff, expected):
    from control_plane.gateway.authz import forwarded_client
    from control_plane.gateway.settings import GatewaySettings

    nets = GatewaySettings(trusted_proxies=("10.0.0.0/8", "127.0.0.0/8")).trusted_networks
    assert forwarded_client(peer, xff, nets) == expected


def test_a_loopback_hop_from_a_lan_peer_is_not_believed():
    from control_plane.gateway.authz import forwarded_client
    from control_plane.gateway.settings import GatewaySettings

    nets = GatewaySettings(trusted_proxies=("10.0.0.0/8",)).trusted_networks
    assert forwarded_client("10.0.0.5", "127.0.0.1, 10.0.0.7", nets) == "10.0.0.5"
    assert forwarded_client("10.0.0.5", "::1", nets) == "10.0.0.5"
    # A proxy on this machine may forward a client from anywhere.
    loop = GatewaySettings(trusted_proxies=("127.0.0.0/8",)).trusted_networks
    assert forwarded_client("127.0.0.1", "203.0.113.9", loop) == "203.0.113.9"


class _Recorder:
    def __init__(self) -> None:
        self.rows = []

    def access(self, record) -> None:
        self.rows.append(record)

    def sample(self, *a) -> None: ...
    def request(self, *a) -> None: ...
    def event(self, *a) -> None: ...
    def log(self, *a) -> None: ...


def _proxied_app(tmp_path, monkeypatch):
    """The app as uvicorn serves it with ``DERATE_TRUSTED_PROXIES=10.0.0.0/8``:
    wrapped in uvicorn's proxy-header middleware exactly when
    ``uvicorn_proxy_kwargs`` asks for it."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DERATE_TRUSTED_PROXIES", "10.0.0.0/8")
    app = create_app()
    recorder = _Recorder()
    app.state.ctx.sink = recorder
    kwargs = uvicorn_proxy_kwargs()
    served = app
    if kwargs.get("proxy_headers"):
        served = ProxyHeadersMiddleware(app, trusted_hosts=kwargs["forwarded_allow_ips"])
    return served, recorder


def test_a_trusted_lan_client_cannot_forge_loopback_in_the_access_log(tmp_path, monkeypatch):
    """GA-1: a client inside the trusted CIDR naming 127.0.0.1 in
    X-Forwarded-For is still the LAN host it is. Setup no longer cares --
    it is open to any client until the first admin exists -- but the
    access log must name the real client, not the forged loopback."""
    app, recorder = _proxied_app(tmp_path, monkeypatch)
    client = TestClient(app, client=("10.0.0.5", 4242))
    r = client.post(
        "/api/setup/admin",
        headers={"X-Forwarded-For": "127.0.0.1, 10.0.0.7", "Host": "localhost"},
        json={"username": "root", "email": "r@x.io", "password": "correct-horse-battery-1"},
    )
    assert r.status_code == 200, r.text
    row = next(x for x in recorder.rows if x.path == "/api/setup/admin")
    assert row.client_ip == "10.0.0.5"
    assert row.peer_ip == "10.0.0.5"
    assert row.client_port == 4242


def test_a_trusted_proxy_names_the_client_and_the_peer_is_kept(tmp_path, monkeypatch):
    app, recorder = _proxied_app(tmp_path, monkeypatch)
    client = TestClient(app, client=("10.0.0.5", 4242))
    client.get("/healthz", headers={"X-Forwarded-For": "198.51.100.7"})
    row = next(x for x in recorder.rows if x.path == "/healthz")
    assert row.client_ip == "198.51.100.7"
    assert row.peer_ip == "10.0.0.5"


def test_setup_through_a_trusted_proxy_is_open_until_the_first_admin(tmp_path, monkeypatch):
    """Setup no longer asks for a loopback peer: through a trusted proxy it
    answers like anywhere else, once, and is closed for good after."""
    app, _ = _proxied_app(tmp_path, monkeypatch)
    client = TestClient(app, client=("10.0.0.5", 4242))
    body = {"username": "root", "email": "r@x.io", "password": "correct-horse-battery-1"}
    r = client.post("/api/setup/admin", headers={"X-Forwarded-For": "127.0.0.1", "Host": "localhost"}, json=body)
    assert r.status_code == 200, r.text
    again = client.post("/api/setup/admin", headers={"X-Forwarded-For": "127.0.0.1", "Host": "localhost"}, json=body)
    assert again.status_code == 410, again.text


def test_ignored_forwarded_for_is_warned_once_per_peer(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DERATE_TRUSTED_PROXIES", raising=False)
    app = create_app()
    first = TestClient(app, client=("10.0.0.7", 40000))
    second = TestClient(app, client=("10.0.0.8", 40001))
    with caplog.at_level(logging.WARNING, logger="gateway.security"):
        for _ in range(3):
            first.get("/healthz", headers={"X-Forwarded-For": "1.2.3.4"})
        first.get("/healthz")
        second.get("/healthz", headers={"X-Forwarded-For": "1.2.3.4"})
    lines = [
        r.getMessage() for r in caplog.records
        if r.name == "gateway.security" and "X-Forwarded-For" in r.getMessage()
    ]
    assert lines == [
        "X-Forwarded-For from 10.0.0.7 ignored: set DERATE_TRUSTED_PROXIES if a proxy is in front",
        "X-Forwarded-For from 10.0.0.8 ignored: set DERATE_TRUSTED_PROXIES if a proxy is in front",
    ]
