"""Signed-in credentials: the OAuth tokens behind a sign-in provider kind.

A separate file from ``secrets.json`` because these are not flat strings. An
access token expires within the hour and the refresh token rotates on every
use, so a record is several values that must be replaced together, plus the
issued client id and the account they belong to. ``SecretStore`` has no way to
say any of that.

``<data_dir>/oauth.json``, mode 0600, written atomically exactly as
``SecretStore.put`` writes ``secrets.json``. Every token this module reads or
writes is handed to the shared :class:`Redactor` first, so a token an upstream
echoes back in an error body is scrubbed like any key.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from control_plane import fsutil

from .secrets import Redactor, contextlib_suppress

log = logging.getLogger(__name__)

_VERSION = 1
_TOKEN_FIELDS = ("id_token", "access_token", "refresh_token")


@dataclass
class SignInRecord:
    provider_id: str
    #: OAuth client id, issued at registration or fixed by the provider protocol.
    #: Refresh and reauthorization use it; it is not a secret.
    client_id: str
    #: Stable authenticated account identity; reconnect must match it.
    subject: str
    email: str
    id_token: str
    access_token: str
    refresh_token: str
    expires_at: float
    scopes: list[str] = field(default_factory=list)
    saved_at: float = 0.0
    #: The refresh token was refused; only the user signing in again fixes it.
    needs_reauth: bool = False


def _record_from_dict(raw: dict) -> SignInRecord:
    return SignInRecord(
        provider_id=str(raw["provider_id"]),
        client_id=str(raw["client_id"]),
        subject=str(raw.get("subject", "")),
        email=str(raw.get("email", "")),
        id_token=str(raw.get("id_token", "")),
        access_token=str(raw.get("access_token", "")),
        refresh_token=str(raw.get("refresh_token", "")),
        expires_at=float(raw.get("expires_at", 0.0)),
        scopes=[str(s) for s in raw.get("scopes") or []],
        saved_at=float(raw.get("saved_at", 0.0)),
        needs_reauth=bool(raw.get("needs_reauth", False)),
    )


class OAuthStore:
    """``oauth.json``: one host id and a record per signed-in provider."""

    def __init__(self, path: Path, *, redactor: Redactor) -> None:
        self.path = Path(path)
        self.redactor = redactor
        self._cache: dict | None = None
        self._cache_mtime: float | None = None

    # -- reading -----------------------------------------------------------

    def _load(self) -> dict:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self._cache, self._cache_mtime = None, None
            return {"version": _VERSION, "host_id": "", "records": {}}
        if self._cache is not None and st.st_mtime == self._cache_mtime:
            return self._cache
        try:
            raw = json.loads(self.path.read_text("utf-8") or "{}")
        except (OSError, json.JSONDecodeError) as exc:
            # The type only: a JSON error quotes the offending line, and every
            # line of this file is a token.
            log.warning("could not read %s (%s); treating as empty", self.path, type(exc).__name__)
            raw = {}
        if not isinstance(raw, dict):
            log.warning("%s is not a JSON object; treating as empty", self.path)
            raw = {}
        records: dict[str, dict] = {}
        for pid, record in (raw.get("records") or {}).items():
            if not isinstance(record, dict):
                continue
            try:
                parsed = _record_from_dict(record)
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("skipping malformed sign-in record: %s", type(exc).__name__)
                continue
            self._remember(parsed)
            records[str(pid)] = asdict(parsed)
        data = {
            "version": _VERSION,
            "host_id": str(raw.get("host_id") or ""),
            "records": records,
        }
        self._cache, self._cache_mtime = data, st.st_mtime
        return data

    def _remember(self, record: SignInRecord) -> None:
        for name in _TOKEN_FIELDS:
            self.redactor.remember(getattr(record, name))

    def host_id(self) -> str:
        """This coordinator's ``ext_agent_host_id``. Minted once, then kept."""
        data = self._load()
        if data["host_id"]:
            return data["host_id"]
        data = {**data, "host_id": f"urn:uuid:{uuid.uuid4()}"}
        self._write(data)
        return data["host_id"]

    def get(self, provider_id: str) -> SignInRecord | None:
        raw = self._load()["records"].get(provider_id)
        return None if raw is None else _record_from_dict(raw)

    def token_values(self) -> list[str]:
        """Every token held, for a scanner that must know what not to find."""
        out: list[str] = []
        for raw in self._load()["records"].values():
            out.extend(v for v in (raw.get(name) for name in _TOKEN_FIELDS) if v)
        return out

    # -- writing -----------------------------------------------------------

    def put(self, record: SignInRecord) -> None:
        self._remember(record)
        data = self._load()
        records = dict(data["records"])
        records[record.provider_id] = asdict(record)
        self._write({**data, "records": records})
        log.info("stored sign-in for provider %s", record.provider_id)  # never a token

    def delete(self, provider_id: str) -> bool:
        data = self._load()
        if provider_id not in data["records"]:
            return False
        records = {k: v for k, v in data["records"].items() if k != provider_id}
        self._write({**data, "records": records})
        return True

    def _write(self, data: dict) -> None:
        """Atomic, 0600. The same sequence as ``SecretStore.put``."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".oauth-")
        try:
            fsutil.harden_fd(fd, self.path)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib_suppress():
                os.unlink(tmp)
            raise
        fsutil.harden_path(self.path)
        self._cache, self._cache_mtime = None, None
