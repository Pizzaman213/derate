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
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .config import SECRETS_FILE, data_dir

from control_plane import fsutil

log = logging.getLogger(__name__)

# -- redaction ---------------------------------------------------------------
#
# Moved to ``control_plane/redaction.py`` and re-exported here, unchanged.
# ``logfiles.py`` needs the scrubber on every node, and a worker importing this
# package would pull httpx and the whole provider stack that ``node.py``'s
# docstring promises the worker path never touches. Every existing import site
# -- ``from .secrets import Redactor``, ``from control_plane.providers import
# looks_like_secret`` -- still resolves through these names.
from control_plane.redaction import (  # noqa: F401
    REDACTED,
    Redactor,
    SecretRedactingFilter,
    SecretsError,
    has_known_key_shape,
    looks_like_secret,
)


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
        if not isinstance(raw, dict):
            # Valid JSON, wrong shape (a list, a string, a number...). This
            # file is optional and best-effort: never fail startup over it,
            # and never log its content — the whole point of the file is
            # that its content is secret, well-formed or not.
            log.warning(
                "%s is valid JSON but not an object (got %s); treating as empty",
                self.path,
                type(raw).__name__,
            )
            raw = {}
        values = {str(k): str(v) for k, v in raw.items()}
        for value in values.values():
            self.redactor.remember(value)
        self._cache, self._cache_mtime = values, st.st_mtime
        return values

    def has(self, ref: str) -> bool:
        return self.get(ref) is not None

    def env_ref(self, ref: str) -> str | None:
        """The *environment's* value for a reference, or None.

        Exposed separately from :meth:`get` because the environment takes
        precedence over the file. A caller about to :meth:`put` needs to know
        whether the name it is writing to is already shadowed -- after the
        write, ``get`` returns the environment's value and the question can no
        longer be asked.
        """
        if not ref:
            return None
        value = self._env.get(ref)
        if value is not None:
            self.redactor.remember(value)
        return value

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
            fsutil.harden_fd(fd, self.path)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(current, fh, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib_suppress():
                os.unlink(tmp)
            raise
        fsutil.harden_path(self.path)
        self.redactor.remember(value)
        self._cache_mtime = None
        log.info("stored secret under reference %s", ref)  # the name, never the value

    def delete(self, ref: str) -> bool:
        current = dict(self._load_file())
        if ref not in current:
            return False
        del current[ref]
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".secrets-")
        fsutil.harden_fd(fd, self.path)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(current, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.path)
        fsutil.harden_path(self.path)
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
