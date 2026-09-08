"""Key material, recognised and scrubbed. No dependencies but the standard library.

This started inside ``providers/secrets.py`` and stayed there while the only
thing that could touch a credential was a provider. It is here because that
stopped being true: ``control_plane/logfiles.py`` writes log files on every
node, worker and coordinator alike, and cannot write a line it has not scrubbed
-- while ``control_plane/node.py``'s own contract is that the worker path
imports nothing from ``control_plane.providers``, which pulls httpx and eleven
modules a worker has no use for. Two rules that cannot both hold with the
scrubber in the providers package, and one small module that lets both hold.

``providers/secrets.py`` re-exports every name below, so
``from .secrets import Redactor`` and ``from control_plane.providers import
looks_like_secret`` keep working and nothing else moved.

The rule the whole thing exists to enforce: ``api_key_ref`` is the *name* of an
environment variable or of a key in ``secrets.json`` at mode 0600, never the
key itself. :class:`Redactor` is the backstop for when something goes wrong
anyway -- upstreams echo credentials back in error bodies, and a provider that
answers ``401 {"message": "Invalid API key sk-or-v1-abc..."}`` would otherwise
put a live key into an error the gateway hands to a client, or into a log file
somebody pastes into an issue.
"""

from __future__ import annotations

import logging
import re

#: What a scrubbed value is replaced with. The canonical spelling -- see
#: ``control_plane/contracts/derived.py``, which fails a test if a copy of it
#: anywhere else stops matching.
REDACTED = "***"


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


def has_known_key_shape(value: str) -> bool:
    """True only for a *recognised* credential shape -- no length heuristic.

    The screen :func:`looks_like_secret` applies is right for a field that is
    stored and echoed, where a false positive costs a rename and a false
    negative leaks a key. It is wrong for a field that is neither, because its
    fallback -- 32+ contiguous mixed-case-and-digit characters -- is also the
    shape of an ordinary model name. Every GGUF variant of
    ``mistralai/Mistral-Small-24B-Instruct-2501`` is refused by it, while
    ``Qwen2.5`` and ``Llama-3.1`` pass because their dots break the run: a
    whole model family failing in a way that reads as random.

    So this keeps the vendor-prefixed patterns, which have no false positives
    worth the name, and drops the entropy guess. Use it where the value is
    passed through to an upstream and never persisted or displayed.
    """
    if not value:
        return False
    return any(p.search(value) for p in _KEY_PATTERNS)


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

