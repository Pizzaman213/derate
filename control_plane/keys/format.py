"""The ``drt_<key_id>_<secret>`` key format: mint, parse, hash, verify.

Pure and standard-library only, so the redactor, the tests and the service
can all import it without opening a store. The secret is only ever held in
memory long enough to hash it; what is stored is ``sha256(secret)``.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import string

from control_plane.contracts.auth import KEY_ID_LEN, KEY_PREFIX, KEY_SECRET_BYTES

#: The alphabet of a key id: lowercase letters and digits.
KEY_ID_ALPHABET = string.ascii_lowercase + string.digits
_KEY_ID_CHARS = frozenset(KEY_ID_ALPHABET)

#: A whole raw key anywhere in a string. ``token_urlsafe(32)`` is 43 chars of
#: ``[A-Za-z0-9_-]``; 32 is the floor so a truncated paste still matches.
KEY_PATTERN = r"\bdrt_[a-z0-9]{10}_[A-Za-z0-9_\-]{32,}"


def new_key_id() -> str:
    return "".join(secrets.choice(KEY_ID_ALPHABET) for _ in range(KEY_ID_LEN))


def mint() -> tuple[str, str, str]:
    """A fresh ``(key_id, secret, raw)``; ``raw`` is what the caller is shown once."""
    key_id = new_key_id()
    secret = secrets.token_urlsafe(KEY_SECRET_BYTES)
    return key_id, secret, f"{KEY_PREFIX}{key_id}_{secret}"


def parse(raw: str) -> tuple[str, str] | None:
    """``(key_id, secret)`` from a raw key, or ``None`` when it is not one."""
    if not isinstance(raw, str) or not raw.startswith(KEY_PREFIX):
        return None
    parts = raw.removeprefix(KEY_PREFIX).split("_", 1)
    if len(parts) != 2:
        return None
    key_id, secret = parts
    if len(key_id) != KEY_ID_LEN or not set(key_id) <= _KEY_ID_CHARS:
        return None
    if not secret:
        return None
    return key_id, secret


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def verify(secret: str, stored_hash: str) -> bool:
    """Constant-time comparison of ``sha256(secret)`` with the stored digest."""
    return hmac.compare_digest(hash_secret(secret), stored_hash or "")
