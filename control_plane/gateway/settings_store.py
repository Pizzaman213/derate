"""Persistence for the handful of settings a human may change at runtime.

`GatewaySettings` has 41 fields. Exactly three of them are things an operator
sets and expects to survive a restart; the rest are code-level tunables whose
values are a decision, not a preference. So this persists an explicit allowlist
rather than the dataclass: persisting all of them would let a stale file on disk
pin a code default forever, and the first time someone changed a default in
source and it did not take effect they would have no way to find out why.

Precedence is **file > env > default**. That is the opposite of the usual
ordering and it is deliberate: the file is the record of a human action taken
through the UI, env is the deployment's opinion. Nothing in the container sets
any of these three, so env-beats-file would make the UI controls permanently
inert -- a control that looks wired and is not, which is the exact failure mode
audit finding H-8 is about.

Write is atomic and 0600, mirroring `providers/store.py`: same mkstemp in the
target directory, same fchmod before any content, same os.replace. A crash
between the two leaves the old file intact, never a half-written one.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger("gateway.settings_store")

#: Bumped when the on-disk shape changes. Present from the first version,
#: because a config file without one is a migration you cannot write later.
SCHEMA_VERSION = 1

SettingKey = Literal[
    "electricity_rate_usd_per_kwh",
    "local_only",
    "daily_spend_cap_usd",
]

#: The allowlist. A key not named here cannot reach the file, whatever a caller
#: passes -- same posture as `serialize.py`'s payload allowlists.
MUTABLE_FIELDS: tuple[SettingKey, ...] = (
    "electricity_rate_usd_per_kwh",
    "local_only",
    "daily_spend_cap_usd",
)

Source = Literal["file", "env", "default"]


class SettingsError(ValueError):
    """A rejected patch. The message names the field and what was wrong."""


@dataclass(frozen=True)
class Resolved:
    """One field's value and where it came from.

    The source travels with the value because the UI has to distinguish
    "0.00 because nobody set it" from "0.00 because someone set it to zero",
    and those render differently.
    """

    value: Any
    source: Source


def data_dir() -> Path:
    """Where the volume is mounted. Same env var the providers store reads."""
    return Path(os.environ.get("DERATE_DATA_DIR", "/data"))


def settings_path(root: Path | None = None) -> Path:
    return (root or data_dir()) / "settings.json"


def _coerce(key: str, value: Any) -> Any:
    """Validate one field. Raises SettingsError with a reason, never silently
    coerces a nonsense value into a plausible one."""
    if key == "local_only":
        if not isinstance(value, bool):
            raise SettingsError(f"{key} must be true or false, got {type(value).__name__}")
        return value

    if key == "electricity_rate_usd_per_kwh":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingsError(f"{key} must be a number, got {type(value).__name__}")
        if value < 0:
            raise SettingsError(f"{key} must not be negative, got {value}")
        return float(value)

    if key == "daily_spend_cap_usd":
        # None and 0.0 are DIFFERENT INSTRUCTIONS and must not be collapsed.
        # None means "no cap". 0.0 means "spend nothing", which is a legitimate
        # way to say local-only while leaving the providers configured. A form
        # whose empty field yields 0.0 would silently turn one into the other.
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingsError(f"{key} must be a number or null, got {type(value).__name__}")
        if value < 0:
            raise SettingsError(f"{key} must not be negative, got {value}")
        return float(value)

    raise SettingsError(
        f"{key!r} is not a writable setting. Writable: {', '.join(MUTABLE_FIELDS)}."
    )


class SettingsStore:
    """Reads and writes the mutable-settings file. Owns no defaults."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings_path()

    def load(self) -> dict[str, Any]:
        """What the file says, allowlisted and validated.

        A corrupt or unreadable file logs and yields `{}`. Startup does not fail
        on it -- same posture as `ProviderStore.load`, and for the same reason:
        an operator locked out of a running cluster by a bad config file is
        worse off than one whose last preference was forgotten.
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("settings file unreadable, ignoring it: %s: %s", self.path, exc)
            return {}

        if not isinstance(raw, dict):
            log.warning("settings file is not an object, ignoring it: %s", self.path)
            return {}

        version = raw.get("version")
        if version != SCHEMA_VERSION:
            log.warning(
                "settings file schema %r is not %d, ignoring it: %s",
                version, SCHEMA_VERSION, self.path,
            )
            return {}

        body = raw.get("settings")
        if not isinstance(body, dict):
            return {}

        out: dict[str, Any] = {}
        for key in MUTABLE_FIELDS:
            if key not in body:
                continue
            try:
                out[key] = _coerce(key, body[key])
            except SettingsError as exc:
                # One bad field does not discard the others.
                log.warning("ignoring persisted setting: %s", exc)
        return out

    def save(self, values: dict[str, Any]) -> None:
        """Write the allowlisted subset, atomically, 0600."""
        body = {k: values[k] for k in MUTABLE_FIELDS if k in values}
        text = json.dumps(
            {"version": SCHEMA_VERSION, "settings": body}, indent=2, sort_keys=True
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".settings-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def patch(self, current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        """Validate a partial update, merge it over `current`, persist, return it.

        An unknown key is refused and names the writable set, rather than being
        dropped -- silently ignoring it is how a control ends up looking wired
        and doing nothing.
        """
        if not isinstance(patch, dict):
            raise SettingsError("body must be a JSON object")
        merged = dict(current)
        for key, value in patch.items():
            merged[key] = _coerce(key, value)
        self.save(merged)
        return merged


def resolve(
    file_values: dict[str, Any],
    env_values: dict[str, Any],
    defaults: dict[str, Any],
) -> dict[str, Resolved]:
    """Apply file > env > default and remember which one won for each field."""
    out: dict[str, Resolved] = {}
    for key in MUTABLE_FIELDS:
        if key in file_values:
            out[key] = Resolved(file_values[key], "file")
        elif key in env_values:
            out[key] = Resolved(env_values[key], "env")
        else:
            out[key] = Resolved(defaults.get(key), "default")
    return out
