"""Attack a running edge listener the way a WAN caller would, and say what held.

    python3 -m tests.edge_probe --edge http://127.0.0.1:18082 --key derate-... \\
        --main http://127.0.0.1:18080 --admin-user ci --admin-password ... --via direct
    python3 -m tests.edge_probe --edge https://<name>.trycloudflare.com --key derate-... \\
        --main http://127.0.0.1:8088 --admin-user ... --admin-password ... --via tunnel

``--via direct`` talks to the edge socket itself, so it writes
``CF-Connecting-IP`` the way Cloudflare would and can test what happens when
the header is missing, repeated or forged. ``--via tunnel`` goes through
Cloudflare, which always sets that header; there the question is whether a
header the CALLER sends survives. Observed on a quick tunnel (2026-11-23):
Cloudflare answers a caller's own ``CF-Connecting-IP`` with 403 (error 1000)
before derate sees it, and refuses ``OPTIONS *`` and every smuggling shape
here with its own 400. ``--main`` and the admin login are optional and add
the checks that read back what the gateway recorded (``last_used_ip``).

``tests/unit/test_edge_spoofing.py`` covers the same ground in-process. This
exists for what in-process cannot reach: a raw request line no HTTP client
would send (dot segments, absolute-form targets, ``*``), two requests in one
write, a smuggled request inside a body, and ``Host`` against a socket that
really is the edge. Every check prints PASS or FAIL; any FAIL exits 1.

The throttle checks run last and spend this caller's refusal budget for a
minute: through a tunnel that is this machine's own public address.
"""

from __future__ import annotations

import argparse
import random
import socket
import ssl
import sys
import time
from urllib.parse import urlsplit

import httpx

#: Bodies that only a non-/v1 handler would produce.
LEAKS = (b'"admin_exists"', b'"secret_once"', b"<!doctype html", b"<html", b'"cluster_id"')


def field(r: httpx.Response, *path: str):
    """``r.json()[path...]``, or None when the body is not that shape -- a
    check reads a wrong answer as a FAIL, never as a crash."""
    try:
        value = r.json()
    except ValueError:
        return None
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _fresh_address() -> str:
    return f"198.{random.randint(18, 19)}.{random.randint(0, 255)}.{random.randint(1, 254)}"


