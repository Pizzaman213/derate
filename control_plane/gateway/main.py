"""Entrypoint. ``python -m control_plane.gateway.main``"""

from __future__ import annotations

import logging
import os

import uvicorn

from .app import create_app
from .settings import GatewaySettings


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("SPARKPLANE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # basicConfig sets the root level, but the journal handler is attached
    # during app startup and wants records the root logger would otherwise
    # filter out before any handler sees them.
    logging.getLogger().setLevel(os.environ.get("SPARKPLANE_LOG_LEVEL", "INFO"))
    settings = GatewaySettings(
        host=os.environ.get("SPARKPLANE_HOST", "0.0.0.0"),
        port=int(os.environ.get("SPARKPLANE_PORT", "8080")),
        electricity_rate_usd_per_kwh=float(
            os.environ.get("SPARKPLANE_ELECTRICITY_RATE", "0")
        ),
    )
    # No deps passed: the day-0 stub surface. Integration replaces this with
    # the real ports without touching anything else here.
    # log_config=None so uvicorn does not replace the configuration above
    # with its own after the fact. Without it two formats share one stream,
    # and its access logger stops propagating to the root handler the
    # telemetry journal is attached to.
    uvicorn.run(
        create_app(settings=settings),
        host=settings.host,
        port=settings.port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
