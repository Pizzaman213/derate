#!/usr/bin/env python3
"""Placeholder node app.

Agent A owns role resolution and the node agent; Agent G owns the gateway and
serves the UI. Until `control_plane.node` exists, this stands in so the image
is a real, testable artifact: it boots, it passes its healthcheck, and it says
plainly what is missing. It serves nothing else and launches nothing.

The entrypoint prefers `control_plane.node` whenever that module is importable,
so this file stops being used the moment A and G land, with no image change.
"""

from __future__ import annotations

import errno
import http.server
import json
import os
import socket
import sys
import threading

PORT = int(os.environ.get("DERATE_PORT", "8080"))
AGENT_PORT = int(os.environ.get("DERATE_AGENT_PORT", str(PORT)))
ROLE = os.environ.get("DERATE_ROLE", "auto")

PAGE = """<!doctype html><meta charset=utf-8><title>derate</title>
<style>body{font:15px/1.6 ui-sans-serif,system-ui,sans-serif;max-width:44rem;
margin:12vh auto;padding:0 1.5rem;color:#e6e6e6;background:#111}
code{background:#1e1e1e;padding:.15rem .4rem;border-radius:3px}
h1{font-size:1.3rem;font-weight:600}</style>
<h1>derate node</h1>
<p>The container is up, host networking is confirmed, and the agent health
endpoint is answering.</p>
<p>The coordinator UI is served by the gateway (Agent G) once
<code>control_plane.node</code> is present in the image. This page is the
placeholder that proves the container and its networking are correct.</p>
<p>Health: <code>/agent/health</code></p>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "derate-placeholder"

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("/agent/health", "/health"):
            self._send(
                200,
                "application/json",
                json.dumps(
                    {
                        "status": "ok",
                        "role": ROLE,
                        "hostname": socket.gethostname(),
                        "coordinator": False,
                        "placeholder": True,
                        "detail": "control_plane.node not present in this image",
                    }
                ).encode(),
            )
        elif self.path.rstrip("/") in ("", "/"):
            self._send(200, "text/html; charset=utf-8", PAGE.encode())
        else:
            self._send(404, "text/plain", b"not found\n")

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("[derate] %s\n" % (fmt % args))


def serve(port: int, label: str) -> http.server.ThreadingHTTPServer:
    """Bind *port* or explain, in one sentence, how to move off it.

    With host networking a port collision is a collision with whatever the
    user already runs on this machine, so a raw traceback is not good enough:
    say which variable moves us.
    """
    try:
        return http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        variable = "DERATE_AGENT_PORT" if label == "agent" else "DERATE_PORT"
        print(
            "[derate] port %d is already in use on this host.\n"
            "[derate] Host networking shares the host's ports, so this is a "
            "collision with something already running.\n"
            "[derate] Pick another one:  docker run --network host -e %s=<port> ..."
            % (port, variable),
            file=sys.stderr,
        )
        raise SystemExit(1) from None


def main() -> int:
    print(
        "[derate] placeholder node app: agent on :%d, UI on :%d (role=%s). "
        "control_plane.node is not in this image yet."
        % (AGENT_PORT, PORT, ROLE),
        file=sys.stderr,
    )
    # /agent/health exists in both roles and lives on the agent port. The
    # coordinator port also answers it so a single-port setup still works.
    agent = serve(AGENT_PORT, "agent")
    if PORT != AGENT_PORT:
        ui = serve(PORT, "ui")
        threading.Thread(target=ui.serve_forever, daemon=True).start()
    agent.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
