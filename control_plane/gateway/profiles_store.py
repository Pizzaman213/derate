"""Saved launch profiles and pinned models, shared by every admin and browser.

One file, ``data_dir()/profiles.json``, holding ``{"version": 1, "profiles":
[...], "pins": [...]}`` -- the same envelope, atomic 0600 write and
corrupt-file-loads-as-empty posture as ``settings_store.py``. One lock guards
read-modify-write so two admins saving at once cannot drop each other's edit.

A profile's ``request`` is a ``POST /api/deployments`` body (the keys
``deploy/relaunch.py`` emits) plus ``target``. Its VALUES are not judged here:
the plan route judges them when the profile is loaded, which is the only place
that knows what this cluster can serve. Its KEYS are, because an unknown key
stored now is a control that silently does nothing later.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from control_plane import fsutil
from control_plane.paths import data_dir

log = logging.getLogger("gateway.profiles_store")

SCHEMA_VERSION = 1

#: The launch keys ``relaunch.launch_request_from`` can emit, plus ``target``.
REQUEST_KEYS: frozenset[str] = frozenset({
    "model_id", "served_name", "runtime", "context", "concurrency", "node_ids",
    "parallelism", "kv_dtype", "dtype", "speculative", "speculative_tokens",
    "extra_args", "custom_command", "enforce_eager", "cudagraph_capture_sizes",
    "trust_remote_code", "engine_options", "lifecycle", "target",
})

MAX_REQUEST_BYTES = 16 * 1024
MAX_NAME = 80
MAX_PINS = 200
#: Unstarred auto-saved runs kept per model; starred runs are never pruned.
MAX_RUNS_PER_MODEL = 10
#: Keys that name a deployment rather than describe its setup.
_IDENTITY_KEYS = frozenset({"served_name", "lifecycle"})


def same_setup(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Two launch requests describe the same setup, whatever they were called."""
    strip = lambda r: {k: v for k, v in (r or {}).items() if k not in _IDENTITY_KEYS}  # noqa: E731
    return strip(a) == strip(b)


def _with_defaults(p: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "saved", "deployment_id": None, "last_result": None, **p}


class ProfileError(ValueError):
    """A rejected profile or pins write; the message says what was wrong."""


class ProfileNotFound(KeyError):
    pass


def profiles_path(root: Path | None = None) -> Path:
    return (root or data_dir()) / "profiles.json"


def _name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileError("name must be a non-empty string.")
    value = value.strip()
    if len(value) > MAX_NAME:
        raise ProfileError(f"name must be at most {MAX_NAME} characters.")
    return value


