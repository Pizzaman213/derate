"""Agent G: gateway, routing, and admission control.

One endpoint, every model in the cluster behind it, whatever node it runs on
and whatever runtime serves it.
"""

from .app import create_app
from .deps import GatewayContext, GatewayDeps
from .settings import GatewaySettings

__all__ = ["GatewayContext", "GatewayDeps", "GatewaySettings", "create_app"]