class Probe:
    def __init__(self, args) -> None:
        self.edge = args.edge.rstrip("/")
        self.via = args.via
        self.key = args.key
        self.key_id = args.key.removeprefix("derate-").split("_", 1)[0]
        self.main = args.main.rstrip("/") if args.main else None
        self.admin = (args.admin_user, args.admin_password) if args.admin_user else None
        # A fresh benchmarking address (198.18.0.0/15) per run, so two runs
        # inside one minute do not share a refusal budget.
        self.addr = _fresh_address()
        self.http = httpx.Client(timeout=20, follow_redirects=False)
        self.results: list[tuple[str, bool, str]] = []
        self._cookie: str | None = None

    # -- plumbing -------------------------------------------------------------

    def who(self, address: str | None = None) -> dict:
        """The client header a direct caller must write; through a tunnel,
        Cloudflare writes it."""
        return {"CF-Connecting-IP": address or self.addr} if self.via == "direct" else {}

    def bearer(self, key: str | None = None) -> dict:
        return {"Authorization": f"Bearer {key or self.key}"}

    def check(self, name: str, ok: bool, detail: str = "", how: str = "") -> None:
        self.results.append((name, ok, detail))
        suffix = f" -- {detail}" if not ok else (f" ({how})" if how else "")
        print(f"{'PASS' if ok else 'FAIL'}  {name}{suffix}", flush=True)

    def get(self, path: str, headers: dict | None = None, method: str = "GET", **kw) -> httpx.Response:
        return self.http.request(method, self.edge + path, headers=headers or {}, **kw)

    def raw(self, payload: bytes, idle: float = 1.5) -> bytes:
        """*payload* written verbatim to the edge socket; everything it answers."""
        parts = urlsplit(self.edge)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        sock = socket.create_connection((parts.hostname, port), timeout=10)
        if parts.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parts.hostname)
        out = b""
        try:
            sock.sendall(payload)
            sock.settimeout(idle)
            while True:
                try:
                    chunk = sock.recv(65536)
                except (TimeoutError, socket.timeout):
                    break
                if not chunk:
                    break
                out += chunk
        finally:
            sock.close()
        return out

    def host_header(self) -> str:
        return urlsplit(self.edge).netloc

    def request(self, target: str, *, method: str = "GET", host: str | None = None,
                extra: str = "", body: bytes = b"", close: bool = True) -> bytes:
        head = f"{method} {target} HTTP/1.1\r\nHost: {host or self.host_header()}\r\n"
        for name, value in {**self.who(), **self.bearer()}.items():
            head += f"{name}: {value}\r\n"
        head += extra
        if body and "content-length" not in extra.lower() and "transfer-encoding" not in extra.lower():
            head += f"Content-Length: {len(body)}\r\n"
        head += "Connection: close\r\n" if close else "Connection: keep-alive\r\n"
        return (head + "\r\n").encode("latin-1") + body

    def verdict(self, data: bytes) -> tuple[bool, str]:
        """Whether a raw answer is safe, and who gave it.

        Only derate's own answers can leak, and every one carries derate's
        ``x-request-id: r-...``. Through a tunnel, Cloudflare refusing the
        request (its own 4xx page) or closing the connection means it never
        reached derate -- a pass, and labelled as one. Talking to the socket
        directly there is nobody in front, so silence or a stranger's answer
        is a FAIL: a probe that cannot tell "refused" from "not asked" proves
        nothing."""
        if not data.startswith(b"HTTP/"):
            return (self.via == "tunnel", "connection closed before derate")
        head, _, _ = data.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        if b"\r\nx-request-id: r-" not in head.lower():
            # uvicorn's protocol layer refuses a malformed request (conflicting
            # Content-Length and Transfer-Encoding) before the app runs, so
            # its 400 carries no request id. Refused, not answered.
            if status.split(" ")[1:2] == ["400"] and data.endswith(b"Invalid HTTP request received."):
                return (True, "refused by the HTTP parser")
            return (self.via == "tunnel", f"refused before derate: {status}")
        low = data.lower()
        hit = next((m for m in LEAKS if m in low), None)
        return (hit is None, f"derate: {status}" if hit is None else f"LEAKED {hit!r}")

    def last_used_ip(self) -> str | None:
        if not (self.main and self.admin):
            return None
        if self._cookie is None:
            r = self.http.post(self.main + "/api/auth/login",
                               json={"username": self.admin[0], "password": self.admin[1]})
            r.raise_for_status()
            self._cookie = r.cookies.get("__Host-derate_session") or r.cookies.get("derate_session")
            self.http.cookies.clear()
        name = "derate_session"
        row = self.http.get(f"{self.main}/api/keys/{self.key_id}", headers={"Cookie": f"{name}={self._cookie}"})
        row.raise_for_status()
        return field(row, "last_used_ip")

    # -- the checks -----------------------------------------------------------

    def baseline(self) -> bool:
        r = self.get("/v1/models", {**self.who(), **self.bearer()})
        ok = r.status_code == 200 and field(r, "object") == "list"
        self.check("a valid key is answered on the edge", ok, f"{r.status_code} {r.text[:120]}")
        return ok

    def only_v1(self) -> None:
        routes = [
            ("GET", "/"), ("GET", "/index.html"), ("GET", "/healthz"), ("GET", "/install.sh"),
            ("GET", "/api/auth/state"), ("POST", "/api/auth/login"), ("POST", "/api/setup/admin"),
            ("POST", "/api/nodes/join"), ("GET", "/auth/callback"), ("GET", "/assets/index.js"),
            ("GET", "/api/keys"), ("DELETE", "/api/keys/x"), ("PUT", "/api/settings"),
            ("HEAD", "/api/auth/state"), ("GET", "/usage/keys"),
        ]
        for method, path in routes:
            r = self.get(path, {**self.who(), **self.bearer()}, method=method, json={} if method in ("POST", "PUT") else None)
            code = field(r, "error", "code") if method != "HEAD" else "not_found"
            ok = r.status_code == 404 and code == "not_found" and "set-cookie" not in r.headers
            self.check(f"{method} {path} is not answered on the edge", ok, f"{r.status_code} {r.text[:120]}")
        r = self.get("/api/keys", {**self.who(), "Origin": "https://evil.example",
                                   "Access-Control-Request-Method": "GET"}, method="OPTIONS")
        self.check("a CORS preflight to /api is not answered on the edge", r.status_code == 404,
                   f"{r.status_code} {r.text[:120]}")

    def raw_targets(self) -> None:
        host = self.host_header()
        targets = [
            "/v1/../api/auth/state", "/v1/%2e%2e/api/auth/state", "/v1%2f..%2fapi%2fauth%2fstate",
            "//api/auth/state", "/v1/..;/api/auth/state", "/%61pi/auth/state",
            "/v1/models/../../api/auth/state", "/./api/auth/state", "/v1/./../api/auth/state",
            f"http://{host}/api/auth/state", "/v1/models/..%2f..%2fapi%2fkeys",
            "/v1/..%2Fcluster", "/v1/..%2F", "/v1/%2E%2E/index.html",
        ]
        for target in targets:
            data = self.raw(self.request(target))
            ok, how = self.verdict(data)
            self.check(f"raw target {target!r} reaches no non-/v1 handler", ok, f"{how}: {data[:160]!r}", how)
        data = self.raw(self.request("*", method="OPTIONS"))
        ok, how = self.verdict(data)
        self.check("OPTIONS * reaches no non-/v1 handler", ok, f"{how}: {data[:160]!r}", how)

    def host_games(self) -> None:
        if self.via != "direct":
            return  # Cloudflare routes by Host; a foreign one never reaches the tunnel
        # The last five are what Starlette would rebuild request.url from:
        # they used to move the path the gate checked away from the router's.
        for host in ("127.0.0.1:8088", "localhost", "127.0.0.1", "[::1]:8088", "",
                     "x#", "x?", f"{self.host_header()}/v1", "[", "x/v1#",
                     # urlsplit raises on these: they were a 500 per request.
                     "[1.2.3.4]", "[:]", "[1]"):
            data = self.raw(self.request("/api/auth/state", host=host))
            ok, how = self.verdict(data)
            ok = ok and (b" 404 " in data.split(b"\r\n", 1)[0] or b" 400 " in data[:16])
            self.check(f"Host {host!r} does not move /api/auth/state onto the LAN listener", ok, repr(data[:160]), how)
        data = self.raw(b"GET /api/auth/state HTTP/1.0\r\n" + b"".join(
            f"{k}: {v}\r\n".encode() for k, v in self.who().items()) + b"\r\n")
        ok, how = self.verdict(data)
        self.check("HTTP/1.0 with no Host still gets the edge's rules", ok, repr(data[:160]), how)
        if self.main:
            # The same trick against the LAN listener, with no credential at
            # all: before the fix this returned the key list to anyone.
            for host in ("x#", "x?", "x/v1", "[1.2.3.4]"):
                r = self.http.get(self.main + "/api/keys", headers={"Host": host})
                self.check(f"the LAN port refuses an anonymous /api/keys with Host {host!r}",
                           r.status_code == 400 and field(r, "error", "code") == "invalid_host",
                           f"{r.status_code} {r.text[:120]}")

    def sockets(self) -> None:
        """A websocket upgrade is refused before accept, which uvicorn answers
        403. One to a path no route owns used to reach the static mount,
        which asserts http: a 500 and a traceback in node.log per attempt."""
        upgrade = ("Upgrade: websocket\r\nSec-WebSocket-Version: 13\r\n"
                   "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n")
        for target in ("/v1/nope", "/v1/realtime/../x", "/api/x", "/"):
            head = f"GET {target} HTTP/1.1\r\nHost: {self.host_header()}\r\nConnection: Upgrade\r\n" + upgrade
            for name, value in self.who().items():
                head += f"{name}: {value}\r\n"
            data = self.raw((head + "\r\n").encode("latin-1"))
            status = data.split(b"\r\n", 1)[0]
            ok = b" 403 " in status + b" " or (self.via == "tunnel" and not data.startswith(b"HTTP/1.1 5"))
            self.check(f"a websocket to {target!r} is refused, not a 500", ok, repr(data[:160]))

    def origins(self) -> None:
        """A malformed Origin made `urlsplit` raise inside the gate, before any
        key was read: a 500 and a traceback per request."""
        for origin in ("http://[", "https://[x]", "//["):
            data = self.raw(self.request(
                "/v1/chat/completions", method="POST", body=b"{}",
                extra=f"Origin: {origin}\r\nContent-Type: application/json\r\n",
            ))
            ok, how = self.verdict(data)
            ok = ok and not data.startswith(b"HTTP/1.1 5")
            self.check(f"Origin {origin!r} is refused, not a 500", ok, repr(data[:160]), how)

    def pipelining_and_smuggling(self) -> None:
        first = self.request("/v1/models", close=False)
        second = self.request("/api/auth/state")
        data = self.raw(first + second, idle=2.5)
        ok, how = self.verdict(data)
        if self.via == "direct":
            # Both requests answered by derate, the second with the edge's 404.
            ok = ok and data.count(b"HTTP/1.1 ") == 2 and b'"not_found"' in data
        self.check("a pipelined /api request on an edge connection is refused", ok, repr(data[-200:]), how)
        smuggled = self.request("/api/auth/state").decode("latin-1")
        cl_te = f"0\r\n\r\n{smuggled}".encode("latin-1")
        payloads = {
            "CL.TE": self.request("/v1/models", method="POST", body=cl_te,
                                  extra=f"Content-Length: {len(cl_te)}\r\nTransfer-Encoding: chunked\r\n", close=False),
            "TE.CL": self.request("/v1/models", method="POST", body=f"{len(smuggled):x}\r\n{smuggled}\r\n0\r\n\r\n".encode("latin-1"),
                                  extra="Content-Length: 4\r\nTransfer-Encoding: chunked\r\n", close=False),
            "TE.TE": self.request("/v1/models", method="POST", body=cl_te,
                                  extra=f"Content-Length: {len(cl_te)}\r\nTransfer-Encoding: chunked\r\nTransfer-encoding: identity\r\n",
                                  close=False),
        }
        for name, payload in payloads.items():
            data = self.raw(payload, idle=2.5)
            ok, how = self.verdict(data)
            self.check(f"a {name} smuggled /api request is never answered", ok, repr(data[-200:]), how)

    def client_header(self) -> None:
        if self.via != "direct":
            return
        r = self.get("/v1/models", self.bearer())
        self.check("an edge request without the client header is refused",
                   (r.status_code, field(r, "error", "code")) == (400, "edge_client_unknown"), r.text[:120])
        for value in ("203.0.113.9, 198.51.100.1", "203.0.113.9:443", "127.0.0.1", "::1",
                      "::ffff:127.0.0.1", "0.0.0.0", "unknown", "2001:db8::1%eth0", "0x7f.0.0.1"):
            r = self.get("/v1/models", {"CF-Connecting-IP": value, **self.bearer()})
            self.check(f"client header {value!r} is refused", r.status_code == 400, f"{r.status_code} {r.text[:120]}")
        r = self.http.get(self.edge + "/v1/models", headers=[
            ("CF-Connecting-IP", "198.51.100.9"), ("CF-Connecting-IP", self.addr),
            ("Authorization", f"Bearer {self.key}")])
        self.check("a repeated client header is refused", r.status_code == 400, f"{r.status_code} {r.text[:120]}")

    def forged_forwarding(self) -> None:
        forged = {
            "X-Forwarded-For": "198.51.100.71", "X-Real-IP": "198.51.100.72", "True-Client-IP": "198.51.100.73",
            "X-Client-IP": "198.51.100.74", "Forwarded": "for=198.51.100.75", "Fastly-Client-IP": "198.51.100.76",
        }
        if self.via == "tunnel":
            # The one header that would matter: Cloudflare must never pass a
            # caller's own CF-Connecting-IP through to derate.
            r = self.get("/v1/models", {"CF-Connecting-IP": "198.51.100.77", **self.bearer()})
            by_edge = r.status_code in (400, 403) and "x-request-id" not in r.headers
            self.check("a caller's own CF-Connecting-IP is refused before derate", by_edge or r.status_code == 200,
                       f"{r.status_code} {r.text[:120]}", f"{r.status_code} from the edge provider" if by_edge else "")
        r = self.get("/v1/models", {**self.who(), **forged, **self.bearer()})
        self.check("forged forwarding headers do not stop a valid request", r.status_code == 200,
                   f"{r.status_code} {r.text[:120]}")
        recorded = self.last_used_ip()
        if recorded is None:
            print("note  --main/--admin-* not given: the recorded address is not checked")
            return
        bad = set(forged.values()) | {"198.51.100.77"}
        if self.via == "direct":
            ok = recorded == self.addr
        else:
            ok = recorded not in bad and not recorded.startswith(("127.", "::1", "10.", "192.168."))
        self.check("the recorded caller is the edge's own client address", ok, f"last_used_ip={recorded}",
                   f"recorded {recorded}")

    def scheme(self) -> None:
        r = self.get("/v1/models", {**self.who(), **self.bearer(), "X-Forwarded-Proto": "http"})
        self.check("X-Forwarded-Proto: http does not switch HSTS off",
                   r.headers.get("strict-transport-security", "").startswith("max-age="), str(dict(r.headers))[:200])

    def credentials(self) -> None:
        import base64

        attempts = {
            "query ?api_key=": ("/v1/models?api_key=" + self.key, {}),
            "X-API-Key": ("/v1/models", {"X-API-Key": self.key}),
            "Basic auth": ("/v1/models", {"Authorization": "Basic " + base64.b64encode(f"{self.key}:".encode()).decode()}),
            "cookie": ("/v1/models", {"Cookie": f"api_key={self.key}"}),
            "legacy drt_ prefix": ("/v1/models", self.bearer("drt_" + self.key.removeprefix("derate-"))),
            "upper-cased": ("/v1/models", self.bearer(self.key.upper())),
            "truncated": ("/v1/models", self.bearer(self.key[:-2])),
        }
        for name, (path, headers) in attempts.items():
            r = self.get(path, {**self.who(), **headers})
            self.check(f"a key sent as {name} is refused", r.status_code == 401, f"{r.status_code} {r.text[:120]}")
        for header in ("X-Original-URL", "X-Rewrite-URL", "X-Forwarded-Prefix"):
            r = self.get("/v1/models", {**self.who(), **self.bearer(), header: "/api/keys"})
            self.check(f"{header} does not reroute a /v1 request", r.status_code == 200 and field(r, "object") == "list",
                       r.text[:120])

    def media(self) -> None:
        urls = {
            "loopback short form": "http://127.1:18081/agent/health",
            "hex octet": "http://0x7f.0.0.1/",
            "metadata service": "http://169.254.169.254/latest/meta-data/",
            "IDNA dot": "http://nas.local\u3002/",
            "backslash authority": "http://127.0.0.1\\@example.com/",
            "percent host": "http://%31%30.0.0.1/",
            "mapped IPv6": "http://[::ffff:10.0.0.1]/",
        }
        for name, url in urls.items():
            body = {"model": "edge-probe", "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": url}}]}]}
            r = self.get("/v1/chat/completions", {**self.who(), **self.bearer()}, method="POST", json=body)
            ok = r.status_code == 400 and field(r, "error", "code") == "media_url_refused"
            self.check(f"media URL ({name}) is refused before forwarding", ok, f"{r.status_code} {r.text[:120]}")
        body = {"model": "edge-probe", "messages": [{"role": "user", "content": [
            {"type": "video_url", "video_url": "http://192.168.0.1/v.mp4"}]}]}
        r = self.get("/v1/chat/completions", {**self.who(), **self.bearer()}, method="POST", json=body)
        self.check("a plain-string video_url is checked too", r.status_code == 400, f"{r.status_code} {r.text[:120]}")

    def throttle(self) -> None:
        bad = "derate-aaaaaaaaaa_" + "x" * 43
        if self.via == "direct":
            address = _fresh_address()
            while address == self.addr:
                address = _fresh_address()
            codes = [self.get("/v1/models", {"CF-Connecting-IP": address, **self.bearer(bad)}).status_code
                     for _ in range(21)]
            self.check("the 21st refused key from one address is answered 429",
                       codes[:20] == [401] * 20 and codes[20] == 429, str(codes))
            prefix = f"2001:db8:{random.randint(1, 0xffff):x}:{random.randint(1, 0xffff):x}"
            codes = [self.get("/v1/models", {"CF-Connecting-IP": f"{prefix}::{i + 1:x}", **self.bearer(bad)}).status_code
                     for i in range(21)]
            self.check("rotating addresses inside one IPv6 /64 shares one budget",
                       codes[:20] == [401] * 20 and codes[20] == 429, str(codes))
        else:
            codes = []
            for _ in range(25):
                codes.append(self.get("/v1/models", self.bearer(bad)).status_code)
                if codes[-1] == 429:
                    break
            r = self.get("/v1/models", self.bearer(bad))
            self.check("refused keys through the tunnel are throttled by the real caller",
                       429 in codes and r.headers.get("retry-after") == "60", str(codes))

    def run(self, skip_throttle: bool) -> int:
        print(f"edge {self.edge} via {self.via}" + (f", caller {self.addr}" if self.via == "direct" else ""))
        if not self.baseline():
            return 1
        self.only_v1()
        self.raw_targets()
        self.host_games()
        self.sockets()
        self.origins()
        self.pipelining_and_smuggling()
        self.client_header()
        self.forged_forwarding()
        self.scheme()
        self.credentials()
        self.media()
        if not skip_throttle:
            self.throttle()
        failed = [name for name, ok, _ in self.results if not ok]
        print(f"\n{len(self.results) - len(failed)} passed, {len(failed)} failed")
        return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m tests.edge_probe", description=__doc__.split("\n\n")[0])
    parser.add_argument("--edge", required=True, help="the edge listener's origin, or the tunnel's")
    parser.add_argument("--key", required=True, help="a valid derate- key")
    parser.add_argument("--via", choices=("direct", "tunnel"), default="direct")
    parser.add_argument("--main", default=None, help="the LAN origin, to read back what was recorded")
    parser.add_argument("--admin-user", default=None)
    parser.add_argument("--admin-password", default=None)
    parser.add_argument("--skip-throttle", action="store_true", help="leave this caller's refusal budget alone")
    args = parser.parse_args(argv)
    started = time.monotonic()
    code = Probe(args).run(args.skip_throttle)
    print(f"({time.monotonic() - started:.1f}s)")
    return code


if __name__ == "__main__":
    sys.exit(main())