def _request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProfileError("request must be a JSON object.")
    unknown = sorted(set(value) - REQUEST_KEYS)
    if unknown:
        raise ProfileError(
            f"request has keys a launch does not take: {', '.join(unknown)}. "
            f"Allowed: {', '.join(sorted(REQUEST_KEYS))}."
        )
    model_id = value.get("model_id")
    if not isinstance(model_id, str) or not model_id.strip():
        raise ProfileError("request.model_id must be a non-empty string.")
    try:
        size = len(json.dumps(value, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ProfileError(f"request is not JSON-encodable: {exc}") from None
    if size > MAX_REQUEST_BYTES:
        raise ProfileError(f"request is {size} bytes; at most {MAX_REQUEST_BYTES} are kept.")
    return value


def _bool(key: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ProfileError(f"{key} must be true or false.")
    return value


def _model_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ProfileError("model_id must be a non-empty string.")
    return value.strip()


class ProfileStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        # Resolved lazily so DERATE_DATA_DIR set after construction still wins.
        return self._path or profiles_path()

    # -- file ---------------------------------------------------------------

    def _load(self) -> dict[str, list]:
        empty: dict[str, list] = {"profiles": [], "pins": []}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return empty
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("profiles file unreadable, ignoring it: %s: %s", self.path, exc)
            return empty
        if not isinstance(raw, dict) or raw.get("version") != SCHEMA_VERSION:
            log.warning("profiles file has an unknown shape, ignoring it: %s", self.path)
            return empty
        profiles = [_with_defaults(p) for p in raw.get("profiles") or [] if isinstance(p, dict)
                    and isinstance(p.get("profile_id"), str)]
        pins = [p for p in raw.get("pins") or [] if isinstance(p, str)]
        return {"profiles": profiles, "pins": pins}

    def _save(self, body: dict[str, list]) -> None:
        text = json.dumps({"version": SCHEMA_VERSION, **body}, indent=2, sort_keys=True)
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".profiles-")
        try:
            fsutil.harden_fd(fd, path)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- profiles -----------------------------------------------------------

    def list(self, model_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._load()["profiles"]
        if model_id is not None:
            rows = [p for p in rows if p.get("model_id") == model_id]
        return sorted(rows, key=lambda p: p.get("created_at") or 0, reverse=True)

    def get(self, profile_id: str) -> dict[str, Any] | None:
        with self._lock:
            return next((p for p in self._load()["profiles"]
                         if p["profile_id"] == profile_id), None)

    def create(self, body: Any, *, actor: str) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise ProfileError("Body must be a JSON object.")
        unknown = sorted(set(body) - {"model_id", "name", "request", "is_default"})
        if unknown:
            raise ProfileError(f"unknown fields: {', '.join(unknown)}.")
        now = time.time()
        profile = {
            "profile_id": "p-" + secrets.token_hex(6),
            "model_id": _model_id(body.get("model_id")),
            "name": _name(body.get("name")),
            "request": _request(body.get("request")),
            "is_default": _bool("is_default", body.get("is_default", False)),
            "created_at": now,
            "updated_at": now,
            "created_by": actor,
            "kind": "saved",
            "deployment_id": None,
            "last_result": None,
        }
        with self._lock:
            data = self._load()
            if profile["is_default"]:
                self._clear_default(data, profile["model_id"])
            data["profiles"].append(profile)
            self._save(data)
        return profile

    def patch(self, profile_id: str, body: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        """Returns ``(before, after)``. Raises ProfileNotFound / ProfileError."""
        if not isinstance(body, dict):
            raise ProfileError("Body must be a JSON object.")
        unknown = sorted(set(body) - {"name", "request", "is_default"})
        if unknown:
            raise ProfileError(f"unknown fields: {', '.join(unknown)}.")
        changes: dict[str, Any] = {}
        if "name" in body:
            changes["name"] = _name(body["name"])
        if "request" in body:
            changes["request"] = _request(body["request"])
        if "is_default" in body:
            changes["is_default"] = _bool("is_default", body["is_default"])
        with self._lock:
            data = self._load()
            idx = next((i for i, p in enumerate(data["profiles"])
                        if p["profile_id"] == profile_id), None)
            if idx is None:
                raise ProfileNotFound(profile_id)
            before = data["profiles"][idx]
            if "request" in changes and before.get("kind") == "run":
                raise ProfileError(
                    "a run's settings are what was launched; save a copy to change them.")
            after = {**before, **changes, "updated_at": time.time()}
            if changes.get("is_default"):
                self._clear_default(data, after["model_id"])
            data["profiles"][idx] = after
            self._save(data)
        return before, after

    def delete(self, profile_id: str) -> dict[str, Any]:
        with self._lock:
            data = self._load()
            hit = next((p for p in data["profiles"] if p["profile_id"] == profile_id), None)
            if hit is None:
                raise ProfileNotFound(profile_id)
            data["profiles"] = [p for p in data["profiles"] if p is not hit]
            self._save(data)
        return hit

    def record_run(self, *, model_id: str, request: dict[str, Any], deployment_id: str,
                   served_name: str, actor: str) -> dict[str, Any]:
        """Save what an operator launch ran, then prune old unstarred runs."""
        now = time.time()
        stamp = time.strftime("%Y-%m-%d %H:%M", time.gmtime(now))
        profile = {
            "profile_id": "p-" + secrets.token_hex(6),
            "model_id": model_id,
            "name": f"Run {served_name} · {stamp} UTC"[:MAX_NAME],
            "request": dict(request),
            "is_default": False,
            "created_at": now,
            "updated_at": now,
            "created_by": actor,
            "kind": "run",
            "deployment_id": deployment_id,
            "last_result": None,
        }
        with self._lock:
            data = self._load()
            data["profiles"].append(profile)
            # Newest first; file order breaks a timestamp tie (appends are newer).
            runs = [p for _, p in sorted(
                ((i, p) for i, p in enumerate(data["profiles"])
                 if p.get("model_id") == model_id
                 and p.get("kind") == "run" and not p.get("is_default")),
                key=lambda ip: (ip[1].get("created_at") or 0, ip[0]), reverse=True)]
            drop = {p["profile_id"] for p in runs[MAX_RUNS_PER_MODEL:]}
            if drop:
                data["profiles"] = [p for p in data["profiles"] if p["profile_id"] not in drop]
            self._save(data)
        return profile

    def attach_result(self, request: dict[str, Any], result: dict[str, Any]) -> int:
        """Set ``last_result`` on every profile with the same setup; returns the count."""
        with self._lock:
            data = self._load()
            count = 0
            for i, p in enumerate(data["profiles"]):
                if same_setup(p.get("request") or {}, request):
                    data["profiles"][i] = {**p, "last_result": dict(result)}
                    count += 1
            if count:
                self._save(data)
        return count

    @staticmethod
    def _clear_default(data: dict[str, list], model_id: str) -> None:
        for i, p in enumerate(data["profiles"]):
            if p.get("model_id") == model_id and p.get("is_default"):
                data["profiles"][i] = {**p, "is_default": False}

    # -- pins ---------------------------------------------------------------

    def pins(self) -> list[str]:
        with self._lock:
            return list(self._load()["pins"])

    def set_pins(self, pins: Any) -> tuple[list[str], list[str]]:
        """Returns ``(before, after)``."""
        if not isinstance(pins, list) or not all(isinstance(p, str) and p.strip() for p in pins):
            raise ProfileError("pins must be a list of model ids.")
        if len(set(pins)) != len(pins):
            raise ProfileError("pins must not repeat a model id.")
        if len(pins) > MAX_PINS:
            raise ProfileError(f"at most {MAX_PINS} models can be pinned.")
        with self._lock:
            data = self._load()
            before = data["pins"]
            data["pins"] = list(pins)
            self._save(data)
        return before, list(pins)
