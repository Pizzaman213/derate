"""Entrypoint. ``python -m control_plane.gateway.main``"""

from __future__ import annotations

import logging
import os

import uvicorn

from control_plane import logfiles

from .app import create_app
from .settings import GatewaySettings, uvicorn_proxy_kwargs


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("DERATE_LOG_LEVEL", "INFO"),
        format=logfiles.LOG_FORMAT,
    )
    for handler in logging.getLogger().handlers:
        handler.setFormatter(logfiles.UTCFormatter(logfiles.LOG_FORMAT))
    # basicConfig sets the root level, but the journal handler is attached
    # during app startup and wants records the root logger would otherwise
    # filter out before any handler sees them.
    logging.getLogger().setLevel(os.environ.get("DERATE_LOG_LEVEL", "INFO"))
    logfiles.install()
    print(
        "=" * 72 + "\n"
        "STUB SURFACE -- fixture data; the real system is "
        "python3 -m control_plane.node\n" + "=" * 72,
        flush=True,
    )
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
