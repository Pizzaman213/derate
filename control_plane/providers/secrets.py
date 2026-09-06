"""Key resolution and redaction.

The rule this module exists to enforce: ``api_key_ref`` is the *name* of an
environment variable or of a key in ``/data/secrets.json`` at mode 0600. It is
never the key itself. Values are resolved at request time and nowhere else.

Two mechanisms, deliberately belt and braces:

1. :class:`SecretStore` resolves a reference to a value and remembers every
   value it has ever handed out.
2. :class:`Redactor` scrubs those values, plus anything that merely *looks*
   like a key, out of any text on its way to a response, a log line, or disk.

The second exists because upstreams echo credentials back in error bodies.
A provider that answers ``401 {"message": "Invalid API key sk-or-v1-abc..."}``
would otherwise put a live key into an error the gateway hands to a client.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .config import REDACTED, SECRETS_FILE, data_dir

log = logging.getLogger(__name__)

# Token shapes common enough to be worth catching even when we never resolved
# them ourselves. Conservative: a known vendor prefix plus a long opaque tail.
_KEY_PATTERNS = [
    re.compile(r"\bsk-or-v1-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bsk-proj-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bgsk_[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}"),
    re.compile(r"(?i)\b(api[-_]?key|access[-_]?token)\s*[=:]\s*[\"']?[A-Za-z0-9._\-]{20,}"),
]

# Minimum length before we bother scrubbing a literal. Below this a "secret"
# is more likely to be a substring of ordinary prose than a credential.
_MIN_SCRUB_LEN = 8


class SecretsError(Exception):
    """Something is wrong with the secret store itself, not with a lookup."""


def looks_like_secret(value: str) -> bool:
    """True when a string looks like key material rather than a reference.

    Used to reject a provider spec where somebody pasted the key into
    ``api_key_ref`` or into ``base_url``. That mistake would otherwise put a
    live key into every listing, since a reference is safe to display.
    """
    if not value:
        return False
    if any(p.search(value) for p in _KEY_PATTERNS):
        return True
    # An env var name is short, uppercase-ish, and has no long random run.
    # Anything with 32+ contiguous mixed-case-and-digit characters is a key.
    return bool(re.search(r"[A-Za-z0-9_\-]{32,}", value) and re.search(r"\d", value)
                and re.search(r"[a-z]", value) and re.search(r"[A-Z0-9]", value))


class Redactor:
    """Scrubs known and probable key material out of text.

    One instance is shared by the store, the service, and the log filter, so a
    value resolved once is scrubbed everywhere afterwards.
    """

    def __init__(self) -> None:
        self._values: set[str] = set()

    def remember(self, value: str | None) -> None:
        if value and len(value) >= _MIN_SCRUB_LEN:
            self._values.add(value)

    def scrub(self, text: str) -> str:
        if not text:
            return text
        # Longest first, so an overlapping prefix cannot leave a tail behind.
        for value in sorted(self._values, key=len, reverse=True):
            if value in text:
                text = text.replace(value, REDACTED)
        for pattern in _KEY_PATTERNS:
            text = pattern.sub(REDACTED, text)
        return text

    def scrub_bytes(self, raw: bytes) -> bytes:
        return self.scrub(raw.decode("utf-8", "replace")).encode("utf-8")

    def contains_secret(self, text: str) -> bool:
        """True if any *remembered* value appears verbatim in the text.

        Deliberately narrower than :meth:`scrub`: this answers "did we leak a
        key we hold", which is the question the assertion below wants.
        """
        return any(v in text for v in self._values)

    def assert_clean(self, text: str, where: str) -> None:
        """Raise rather than emit key material. Used on every serialization path."""
        if self.contains_secret(text):
            raise SecretsError(f"refusing to emit key material in {where}")


class SecretRedactingFilter(logging.Filter):
    """Attach to any logger that could touch provider material.

    We do not log request bodies at all, but a stray exception message is
    exactly the sort of thing that carries a key into a log file.
    """

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - a broken record is not our problem
            return True
        scrubbed = self._redactor.scrub(message)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = ()
        return True


class SecretStore:
    """Resolves ``api_key_ref`` to a value. Environment first, then the file.

    Environment wins so an operator can override a stored secret without
    editing the file, and so a container can run with no secrets file at all.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        env: Mapping[str, str] | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else data_dir() / SECRETS_FILE
        self._env = env if env is not None else os.environ
        self.redactor = redactor or Redactor()
        self._cache: dict[str, str] = {}
        self._cache_mtime: float | None = None

    # -- reading -----------------------------------------------------------

    def _load_file(self) -> dict[str, str]:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self._cache, self._cache_mtime = {}, None
            return self._cache
        if self._cache_mtime is not None and st.st_mtime == self._cache_mtime:
            return self._cache
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            log.warning(
                "%s is group or world accessible (mode %o); expected 0600",
                self.path,
                stat.S_IMODE(st.st_mode),
            )
        try:
            raw = json.loads(self.path.read_text("utf-8") or "{}")
        except (OSError, json.JSONDecodeError) as exc:
            # Never include the exception's text: a JSON error quotes the
            # offending line, and that line is a secret.
            log.error("could not read %s (%s); treating as empty", self.path, type(exc).__name__)
            raw = {}
        values = {str(k): str(v) for k, v in raw.items() if isinstance(raw, dict)}
        for value in values.values():
            self.redactor.remember(value)
        self._cache, self._cache_mtime = values, st.st_mtime
        return values

    def has(self, ref: str) -> bool:
        return self.get(ref) is not None

    def get(self, ref: str) -> str | None:
        """Resolve a reference, or None. Never logs, never raises on a miss."""
        if not ref:
            return None
        value = self._env.get(ref)
        if value is None:
            value = self._load_file().get(ref)
        if value is not None:
            self.redactor.remember(value)
        return value

    # -- writing -----------------------------------------------------------

    def put(self, ref: str, value: str) -> None:
        """Store a secret at mode 0600. Only ever called from an explicit set-key path."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        current = dict(self._load_file())
        current[ref] = value
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".secrets-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(current, fh, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib_suppress():
                os.unlink(tmp)
            raise
        os.chmod(self.path, 0o600)
        self.redactor.remember(value)
        self._cache_mtime = None
        log.info("stored secret under reference %s", ref)  # the name, never the value

    def delete(self, ref: str) -> bool:
        current = dict(self._load_file())
        if ref not in current:
            return False
        del current[ref]
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".secrets-")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(current, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)
        self._cache_mtime = None
        return True

    def refs(self) -> list[str]:
        """Reference *names* present in the file. Names are safe to show."""
        return sorted(self._load_file())


class contextlib_suppress:
    """Tiny local suppressor; avoids an import purely for a cleanup path."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return True
