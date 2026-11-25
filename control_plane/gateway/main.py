"""Entrypoint. ``python -m control_plane.gateway.main``

With ``--isolated ROOT --ui-dir DIR`` it is the stub UI the agents' MCP tool
(``.omp/stubui/server.mjs``) looks at: the same stub surface, sealed off from
this machine by ``isolated.py``.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import uvicorn

from control_plane import logfiles
from control_plane.redaction import Redactor, SecretRedactingFilter

from .app import create_app
from .isolated import BANNER, serve_isolated
from .settings import GatewaySettings, uvicorn_proxy_kwargs


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python3 -m control_plane.gateway.main")
    parser.add_argument("--isolated", type=Path, metavar="ROOT",
                        help="serve the stub sealed off under ROOT, for the stub-UI MCP tool")
    parser.add_argument("--ui-dir", help="the built UI to serve; only with --isolated")
    args = parser.parse_args(argv)
    if bool(args.isolated) != bool(args.ui_dir):
        parser.error("--isolated and --ui-dir go together")
    return args


def main(argv: list[str] | None = None) -> None:
    args = _parse(argv)
    if args.isolated:
        logging.basicConfig(level=logging.WARNING, format=logfiles.LOG_FORMAT)
        serve_isolated(args.isolated, args.ui_dir)
        return
    logging.basicConfig(
        level=os.environ.get("DERATE_LOG_LEVEL", "INFO"),
        format=logfiles.LOG_FORMAT,
    )
    for handler in logging.getLogger().handlers:
        handler.setFormatter(logfiles.UTCFormatter(logfiles.LOG_FORMAT))
        handler.addFilter(SecretRedactingFilter(Redactor()))
    # basicConfig sets the root level, but the journal handler is attached
    # during app startup and wants records the root logger would otherwise
    # filter out before any handler sees them.
    logging.getLogger().setLevel(os.environ.get("DERATE_LOG_LEVEL", "INFO"))
    logfiles.install()
    print("=" * 72 + "\n" + BANNER + "\n" + "=" * 72, flush=True)
    settings = GatewaySettings(
        host=os.environ.get("DERATE_HOST", "0.0.0.0"),
        port=int(os.environ.get("DERATE_PORT", "8080")),
        electricity_rate_usd_per_kwh=float(
            os.environ.get("DERATE_ELECTRICITY_RATE", "0")
        ),
    )
    # No deps passed: the day-0 stub surface. Integration replaces this with
    # the real ports without touching anything else here.
    # log_config=None so uvicorn does not replace the configuration above
    # with its own after the fact; without it two formats share one stream.
    # access_log=False because authz.on_request_done writes the access line
    # on logger gateway.access (request id, principal, UTC), which replaces
    # uvicorn's.
    uvicorn.run(
        create_app(settings=settings),
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
        server_header=False,
        **uvicorn_proxy_kwargs(),
    )


if __name__ == "__main__":
    main()
