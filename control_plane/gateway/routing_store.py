"""``routing.json``: the operator's per-name policy overrides, route limits
and traffic shares.

All three used to live only in the router's memory, so a restart quietly put every
served name back on ``auto`` and dropped its limits. This file is the one
place they survive. A missing or malformed file is an empty state, never a
failed startup -- the same rule ``providers.json`` follows.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from control_plane import fsutil
from control_plane.contracts import RoutingPolicy

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


@dataclass
class RoutingState:
    overrides: dict[str, RoutingPolicy] = field(default_factory=dict)
    limits: dict[str, dict[str, int]] = field(default_factory=dict)
    #: served_name -> target_id -> whole-number share; see RoutingConfig.shares.
    shares: dict[str, dict[str, int]] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> RoutingState:
        return cls(overrides={}, limits={}, shares={})


class RoutingStore:
    """Reads and writes ``routing.json``. Atomic."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> RoutingState:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return RoutingState.empty()
        except OSError as exc:
            log.error("routing.json ignored: %s", exc)
            return RoutingState.empty()
        try:
            raw = json.loads(text)
            if not isinstance(raw, dict):
                raise ValueError("top level is not an object")
            overrides_raw = raw.get("overrides", {})
            limits_raw = raw.get("limits", {})
            shares_raw = raw.get("shares", {})
            if not all(isinstance(x, dict) for x in (overrides_raw, limits_raw, shares_raw)):
                raise ValueError("overrides, limits and shares must be objects")
            overrides = {str(k): RoutingPolicy(v) for k, v in overrides_raw.items()}
            limits: dict[str, dict[str, int]] = {}
            for name, entry in limits_raw.items():
                if not isinstance(entry, dict):
                    raise ValueError(f"limits for {name!r} is not an object")
                values = {str(k): int(v) for k, v in entry.items()}
                if values:
                    limits[str(name)] = values
            shares: dict[str, dict[str, int]] = {}
            for name, entry in shares_raw.items():
                if not isinstance(entry, dict):
                    raise ValueError(f"shares for {name!r} is not an object")
                values = {str(k): int(v) for k, v in entry.items()}
                if values:
                    shares[str(name)] = values
        except (ValueError, TypeError) as exc:
            log.error("routing.json ignored: %s", exc)
            return RoutingState.empty()
        return RoutingState(overrides=overrides, limits=limits, shares=shares)

    def save(self, state: RoutingState) -> None:
        payload = {
            "version": SCHEMA_VERSION,
            "overrides": {k: v.value for k, v in state.overrides.items()},
            "limits": {k: dict(v) for k, v in state.limits.items() if v},
            "shares": {k: dict(v) for k, v in state.shares.items() if v},
        }
        text = json.dumps(payload, indent=2, sort_keys=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".routing-")
        try:
            fsutil.harden_fd(fd, self.path)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
