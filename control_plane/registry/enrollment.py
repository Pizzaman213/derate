"""Short-lived enrollment tokens: the credential you are meant to copy.

The cluster token in :mod:`identity` is permanent. It is printed once, to the
coordinator's stdout, and the documented way to bring up a second machine is to
carry it there by hand -- so the forever-secret ends up in a shell history, a
chat message, and a screenshot. It also only ever buys you *candidate* status,
so a human still has to find the UI and click Admit.

An enrollment token is the other half. It is minted on demand, expires (one
hour by default), is spent after a fixed number of uses (one by default), can
be revoked, and -- because minting it *is* the admission decision, made in
advance -- it admits the joiner straight to member. ``Registry.add_node``
already takes that position for a human typing an address; this is the same act
with the typing done on the other machine.

What this module deliberately does not do:

* It is not authentication for the ``/api`` surface. That surface has none
  (see AUDIT-2026-09-06), and ``POST /api/nodes/{id}/admit`` is already open to
  anyone who can reach the port. Enrollment tokens do not widen that; they also
  do not narrow it, and must not be described as fixing it.
* It never replaces the cluster token. A node admitted this way is handed the
  permanent token on the way in, so the enrollment token expiring cannot later
  lock an established member out of its own cluster.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from control_plane.paths import data_dir as _data_dir

from control_plane import fsutil

log = logging.getLogger(__name__)

ENROLLMENT_FILE = "enrollments.json"

# One hour, one machine. Both are the common case -- you are standing at the
# box you are about to install -- and both bound the damage if the command
# lands somewhere it should not.
DEFAULT_TTL_S = 3600.0
DEFAULT_USES = 1

# An hour is the default, a day is the ceiling. Past that, use the cluster
# token and admit by hand: a week-long auto-admit credential is a standing
# invitation, not a convenience.
MAX_TTL_S = 86400.0

TOKEN_PREFIX = "ej_"


@dataclass(frozen=True)
class EnrollmentToken:
    """One minted credential. ``token`` is the secret; everything else is not."""

    token: str
    token_id: str
    created_at: float
    expires_at: float
    # None means unlimited uses within the TTL. Not the default, and not
    # reachable from the UI -- it exists for provisioning a rack at once.
    uses_remaining: int | None
    auto_admit: bool = True

    def is_live(self, now: float) -> bool:
        if now >= self.expires_at:
            return False
        return self.uses_remaining is None or self.uses_remaining > 0

    def public(self, now: float) -> dict:
        """What may be shown. Never the secret.

        The key is ``token_id``, not ``token``: the UI's ``scrub()`` blanks any
        field literally named ``token``, and more to the point a list of live
        credentials has no reason to carry them.
        """
        return {
            "token_id": self.token_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "expires_in_s": max(0.0, self.expires_at - now),
            "uses_remaining": self.uses_remaining,
            "auto_admit": self.auto_admit,
        }


def _new_token() -> tuple[str, str]:
    """(token_id, token). The id is a visible handle, the token is the secret."""
    token_id = secrets.token_hex(4)
    return token_id, f"{TOKEN_PREFIX}{token_id}_{secrets.token_urlsafe(24)}"


class EnrollmentStore:
    """Mint, verify, spend and revoke enrollment tokens.

    Persisted so that a coordinator restart in the middle of an install does
    not strand the machine halfway through it. An unwritable data volume is a
    warning and an in-memory store, matching ``load_or_create_identity``: the
    cluster still forms, the tokens just do not survive a restart.
    """

    def __init__(
        self,
        data_dir: Path | str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        root = Path(data_dir) if data_dir is not None else _data_dir()
        self._path = root / ENROLLMENT_FILE
        self._clock = clock
        self._tokens: dict[str, EnrollmentToken] = {}
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, ValueError) as exc:
            log.warning("could not read %s (%s); starting with no tokens", self._path, exc)
            return
        for row in raw.get("tokens", []):
            try:
                token = EnrollmentToken(
                    token=str(row["token"]),
                    token_id=str(row["token_id"]),
                    created_at=float(row["created_at"]),
                    expires_at=float(row["expires_at"]),
                    uses_remaining=(
                        None if row.get("uses_remaining") is None
                        else int(row["uses_remaining"])
                    ),
                    auto_admit=bool(row.get("auto_admit", True)),
                )
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("skipping malformed enrollment record (%s)", exc)
                continue
            self._tokens[token.token_id] = token
        self._prune()

    def _save(self) -> None:
        payload = {
            "tokens": [
                {
                    "token": t.token,
                    "token_id": t.token_id,
                    "created_at": t.created_at,
                    "expires_at": t.expires_at,
                    "uses_remaining": t.uses_remaining,
                    "auto_admit": t.auto_admit,
                }
                for t in self._tokens.values()
            ]
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # 0600 at creation, before any byte is written, so the tokens are
            # never briefly world-readable. Same as identity.py.
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(payload, handle)
            fsutil.harden_path(self._path)
        except OSError as exc:
            log.warning(
                "could not persist enrollment tokens to %s (%s); they will not "
                "survive a restart",
                self._path,
                exc,
            )

    def _prune(self) -> bool:
        """Drop expired and spent tokens. True when anything was dropped."""
        now = self._clock()
        dead = [tid for tid, t in self._tokens.items() if not t.is_live(now)]
        for tid in dead:
            del self._tokens[tid]
        return bool(dead)

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    def mint(
        self,
        ttl_s: float = DEFAULT_TTL_S,
        uses: int | None = DEFAULT_USES,
        auto_admit: bool = True,
    ) -> EnrollmentToken:
        ttl_s = float(ttl_s)
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        if ttl_s > MAX_TTL_S:
            raise ValueError(f"ttl_s must be at most {int(MAX_TTL_S)} seconds")
        if uses is not None and int(uses) < 1:
            raise ValueError("uses must be at least 1, or null for unlimited")

        now = self._clock()
        token_id, secret = _new_token()
        token = EnrollmentToken(
            token=secret,
            token_id=token_id,
            created_at=now,
            expires_at=now + ttl_s,
            uses_remaining=None if uses is None else int(uses),
            auto_admit=bool(auto_admit),
        )
        self._prune()
        self._tokens[token_id] = token
        self._save()
        log.info(
            "minted enrollment token %s (ttl %.0fs, uses %s)",
            token_id,
            ttl_s,
            "unlimited" if uses is None else uses,
        )
        return token

    def verify(self, presented: str | None) -> EnrollmentToken | None:
        """The live token this string is, or None.

        Compared against every live token with ``hmac.compare_digest`` rather
        than looked up by the id embedded in the prefix: the loop costs nothing
        at this scale and keeps the comparison off the length of a shared
        prefix.
        """
        if not presented:
            return None
        if self._prune():
            self._save()
        for token in self._tokens.values():
            if hmac.compare_digest(token.token, str(presented)):
                return token
        return None

    def consume(self, token_id: str) -> None:
        """Spend one use. A token that hits zero is dropped, not kept at zero."""
        token = self._tokens.get(token_id)
        if token is None or token.uses_remaining is None:
            return
        remaining = token.uses_remaining - 1
        if remaining <= 0:
            del self._tokens[token_id]
            log.info("enrollment token %s spent its last use", token_id)
        else:
            self._tokens[token_id] = EnrollmentToken(
                token=token.token,
                token_id=token.token_id,
                created_at=token.created_at,
                expires_at=token.expires_at,
                uses_remaining=remaining,
                auto_admit=token.auto_admit,
            )
        self._save()

    def revoke(self, token_id: str) -> bool:
        if self._tokens.pop(token_id, None) is None:
            return False
        self._save()
        log.info("revoked enrollment token %s", token_id)
        return True

    def live(self) -> list[EnrollmentToken]:
        if self._prune():
            self._save()
        return sorted(self._tokens.values(), key=lambda t: t.created_at)

    def public_list(self) -> list[dict]:
        now = self._clock()
        return [t.public(now) for t in self.live()]
