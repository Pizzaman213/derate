"""One place that says what models this cluster knows about.

The Models tab used to fold six endpoints together in the browser, and
fourteen other screens each re-derived their own answer. This package holds
the merge once, on the server, in SQLite, so there is one shape to fetch and
one writer to blame.

It is a materialized view. The stores it reads from keep owning their
records; nothing here routes a request. See :mod:`.service` for both rules
stated properly.
"""

from .build import build
from .db import SCHEMA_VERSION
from .records import (
    FACET_ORDER,
    SERVER_FACETS,
    CacheScan,
    ModelRecord,
    RowCache,
    RowDeployment,
    RowProvider,
)
from .service import ModelInventory

__all__ = [
    "FACET_ORDER",
    "SCHEMA_VERSION",
    "SERVER_FACETS",
    "CacheScan",
    "ModelInventory",
    "ModelRecord",
    "RowCache",
    "RowDeployment",
    "RowProvider",
    "build",
]
