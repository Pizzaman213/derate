"""The installer surface: the script, and the credential that makes it one line.

A third `APIRouter`, for the reason `ui_api.py` gives at length -- `internal_api.py`
is 900 lines in a single closure that several sessions edit at once, and
FastAPI composes routers natively, so a separate module costs one
`include_router` line and is never contended.

**Registration order is load-bearing here too, and worse than usual.** `app.py`
mounts the built UI at `"/"`, and a Starlette mount at the root swallows every
path not matched by an *earlier* route. `GET /install.sh` is a root-level path,
so if this router is included below that mount the coordinator serves
`index.html` to `curl | sh` -- which fails as a shell script somewhere around
the first `<`, on the machine being installed, with nothing useful on screen.

Two things this file is careful about:

**The script never contains a token.** `/install.sh` serves the same bytes to
everyone; the credential is only ever an argv value in the command the UI
renders. That is what makes serving it on an unauthenticated surface fine, and
it is a property to preserve rather than a coincidence.

**The address comes from the server, not the browser.**
`ui/src/tabs/settings/ClusterCard.tsx` deleted its gateway-address row because
"the only candidate value is the browser's own `window.location.host` ...
correct in production and a lie in dev". The command composed here has the same
problem and solves it the same way: the coordinator's own probed address wins,
and the request's Host header is a fallback used only when it is not loopback.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from . import errors
from .deps import GatewayContext

log = logging.getLogger("gateway.enroll_api")

# The installed name inside the image (Dockerfile stage 3), then the repo root
# for a dev tree run straight from a checkout. DERATE_INSTALL_SH overrides
# both.
_IMAGE_PATH = Path("/opt/derate/install.sh")
_REPO_PATH = Path(__file__).resolve().parents[2] / "install.sh"

# Where the first node's copy comes from, since there is no coordinator to ask
# yet. One constant, named in the UI and the README too.
INSTALL_REPO = "Pizzaman213/derate"
INSTALL_BRANCH = "main"
PUBLIC_INSTALL_URL = (
    f"https://raw.githubusercontent.com/{INSTALL_REPO}/{INSTALL_BRANCH}/install.sh"
)

_LOOPBACK = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]")


def install_script_path() -> Path | None:
    override = os.environ.get("DERATE_INSTALL_SH")
    for candidate in ([Path(override)] if override else []) + [_IMAGE_PATH, _REPO_PATH]:
        if candidate.is_file():
            return candidate
    return None


def _is_loopback(host: str) -> bool:
    return host.split(":")[0].strip().lower() in _LOOPBACK


def _coordinator_origin(ctx: GatewayContext, request: Request) -> str:
    """`http://host:port` a *different machine* can reach this coordinator on.

    The registry's own probed address is the truth -- it is what the node
    advertises over mDNS and what every peer already dials. The request's Host
    is used only when the registry cannot say, and never when it is loopback:
    pasting `http://localhost:8080` into a command meant for another box is the
    one failure that looks like it worked.
    """
    port = 8080
    address = None
    registry = ctx.deps.registry
    profile = getattr(registry, "local_profile", None)
    if profile is not None:
        address = getattr(profile, "address", None)
    config = getattr(registry, "config", None)
    if config is not None:
        port = getattr(config, "coordinator_port", None) or port
    else:
        port = getattr(ctx.settings, "port", None) or port

    if address and not _is_loopback(str(address)):
        return f"http://{address}:{port}"

    host = request.headers.get("host") or ""
    if host and not _is_loopback(host):
        return f"http://{host}"

    # Nothing routable to offer. Say so with the loopback form rather than
    # inventing an address: a wrong address fails on the other machine with a
    # connection timeout and no clue why.
    return f"http://{address or '127.0.0.1'}:{port}"


def build_command(origin: str, token: str) -> str:
    """The one line to paste on the new machine. Composed in exactly one place.

    The Docker form, and still the default: it is what a Linux GPU node runs.
    Kept as its own function and its own response field so nothing that already
    reads ``command`` has to change.
    """
    return f"curl -fsSL {origin}/install.sh | sh -s -- --join {origin} --token {token}"


def build_commands(origin: str, token: str) -> dict[str, str]:
    """The join line. Still composed in exactly one place.

    One entry, and that is the claim being made: ``install.sh`` installs a
    *container* and refuses to run anywhere Docker cannot give it host
    networking, the GPU and the host PID namespace. Linux is what derate
    supports today.

    There were two more, ``native`` and ``windows``, and both were
    ``pipx install derate``. **That name on PyPI is somebody else's** -- "a
    machine wide rate limiter", at version 0.1.0 -- so the line installed an
    unrelated project onto the laptop of whoever was adding their first node,
    and looked like it had worked. Nothing rendered them, which is how it
    survived. Do not put a platform back in this dict without an installer
    that has been run on that platform.
    """
    return {"docker": build_command(origin, token)}


def create_router(ctx: GatewayContext) -> APIRouter:
    router = APIRouter()

    def _registry_call(name: str):
        fn = getattr(ctx.deps.registry, name, None)
        return fn if callable(fn) else None

    @router.get("/install.sh")
    async def install_script() -> Response:
        path = install_script_path()
        if path is None:
            return errors.error_response(
                404,
                "The installer script is not present in this deployment. It "
                "ships at /opt/derate/install.sh; set DERATE_INSTALL_SH "
                "to point at it.",
                "invalid_request_error",
                "install_script_missing",
            )
        try:
            body = path.read_text()
        except OSError as exc:
            log.exception("could not read the installer script")
            return errors.error_response(
                502,
                f"Could not read the installer script: {type(exc).__name__}.",
                "server_error",
                "install_script_unreadable",
            )
        # text/x-shellscript, and explicitly not text/html: a browser that
        # renders this instead of downloading it is the least of it, but a
        # proxy that decides to rewrite HTML in flight would corrupt a script
        # that is about to be piped into sh.
        return PlainTextResponse(
            body,
            media_type="text/x-shellscript; charset=utf-8",
            headers={"cache-control": "no-store"},
        )

    @router.post("/api/enroll")
    async def mint_enrollment(request: Request) -> Response:
        mint = _registry_call("mint_enrollment")
        if mint is None:
            return errors.error_response(
                501,
                "Enrollment tokens are not wired up yet: the registry does not "
                "expose them.",
                "server_error",
                "not_implemented",
            )
        try:
            payload: dict[str, Any] = await request.json()
        except Exception:
            # An empty body is the common case -- "give me the default token" --
            # so it is not an error, unlike a body that is present and malformed.
            payload = {}
        if not isinstance(payload, dict):
            return errors.error_response(
                400,
                "Body must be a JSON object.",
                "invalid_request_error",
                "invalid_request",
            )

        kwargs: dict[str, Any] = {}
        try:
            if payload.get("ttl_s") is not None:
                kwargs["ttl_s"] = float(payload["ttl_s"])
            if "uses" in payload:
                kwargs["uses"] = None if payload["uses"] is None else int(payload["uses"])
            if payload.get("auto_admit") is not None:
                kwargs["auto_admit"] = bool(payload["auto_admit"])
        except (TypeError, ValueError) as exc:
            return errors.error_response(
                400, f"Enrollment body malformed: {exc}.",
                "invalid_request_error", "invalid_request",
            )

        try:
            token = mint(**kwargs)
        except ValueError as exc:
            return errors.error_response(
                400, str(exc), "invalid_request_error", "invalid_request"
            )
        except OSError as exc:
            log.exception("could not mint an enrollment token")
            return errors.error_response(
                502,
                f"Could not persist the enrollment token: {type(exc).__name__}.",
                "server_error",
                "enrollment_write_failed",
            )

        origin = _coordinator_origin(ctx, request)
        body = token.public(token.created_at)
        # `command` carries the secret, and is the only field that does. The
        # UI's scrub() blanks any field literally named `token`, which is the
        # right default and stays -- this is one composed line an operator is
        # meant to copy, minted for one install and expiring within the hour,
        # not a credential the UI stores or a field it can reach into. No
        # endpoint anywhere returns the permanent cluster token.
        body["command"] = build_command(origin, token.token)
        # Additive: the Docker line above is unchanged for every existing
        # reader, and "commands" carries the same token for the two
        # platforms that cannot run it.
        body["commands"] = build_commands(origin, token.token)
        body["join_url"] = origin
        body["install_url"] = f"{origin}/install.sh"
        body["public_install_url"] = PUBLIC_INSTALL_URL
        return JSONResponse(body)

    @router.get("/api/enroll")
    async def list_enrollments() -> Response:
        listing = _registry_call("enrollments")
        if listing is None:
            return JSONResponse([])
        try:
            return JSONResponse(listing())
        except Exception:
            log.exception("enrollment listing failed")
            return JSONResponse([])

    @router.delete("/api/enroll/{token_id}")
    async def revoke_enrollment(token_id: str) -> Response:
        revoke = _registry_call("revoke_enrollment")
        if revoke is None:
            return errors.error_response(
                501,
                "Enrollment tokens are not wired up yet: the registry does not "
                "expose them.",
                "server_error",
                "not_implemented",
            )
        if not revoke(token_id):
            return errors.error_response(
                404,
                f"No live enrollment token '{token_id}'.",
                "invalid_request_error",
                "enrollment_not_found",
            )
        return Response(status_code=204)

    return router
