"""``AuthService``: the ``AuthPort`` over ``auth.db``.

Every method is synchronous and takes the store's ``RLock``; the gateway calls
them through ``asyncio.to_thread`` where it matters. Any SQLite failure after
open surfaces as ``AuthUnavailable`` so a broken store fails closed (503), never
open.

Rules that are not obvious from the Protocol:

* ``login`` always runs one argon2 verify -- against a fixed dummy hash when
  the username is unknown -- so response time does not reveal which usernames
  exist. The exceptions are a locked login, which verifies nothing (and
  records no attempt): checking a password while locked would turn the lock
  into an oracle; and an oversized identifier or password, which verifies the
  dummy hash once and records nothing.
* Lockout is keyed on the identifier as typed (case-folded), never on the
  admin it resolves to, so a 429 cannot link an email to its username.
  ``LOGIN_LOCK_THRESHOLD`` failures from one (identifier, ip) pair lock that
  pair until ``last + min(LOGIN_LOCK_MAX_S, LOGIN_LOCK_BASE_S * 2**(n - 5))``.
  Failures for the identifier from addresses that are not themselves locked
  add up too, but only *delay* the identifier (``login_delay_s``, at most
  ``LOGIN_DELAY_MAX_S``) -- a stranger can type the admin's name, so refusing
  on it would let two addresses keep the real admin out indefinitely.
  Failures from one address across identifiers delay that address too.
  The attempt row is written as a failure in the same ``BEGIN IMMEDIATE``
  that read the lock state and flipped to ``ok=1`` on success.
  A success clears nothing: failures age out of the window. The lock onset
  writes one ``login_locked`` audit row; attempts during a hold write a
  ``gateway.security`` warning and nothing else. ``reauthenticate`` runs the
  same path without minting a session.
* A session's ``last_seen_at`` is written at most once per
  ``_TOUCH_INTERVAL_S``, so a busy UI does not cost a write per request; idle
  expiry is therefore accurate to that interval.
* ``change_password``, ``remove_admin`` and ``revoke_all`` revoke every
  session of that admin, including the one that made the change; the UI logs
  in again.
* Audit ``before``/``after`` never carry a password or a hash.
* Every audit row from v3 on is hash-chained to the one before it inside the
  write transaction, then copied to the JSONL mirror and the remote sink after
  the transaction commits. A mirror or sink failure is logged, never raised.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from control_plane import auditlog
from control_plane.redaction import printable
from control_plane.contracts.auth import (
    EMAIL_MAX_LEN,
    LOGIN_DELAY_MAX_S,
    LOGIN_LOCK_BASE_S,
    LOGIN_LOCK_MAX_S,
    LOGIN_LOCK_THRESHOLD,
    LOGIN_LOCK_USERNAME_MAX_S,
    LOGIN_LOCK_WINDOW_S,
    LOGIN_PASSWORD_MAX_LEN,
    PASSWORD_MIN_LEN,
    SESSION_IDLE_S,
    SESSION_MAX_S,
    Admin,
    AuditAction,
    AuditEntry,
    AuthUnavailable,
    ChainReport,
    InvalidCredentials,
    LoginLocked,
    Session,
)

from . import store

if TYPE_CHECKING:
    from control_plane.auditlog import AuditMirror, AuditSink

security_log = logging.getLogger("gateway.security")
log = logging.getLogger("auth")

#: ``last_seen_at`` is rewritten no more often than this.
_TOUCH_INTERVAL_S = 60.0
USERNAME_MAX_LEN = 64
#: Printable ASCII, any punctuation (``! # $ % & * + ? ~ ^``...), but no space
#: and no ``/``: a username is one path segment in ``DELETE
#: /api/admins/{username}``, and a slash would make it two.
_USERNAME_RE = re.compile(r"^[!-.0-~]+$")
#: A shape check, not RFC 5322: one ``@``, a dot after it, no whitespace and
#: no ``/``. Whether the mailbox exists is not something this service can know.
_EMAIL_RE = re.compile(r"^[^\s@/]+@[^\s@/]+\.[^\s@/]+$")
_ADMIN = "admin"
_SESSION = "session"
#: A name shorter than this is not looked for inside a password: "containing
#: the username" stops meaning anything for a two-letter name.
_CONTAINED_MIN_LEN = 3
#: Passwords that meet the length rule and are still the first guesses of any
#: list. Compared case-insensitively.
_DENYLIST = frozenset(
    {
        "password1234",
        "password12345",
        "password123456",
        "123456789012",
        "1234567890123",
        "12345678901234",
        "qwertyuiop12",
        "qwertyuiop123",
        "qwertyuiopasdf",
        "1q2w3e4r5t6y",
        "letmein12345",
        "iloveyou1234",
        "welcome12345",
        "adminadmin12",
        "administrator",
        "changeme1234",
        "passw0rd1234",
        "aaaaaaaaaaaa",
        "abcdefghijkl",
        "abc123abc123",
    }
)


def hash_cookie(cookie_value: str) -> str:
    return hashlib.sha256(cookie_value.encode("utf-8")).hexdigest()


def _valid_username(username: str) -> str:
    name = (username or "").strip()
    if not name:
        raise ValueError("username must not be empty")
    if len(name) > USERNAME_MAX_LEN:
        raise ValueError(f"username must be at most {USERNAME_MAX_LEN} characters")
    if not _USERNAME_RE.match(name):
        raise ValueError(
            "username may contain letters, digits and punctuation, but no spaces or /"
        )
    return name


def _valid_password(
    password: str, *, username: str | None = None, email: str | None = None
) -> str:
    if not isinstance(password, str) or len(password) < PASSWORD_MIN_LEN:
        raise ValueError(f"password must be at least {PASSWORD_MIN_LEN} characters")
    folded = password.casefold()
    if folded in _DENYLIST:
        raise ValueError("password is on the list of the most common passwords; choose another")
    for label, value in (
        ("username", username),
        ("email", (email or "").split("@", 1)[0] or None),
    ):
        if value and len(value) >= _CONTAINED_MIN_LEN and value.casefold() in folded:
            raise ValueError(f"password must not contain the {label}; choose another")
    return password


def _valid_email(email: str | None) -> str | None:
    """Stripped and kept in its original case; uniqueness ignores case (the index)."""
    if email is None:
        return None
    value = email.strip() if isinstance(email, str) else ""
    if not value:
        raise ValueError("email must not be empty")
    if len(value) > EMAIL_MAX_LEN:
        raise ValueError(f"email must be at most {EMAIL_MAX_LEN} characters")
    if not _EMAIL_RE.match(value):
        raise ValueError("email must look like name@example.com")
    return value


def _session(row: sqlite3.Row) -> Session:
    return Session(
        session_id=row["sid_hash"],
        username=row["username"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        expires_at=row["expires_at"],
        ip=row["ip"],
        user_agent=row["user_agent"],
        login_ip=row["login_ip"],
    )


def _admin(row: sqlite3.Row) -> Admin:
    return Admin(
        username=row["username"],
        email=row["email"],
        created_at=row["created_at"],
        created_by=row["created_by"],
    )


def _lock_key(identifier: str) -> str:
    """What lockout counts against: the identifier as typed, case-folded."""
    return identifier.casefold()


class AdminExists(ValueError):
    """``create_first_admin`` found an admin already there: setup is closed."""


class AuthService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        lock,
        *,
        clock: Callable[[], float] = time.time,
        hasher: PasswordHasher | None = None,
        mirror: "AuditMirror | None" = None,
        sink: "AuditSink | None" = None,
    ) -> None:
        self._conn = conn
        self._lock = lock
        self._clock = clock
        self._ph = hasher or PasswordHasher()
        # Verified against when the username is unknown, so that path costs
        # the same argon2 work as a real mismatch.
        self._dummy_hash = self._ph.hash(secrets.token_urlsafe(24))
        self._mirror = mirror
        self._sink = sink
        self._host = auditlog.hostname()
        # Audit rows inserted by the open write transaction, published to the
        # mirror and the sink once it commits (dropped if it rolls back).
        self._unpublished: list[dict] = []
        self._unpublished_rows: dict[int, int] = {}
        # The audit table's row count, for the mirror's HEAD anchor: counted
        # once, then kept by _audit; forgotten on a rollback.
        self._audit_rows: int | None = None

    @classmethod
    def open(
        cls,
        path: Path | str,
        *,
        clock: Callable[[], float] = time.time,
        hasher: PasswordHasher | None = None,
        mirror: "AuditMirror | None" = None,
        sink: "AuditSink | None" = None,
    ) -> "AuthService":
        conn, lock = store.open_database(path)
        return cls(conn, lock, clock=clock, hasher=hasher, mirror=mirror, sink=sink)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- plumbing -----------------------------------------------------------

    @contextmanager
    def _db(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                if write:
                    self._conn.execute("BEGIN IMMEDIATE")
                try:
                    yield self._conn
                except BaseException:
                    if write:
                        self._unpublished.clear()
                        self._unpublished_rows.clear()
                        self._audit_rows = None
                        self._conn.execute("ROLLBACK")
                    raise
                if write:
                    self._conn.execute("COMMIT")
            except sqlite3.Error as exc:
                self._unpublished.clear()
                self._unpublished_rows.clear()
                self._audit_rows = None
                raise AuthUnavailable(f"the auth store failed: {exc}") from exc
            if write and self._unpublished:
                rows, self._unpublished = self._unpublished, []
                counts, self._unpublished_rows = self._unpublished_rows, {}
                self._publish(rows, counts)

    def _publish(self, rows: list[dict], counts: dict[int, int]) -> None:
        """*counts* maps an audit row id to the table's row count with it
        in, which the mirror writes into ``HEAD``."""
        for row in rows:
            if self._mirror is not None:
                try:
                    self._mirror.write(row, rows=counts.get(row["id"]))
                except Exception:  # the mirror never fails an audit write
                    log.exception("audit mirror write failed")
            if self._sink is not None:
                try:
                    self._sink.send(row)
                except Exception:
                    log.exception("audit sink enqueue failed")

    def _audit(
        self,
        conn: sqlite3.Connection,
        actor: str,
        action: AuditAction,
        target_kind: str,
        target_id: str,
        *,
        before: dict | None = None,
        after: dict | None = None,
        ip: str | None = None,
        ts: float | None = None,
        request_id: str | None = None,
        user_agent: str | None = None,
        session_id: str | None = None,
        status: int | None = None,
    ) -> None:
        row = {
            "ts": self._clock() if ts is None else ts,
            "actor": (actor or "-")[:EMAIL_MAX_LEN],
            "action": AuditAction(action).value,
            "target_kind": target_kind,
            "target_id": target_id,
            "before_json": store.dump(before),
            "after_json": store.dump(after),
            "ip": ip,
            "request_id": request_id,
            "user_agent": user_agent[:512] if user_agent else None,
            "session_id": session_id,
            "status": None if status is None else int(status),
        }
        last = conn.execute("SELECT row_hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
        row["prev_hash"] = (last["row_hash"] if last else None) or ""
        row["row_hash"] = store.chain_hash(row["prev_hash"], row)
        columns = (*store.CHAIN_COLUMNS, "prev_hash", "row_hash")
        cur = conn.execute(
            f"INSERT INTO audit({', '.join(columns)}) VALUES({', '.join('?' * len(columns))})",
            tuple(row[c] for c in columns),
        )
        if self._audit_rows is None:
            self._audit_rows = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        else:
            self._audit_rows += 1
        self._unpublished.append(
            {
                "kind": "audit",
                "id": cur.lastrowid,
                **row,
                "ts_iso": auditlog.ts_iso(row["ts"]),
                "host": self._host,
            }
        )
        self._unpublished_rows[cur.lastrowid] = self._audit_rows

    def _verify(self, password_hash: str | None, password: str) -> bool:
        try:
            return self._ph.verify(password_hash or self._dummy_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    # -- admins -------------------------------------------------------------

    def admin_exists(self) -> bool:
        with self._db() as conn:
            return conn.execute("SELECT 1 FROM admins LIMIT 1").fetchone() is not None

    def create_admin(
        self,
        username: str,
        password: str,
        *,
        email: str | None = None,
        created_by: str | None,
        ip: str | None,
        request_id: str | None = None,
        user_agent: str | None = None,
        session_id: str | None = None,
    ) -> Admin:
        return self._insert_admin(
            username, password, email=email, created_by=created_by, ip=ip,
            request_id=request_id, user_agent=user_agent, session_id=session_id,
            first=False,
        )

    def create_first_admin(
        self,
        username: str,
        password: str,
        *,
        email: str | None = None,
        ip: str | None,
        request_id: str | None = None,
        user_agent: str | None = None,
        session_id: str | None = None,
    ) -> Admin:
        """The first admin, or :class:`AdminExists` when there already is
        one. The check and the insert share one ``BEGIN IMMEDIATE``, so two
        racing setups -- in this process or another on the same file --
        cannot both create a "first" admin."""
        return self._insert_admin(
            username, password, email=email, created_by=None, ip=ip,
            request_id=request_id, user_agent=user_agent, session_id=session_id,
            first=True,
        )

    def _insert_admin(
        self,
        username: str,
        password: str,
        *,
        email: str | None,
        created_by: str | None,
        ip: str | None,
        request_id: str | None,
        user_agent: str | None,
        session_id: str | None,
        first: bool,
    ) -> Admin:
        name = _valid_username(username)
        email = _valid_email(email)
        password_hash = self._ph.hash(_valid_password(password, username=name, email=email))
        now = self._clock()
        with self._db(write=True) as conn:
            if first and conn.execute("SELECT 1 FROM admins LIMIT 1").fetchone():
                raise AdminExists("an admin already exists")
            if conn.execute("SELECT 1 FROM admins WHERE username=?", (name,)).fetchone():
                raise ValueError(f"admin {name!r} already exists")
            if email is not None and conn.execute(
                "SELECT username FROM admins WHERE email=? COLLATE NOCASE", (email,)
            ).fetchone():
                raise ValueError(f"an admin with email {email!r} already exists")
            conn.execute(
                "INSERT INTO admins(username, email, password_hash, created_at, created_by)"
                " VALUES(?, ?, ?, ?, ?)",
                (name, email, password_hash, now, created_by),
            )
            action = AuditAction.SETUP_ADMIN_CREATED if created_by is None else AuditAction.ADMIN_ADDED
            self._audit(
                conn,
                created_by or name,
                action,
                _ADMIN,
                name,
                after={"username": name, "email": email, "created_at": now, "created_by": created_by},
                ip=ip,
                ts=now,
                request_id=request_id,
                user_agent=user_agent,
                session_id=session_id,
            )
        return Admin(username=name, email=email, created_at=now, created_by=created_by)

    def list_admins(self) -> list[Admin]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT username, email, created_at, created_by FROM admins"
                " ORDER BY created_at, username"
            ).fetchall()
        return [_admin(r) for r in rows]

    def remove_admin(
        self,
        username: str,
        *,
        actor: str,
        ip: str | None,
        request_id: str | None = None,
        user_agent: str | None = None,
        session_id: str | None = None,
    ) -> None:
        with self._db(write=True) as conn:
            row = conn.execute(
                "SELECT username, email, created_at, created_by FROM admins WHERE username=?",
                (username,),
            ).fetchone()
            if row is None:
                raise ValueError(f"no admin named {username!r}")
            if conn.execute("SELECT COUNT(*) FROM admins").fetchone()[0] <= 1:
                raise ValueError("cannot remove the last admin")
            conn.execute("DELETE FROM admins WHERE username=?", (username,))
            conn.execute("DELETE FROM sessions WHERE username=?", (username,))
            self._audit(
                conn,
                actor,
                AuditAction.ADMIN_REMOVED,
                _ADMIN,
                username,
                before={
                    "username": row["username"],
                    "email": row["email"],
                    "created_at": row["created_at"],
                    "created_by": row["created_by"],
                },
                ip=ip,
                request_id=request_id,
                user_agent=user_agent,
                session_id=session_id,
            )

    def change_password(
        self,
        username: str,
        old_password: str,
        new_password: str,
        *,
        actor: str,
        ip: str | None,
        request_id: str | None = None,
        user_agent: str | None = None,
        session_id: str | None = None,
    ) -> None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT password_hash, email FROM admins WHERE username=?", (username,)
            ).fetchone()
        _valid_password(new_password, username=username, email=row["email"] if row else None)
        ok = self._verify(row["password_hash"] if row else None, old_password)
        if row is None or not ok:
            raise InvalidCredentials("the current password is wrong")
        new_hash = self._ph.hash(new_password)
        with self._db(write=True) as conn:
            cur = conn.execute(
                "UPDATE admins SET password_hash=? WHERE username=?", (new_hash, username)
            )
            if cur.rowcount == 0:
                raise InvalidCredentials("the current password is wrong")
            conn.execute("DELETE FROM sessions WHERE username=?", (username,))
            self._audit(
                conn, actor, AuditAction.PASSWORD_CHANGED, _ADMIN, username, ip=ip,
                request_id=request_id, user_agent=user_agent, session_id=session_id,
            )

    # -- login / sessions ---------------------------------------------------

    def _lock_state(
        self, conn: sqlite3.Connection, key: str, ip: str | None, now: float
    ) -> tuple[float, float, int]:
        """``(pair seconds left, username seconds left, failures)`` for *key* from *ip*.

        The pair hold counts this address's failures for the identifier and is
        a refusal. The username hold counts failures for it from every address
        *not* itself at the threshold -- those are already locked out by their
        own pair -- and is only ever a delay: anyone can type the admin's name,
        so refusing on it would let two addresses keep the real admin out.
        """
        rows = conn.execute(
            "SELECT ip, COUNT(*) AS n, MAX(ts) AS last FROM login_attempts"
            " WHERE ok=0 AND ts>? AND username=? GROUP BY ip",
            (now - LOGIN_LOCK_WINDOW_S, key),
        ).fetchall()
        pair, failures = 0.0, 0
        spread_n, spread_last = 0, 0.0
        for row in rows:
            n, last = row["n"], row["last"]
            if row["ip"] == ip:
                failures = n
            if n >= LOGIN_LOCK_THRESHOLD:
                if row["ip"] == ip:
                    hold = min(LOGIN_LOCK_MAX_S, LOGIN_LOCK_BASE_S * 2 ** (n - LOGIN_LOCK_THRESHOLD))
                    pair = max(pair, last + hold - now)
            else:
                spread_n += n
                spread_last = max(spread_last, last)
        username = 0.0
        if spread_n >= LOGIN_LOCK_THRESHOLD:
            hold = min(
                LOGIN_LOCK_USERNAME_MAX_S,
                LOGIN_LOCK_BASE_S * 2 ** (spread_n - LOGIN_LOCK_THRESHOLD),
            )
            username = spread_last + hold - now
        return max(0.0, pair), max(0.0, username), failures

    def login_delay_s(self, ip: str | None, username: str | None = None) -> float:
        """How long the gateway sleeps before answering a login from *ip*.

        ``min(2**(n - LOGIN_LOCK_THRESHOLD), LOGIN_DELAY_MAX_S)`` once that
        address has ``n >= LOGIN_LOCK_THRESHOLD`` failures in the window, over
        any identifiers, plus ``min(username hold, LOGIN_DELAY_MAX_S)`` when
        *username* is held by failures spread over addresses. A delay, never a
        refusal: behind NAT one address is many people, and anyone can type a
        username."""
        now = self._clock()
        delay = 0.0
        with self._db() as conn:
            if ip is not None:
                n = conn.execute(
                    "SELECT COUNT(*) FROM login_attempts WHERE ok=0 AND ts>? AND ip=?",
                    (now - LOGIN_LOCK_WINDOW_S, ip),
                ).fetchone()[0]
                if n >= LOGIN_LOCK_THRESHOLD:
                    delay += float(min(2 ** (n - LOGIN_LOCK_THRESHOLD), LOGIN_DELAY_MAX_S))
            name = (username or "").strip() if isinstance(username, str) else ""
            if name and len(name) <= EMAIL_MAX_LEN:
                _, held, _ = self._lock_state(conn, _lock_key(name), ip, now)
                delay += min(held, float(LOGIN_DELAY_MAX_S))
        return delay

    def _attempt(
        self,
        name: str,
        password: str,
        *,
        ip: str | None,
        request_id: str | None,
        user_agent: str | None,
        session_id: str | None = None,
    ) -> tuple[str, str | None, int]:
        """Check *password* for the identifier *name* under the lockout rules.

        Returns ``(canonical username, stored hash, attempt row id)``; the
        attempt row is already written as a failure, inside the same
        ``BEGIN IMMEDIATE`` that read the lock state, so two concurrent
        guesses cannot both pass an unlocked check. The caller flips it to
        ``ok=1`` on success. Raises ``LoginLocked`` / ``InvalidCredentials``.
        """
        if (
            not name
            or len(name) > EMAIL_MAX_LEN
            or not isinstance(password, str)
            or len(password) > LOGIN_PASSWORD_MAX_LEN
        ):
            # Same argon2 work as any other refusal, and nothing recorded: an
            # unbounded identifier would otherwise land in audit.actor verbatim.
            self._verify(None, password[:LOGIN_PASSWORD_MAX_LEN] if isinstance(password, str) else "")
            raise InvalidCredentials()
        key = _lock_key(name)
        context = {"request_id": request_id, "user_agent": user_agent, "session_id": session_id}
        now = self._clock()
        with self._db(write=True) as conn:
            conn.execute(
                "DELETE FROM login_attempts WHERE ts<=?", (now - LOGIN_LOCK_WINDOW_S,)
            )
            conn.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
            row = conn.execute(
                "SELECT username, password_hash FROM admins"
                " WHERE username=? OR (email IS NOT NULL AND email=? COLLATE NOCASE)"
                " ORDER BY (username=?) DESC LIMIT 1",
                (name, name, name),
            ).fetchone()
            canonical = row["username"] if row else name
            remaining, _, _ = self._lock_state(conn, key, ip, now)
            attempt_id = 0
            if remaining <= 0:
                attempt_id = conn.execute(
                    "INSERT INTO login_attempts(ts, username, ip, ok) VALUES(?, ?, ?, 0)",
                    (now, key, ip),
                ).lastrowid
        if remaining > 0:
            security_log.warning(
                "login_locked actor=%s ip=%s rid=%s scope=pair retry_after_s=%.0f",
                printable(canonical), ip, request_id, remaining,
            )
            raise LoginLocked(remaining)

        stored = row["password_hash"] if row else None
        if self._verify(stored, password) and row is not None:
            return canonical, stored, attempt_id
        with self._db(write=True) as conn:
            self._audit(
                conn, canonical, AuditAction.LOGIN_FAILED, _ADMIN, canonical,
                ip=ip, ts=now, **context,
            )
            hold, _, failures = self._lock_state(conn, key, ip, now)
            if hold > 0:
                # The attempt recorded above is what started the lock: before
                # it, the check said unlocked. One row per onset.
                self._audit(
                    conn, canonical, AuditAction.LOGIN_LOCKED, _ADMIN, canonical,
                    after={"hold_s": round(hold, 3), "scope": "pair", "failures": failures},
                    ip=ip, ts=now, **context,
                )
        if hold > 0:
            security_log.warning(
                "login_locked actor=%s ip=%s rid=%s scope=pair hold_s=%.0f (lock begins)",
                printable(canonical), ip, request_id, hold,
            )
        raise InvalidCredentials()

    def reauthenticate(
        self,
        username: str,
        password: str,
        *,
        ip: str | None,
        request_id: str | None = None,
        user_agent: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Confirm the signed-in *username*'s password before an admin-level change.

        The same verify, lockout and attempt record as ``login``, and no
        session: a stolen cookie alone cannot mint an admin or rotate the
        admin key. Failure writes ``login_failed`` (actor = *username*) and
        raises ``InvalidCredentials``; a held pair raises ``LoginLocked``.
        """
        user_agent = user_agent[:512] if user_agent else None
        name = username if isinstance(username, str) else ""
        _, _, attempt_id = self._attempt(
            name, password, ip=ip, request_id=request_id, user_agent=user_agent,
            session_id=session_id,
        )
        with self._db(write=True) as conn:
            conn.execute("UPDATE login_attempts SET ok=1 WHERE rowid=?", (attempt_id,))

    def login(
        self,
        username: str,
        password: str,
        *,
        ip: str | None,
        request_id: str | None = None,
        user_agent: str | None = None,
        session_id: str | None = None,
    ) -> tuple[Session, str]:
        """``username`` is the username or the email; the session is the username's.

        An exact username wins over an email match, because a username may
        contain ``@`` and so may equal somebody else's email. Attempts are
        counted against the identifier as typed, so locking one spelling
        says nothing about which admin it is. ``session_id`` is ignored: the
        rows this writes carry the new session's.
        """
        name = (username or "").strip() if isinstance(username, str) else ""
        user_agent = user_agent[:512] if user_agent else None
        canonical, stored, attempt_id = self._attempt(
            name, password, ip=ip, request_id=request_id, user_agent=user_agent
        )
        context = {"request_id": request_id, "user_agent": user_agent}
        now = self._clock()
        rehash = self._ph.hash(password) if self._ph.check_needs_rehash(stored) else None
        cookie = secrets.token_urlsafe(32)
        sess = Session(
            session_id=hash_cookie(cookie),
            username=canonical,
            created_at=now,
            last_seen_at=now,
            expires_at=now + SESSION_MAX_S,
            ip=ip,
            user_agent=user_agent,
            login_ip=ip,
        )
        with self._db(write=True) as conn:
            if rehash is not None:
                conn.execute(
                    "UPDATE admins SET password_hash=? WHERE username=?", (rehash, canonical)
                )
            conn.execute("UPDATE login_attempts SET ok=1 WHERE rowid=?", (attempt_id,))
            conn.execute(
                "INSERT INTO sessions(sid_hash, username, created_at, last_seen_at, expires_at,"
                " ip, user_agent, login_ip) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (sess.session_id, canonical, now, now, sess.expires_at, ip, user_agent, ip),
            )
            self._audit(
                conn, canonical, AuditAction.LOGIN, _ADMIN, canonical, ip=ip, ts=now,
                session_id=sess.session_id[:12], **context,
            )
        return sess, cookie

    def session(self, cookie_value: str, *, now: float | None = None) -> Session | None:
        if not cookie_value:
            return None
        now = self._clock() if now is None else now
        sid = hash_cookie(cookie_value)
        with self._db(write=True) as conn:
            row = conn.execute("SELECT * FROM sessions WHERE sid_hash=?", (sid,)).fetchone()
            if row is None:
                return None
            if now >= row["expires_at"] or now - row["last_seen_at"] > SESSION_IDLE_S:
                conn.execute("DELETE FROM sessions WHERE sid_hash=?", (sid,))
                return None
            if now - row["last_seen_at"] >= _TOUCH_INTERVAL_S:
                conn.execute("UPDATE sessions SET last_seen_at=? WHERE sid_hash=?", (now, sid))
                row = conn.execute("SELECT * FROM sessions WHERE sid_hash=?", (sid,)).fetchone()
        return _session(row)

    def touch_session(
        self, sid_hash: str, *, ip: str | None, user_agent: str | None
    ) -> None:
        """Record where a live session was last seen from. ``login_ip`` is not
        touched: it is where the session signed in, for the life of the row."""
        with self._db(write=True) as conn:
            conn.execute(
                "UPDATE sessions SET ip=?, user_agent=? WHERE sid_hash=?",
                (ip, user_agent, sid_hash),
            )

    def list_sessions(self, *, now: float | None = None) -> list[Session]:
        """Every live session of every admin, newest first. Expired rows are
        left for ``session()``/``login`` to prune rather than written here."""
        now = self._clock() if now is None else now
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE expires_at>? AND last_seen_at>=?"
                " ORDER BY created_at DESC",
                (now, now - SESSION_IDLE_S),
            ).fetchall()
        return [_session(r) for r in rows]

    def logout(
        self,
        cookie_value: str,
        *,
        ip: str | None,
        request_id: str | None = None,
        user_agent: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """End the cookie's session. Audited only when a live row existed, so
        an open logout route cannot be used to write rows."""
        if not cookie_value:
            return
        sid = hash_cookie(cookie_value)
        with self._db(write=True) as conn:
            row = conn.execute("SELECT username FROM sessions WHERE sid_hash=?", (sid,)).fetchone()
            if row is None:
                return
            conn.execute("DELETE FROM sessions WHERE sid_hash=?", (sid,))
            self._audit(
                conn, row["username"], AuditAction.LOGOUT, _SESSION, row["username"], ip=ip,
                request_id=request_id, user_agent=user_agent, session_id=sid[:12],
            )

    def revoke_all(
        self,
        username: str,
        *,
        actor: str,
        ip: str | None,
        request_id: str | None = None,
        user_agent: str | None = None,
        session_id: str | None = None,
    ) -> int:
        """End every session of *username*, the caller's included. Returns
        how many there were; audited as ``sessions_revoked`` with the count."""
        with self._db(write=True) as conn:
            revoked = [
                r["sid_hash"][:12]
                for r in conn.execute(
                    "SELECT sid_hash FROM sessions WHERE username=?", (username,)
                ).fetchall()
            ]
            conn.execute("DELETE FROM sessions WHERE username=?", (username,))
            self._audit(
                conn, actor, AuditAction.SESSIONS_REVOKED, _SESSION, username,
                after={"count": len(revoked), "sessions": revoked},
                ip=ip, request_id=request_id, user_agent=user_agent, session_id=session_id,
            )
        return len(revoked)

    # -- audit --------------------------------------------------------------

    def audit(
        self,
        actor: str,
        action: AuditAction,
        target_kind: str,
        target_id: str,
        *,
        before: dict | None = None,
        after: dict | None = None,
        ip: str | None = None,
        request_id: str | None = None,
        user_agent: str | None = None,
        session_id: str | None = None,
        status: int | None = None,
    ) -> None:
        with self._db(write=True) as conn:
            self._audit(
                conn, actor, action, target_kind, target_id,
                before=before, after=after, ip=ip, request_id=request_id,
                user_agent=user_agent, session_id=session_id, status=status,
            )

    def verify_chain(self) -> ChainReport:
        """Recompute every hash-chained row, oldest first, then check the
        newest against the mirror's ``HEAD`` anchor when there is a mirror."""
        directory = getattr(self._mirror, "directory", None)
        head = auditlog.read_head(directory) if directory is not None else None
        with self._db() as conn:
            rows = conn.execute("SELECT * FROM audit ORDER BY id").fetchall()
            return store.anchor_chain(store.walk_chain(rows), conn, head)

    def head_hash(self) -> tuple[str | None, int]:
        """``(row_hash of the newest row, number of audit rows)`` -- what the
        sink's heartbeat anchors off the box."""
        with self._db() as conn:
            last = conn.execute("SELECT row_hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
            rows = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        return (last["row_hash"] if last else None), rows

    def audit_page(
        self,
        *,
        start: float | None = None,
        end: float | None = None,
        actor: str = "",
        action: str = "",
        target: str = "",
        page: int = 1,
        page_size: int = 100,
    ) -> tuple[list[AuditEntry], int]:
        where: list[str] = []
        args: list[object] = []
        if start is not None:
            where.append("ts>=?")
            args.append(start)
        if end is not None:
            where.append("ts<?")
            args.append(end)
        if actor:
            where.append("actor=?")
            args.append(actor)
        if action:
            where.append("action=?")
            args.append(action.value if isinstance(action, AuditAction) else action)
        if target:
            # instr(), not LIKE: a target id may contain % or _.
            where.append("(instr(target_id, ?) > 0 OR instr(target_kind, ?) > 0)")
            args.extend((target, target))
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        page = max(1, int(page))
        page_size = max(1, int(page_size))
        with self._db() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM audit{clause}", args).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM audit{clause} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                (*args, page_size, (page - 1) * page_size),
            ).fetchall()
        return [
            AuditEntry(
                id=r["id"],
                ts=r["ts"],
                actor=r["actor"],
                action=AuditAction(r["action"]),
                target_kind=r["target_kind"],
                target_id=r["target_id"],
                before=store.load(r["before_json"]),
                after=store.load(r["after_json"]),
                ip=r["ip"],
                request_id=r["request_id"],
                user_agent=r["user_agent"],
                session_id=r["session_id"],
                status=r["status"],
            )
            for r in rows
        ], total
