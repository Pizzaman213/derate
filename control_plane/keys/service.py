"""``KeyService``: orgs and ``drt_`` API keys, implementing ``KeyPort``.

Pure state. Nothing here writes an audit row -- the gateway does, because it
holds the actor's session and the before/after it wants recorded. Nothing
here logs a secret either: the raw key exists only in the
:class:`MintedKey` returned by :meth:`KeyService.create_key` and
:meth:`KeyService.rotate_key`.

Errors: a malformed argument is ``ValueError`` with a sentence naming what
to change; an unknown key or org id is ``KeyError``; a credential refusal is
``AuthError`` with one of the four ``/v1`` codes; a store that cannot be
opened is ``AuthUnavailable``.
"""

from __future__ import annotations

import math
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterator

from control_plane.contracts.auth import (
    KEY_PREFIX,
    KEY_SECRET_BYTES,
    KEY_EXPIRY_DAYS,
    LIMIT_TYPES,
    MODEL_LIMIT_SUFFIXES,
    AllowlistExceedsOrg,
    ApiKey,
    AuthError,
    AuthErrorCode,
    AuthUnavailable,
    KeyStatus,
    LimitExceedsOrg,
    MintedKey,
    Org,
)
from control_plane.contracts.routing import RoutingPolicy

from control_plane.limits.types import MODEL_PLACEHOLDER

from . import format as keyformat
from . import store

DAY_S = 86400.0
#: ``authenticate`` rewrites ``last_used_*`` at most this often per key
#: (unless the ip changed), so a busy key is not one write per request.
LAST_USED_WRITE_S = 5.0
DEFAULT_ORG_NAME = "default"
_ORG_ID_LEN = 8
_FIXED_LIMIT_TYPES = frozenset(t for t in LIMIT_TYPES if not t.startswith("model:"))

_ORG_FIELDS = frozenset(
    {
        "name",
        "allowed_models",
        "limits",
        "default_key_limits",
        "default_key_expiry_days",
        "routing_policy",
    }
)
_KEY_FIELDS = frozenset(
    {
        "name",
        "notes",
        "limits",
        "allowed_models",
        "routing_policy",
        "cloud_fallback_local_model",
    }
)


# ---------------------------------------------------------------- validation


def validate_limit_name(name: str) -> None:
    """Accept a fixed name from ``LIMIT_TYPES`` or ``model:<id>:<suffix>``."""
    if not isinstance(name, str):
        raise ValueError(f"limit name {name!r} is not a string")
    if name in _FIXED_LIMIT_TYPES:
        return
    if name.startswith("model:"):
        model, sep, suffix = name.removeprefix("model:").rpartition(":")
        if sep and model and model != MODEL_PLACEHOLDER and suffix in MODEL_LIMIT_SUFFIXES:
            return
    raise ValueError(
        f"unknown limit {name!r}; use one of {', '.join(sorted(_FIXED_LIMIT_TYPES))} "
        f"or model:<served name>:<{'|'.join(MODEL_LIMIT_SUFFIXES)}>"
    )


def _limits(value: object, field_name: str) -> dict[str, float]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object of limit name to number")
    out: dict[str, float] = {}
    for name, v in value.items():
        validate_limit_name(name)
        if (
            isinstance(v, bool)
            or not isinstance(v, (int, float))
            or not math.isfinite(v)
            or not v > 0
        ):
            raise ValueError(
                f"{field_name}.{name} must be a finite number greater than 0, not {v!r}"
            )
        out[name] = float(v)
    return out


def _models(value: object, field_name: str) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_name} must be a list of served model names or null")
    out: list[str] = []
    for m in value:
        if not isinstance(m, str) or not m.strip():
            raise ValueError(f"{field_name} entries must be non-empty strings, not {m!r}")
        if m not in out:
            out.append(m)
    return out


def _policy(value: object) -> RoutingPolicy | None:
    if value is None or isinstance(value, RoutingPolicy):
        return value
    try:
        return RoutingPolicy(value)
    except ValueError:
        raise ValueError(
            f"routing_policy {value!r} is not one of "
            + ", ".join(p.value for p in RoutingPolicy)
        ) from None


def _name(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} name must be a non-empty string")
    return value.strip()


def _expiry_days(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or value not in KEY_EXPIRY_DAYS:
        raise ValueError(
            f"{field_name} must be one of {', '.join(map(str, KEY_EXPIRY_DAYS))} days, not {value!r}"
        )
    return int(value)  # type: ignore[arg-type]


def _optional_str(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string or null")
    return value.strip()


def _num(v: float) -> str:
    return f"{v:g}"


# ------------------------------------------------------------------- service


class KeyService:
    """Orgs and API keys over ``keys.db``. Every method takes the one lock."""

    def __init__(
        self, conn: sqlite3.Connection, lock, *, clock: Callable[[], float] = time.time
    ) -> None:
        self._conn = conn
        self._lock = lock
        self._clock = clock
        self._default_org_id = self._ensure_default_org()

    @classmethod
    def open(cls, path: Path | str, *, clock: Callable[[], float] = time.time) -> "KeyService":
        conn, lock = store.open_database(path)
        try:
            return cls(conn, lock, clock=clock)
        except sqlite3.Error as exc:
            conn.close()
            raise AuthUnavailable(f"the key store at {path} could not be opened: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- plumbing

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _now(self, now: float | None) -> float:
        return self._clock() if now is None else now

    def _org_row(self, conn: sqlite3.Connection, org_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM orgs WHERE org_id=?", (org_id,)).fetchone()
        if row is None:
            raise KeyError(org_id)
        return row

    def _key_row(self, conn: sqlite3.Connection, key_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM api_keys WHERE key_id=?", (key_id,)).fetchone()
        if row is None:
            raise KeyError(key_id)
        return row

    def _live_key_row(self, conn: sqlite3.Connection, key_id: str) -> sqlite3.Row:
        row = self._key_row(conn, key_id)
        if row["deleted_at"] is not None:
            raise ValueError(f"key {key_id} has been deleted")
        return row

    def _check_org_name_free(
        self, conn: sqlite3.Connection, name: str, *, except_id: str | None = None
    ) -> None:
        row = conn.execute(
            "SELECT org_id FROM orgs WHERE name=? AND deleted_at IS NULL AND org_id IS NOT ?",
            (name, except_id),
        ).fetchone()
        if row is not None:
            raise ValueError(f"an org named {name!r} already exists")

    def _ensure_default_org(self) -> str:
        with self._tx() as conn:
            stored = store.get_meta(conn, "default_org_id")
            if stored is not None:
                return stored
            row = conn.execute(
                "SELECT org_id FROM orgs WHERE name=? AND deleted_at IS NULL "
                "ORDER BY created_at, org_id LIMIT 1",
                (DEFAULT_ORG_NAME,),
            ).fetchone()
            org_id = row["org_id"] if row is not None else self._insert_org(
                conn, DEFAULT_ORG_NAME, actor="system", fields={}
            ).org_id
            store.set_meta(conn, "default_org_id", org_id)
            return org_id

    # -- orgs

    def _org_values(self, fields: dict) -> dict:
        unknown = set(fields) - _ORG_FIELDS
        if unknown:
            raise ValueError(
                f"unknown org field(s) {', '.join(sorted(unknown))}; "
                f"editable: {', '.join(sorted(_ORG_FIELDS))}"
            )
        values: dict = {}
        if "name" in fields:
            values["name"] = _name(fields["name"], "org")
        if "allowed_models" in fields:
            values["allowed_models_json"] = store.dump_list(
                _models(fields["allowed_models"], "allowed_models")
            )
        if "limits" in fields:
            values["limits_json"] = store.dump_limits(_limits(fields["limits"], "limits"))
        if "default_key_limits" in fields:
            values["default_key_limits_json"] = store.dump_limits(
                _limits(fields["default_key_limits"], "default_key_limits")
            )
        if "default_key_expiry_days" in fields:
            values["default_key_expiry_days"] = _expiry_days(
                fields["default_key_expiry_days"], "default_key_expiry_days"
            )
        if "routing_policy" in fields:
            values["routing_policy"] = store.dump_policy(_policy(fields["routing_policy"]))
        return values

    def _insert_org(self, conn: sqlite3.Connection, name: str, *, actor: str, fields: dict) -> Org:
        name = _name(name, "org")
        if "name" in fields:
            raise ValueError("pass the org name once, as the first argument")
        values = self._org_values(fields)
        self._check_org_name_free(conn, name)
        now = self._clock()
        while True:
            org_id = "org_" + "".join(
                secrets.choice(keyformat.KEY_ID_ALPHABET) for _ in range(_ORG_ID_LEN)
            )
            if conn.execute("SELECT 1 FROM orgs WHERE org_id=?", (org_id,)).fetchone() is None:
                break
        conn.execute(
            "INSERT INTO orgs(org_id, name, created_at, created_by, allowed_models_json,"
            " limits_json, default_key_limits_json, default_key_expiry_days, routing_policy)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                org_id,
                name,
                now,
                actor,
                values.get("allowed_models_json"),
                values.get("limits_json", "{}"),
                values.get("default_key_limits_json", "{}"),
                values.get("default_key_expiry_days"),
                values.get("routing_policy"),
            ),
        )
        return store.row_to_org(self._org_row(conn, org_id))

    def create_org(self, name: str, *, actor: str, **fields) -> Org:
        with self._tx() as conn:
            return self._insert_org(conn, name, actor=actor, fields=fields)

    def get_org(self, org_id: str) -> Org | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM orgs WHERE org_id=?", (org_id,)).fetchone()
        return None if row is None else store.row_to_org(row)

    def list_orgs(self, *, include_deleted: bool = False) -> list[Org]:
        sql = "SELECT * FROM orgs"
        if not include_deleted:
            sql += " WHERE deleted_at IS NULL"
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY created_at, org_id").fetchall()
        return [store.row_to_org(r) for r in rows]

    def update_org(self, org_id: str, *, actor: str, **fields) -> Org:
        values = self._org_values(fields)
        with self._tx() as conn:
            row = self._org_row(conn, org_id)
            if row["deleted_at"] is not None:
                raise ValueError(f"org {org_id} has been deleted")
            if "name" in values:
                self._check_org_name_free(conn, values["name"], except_id=org_id)
            if values:
                cols = ", ".join(f"{c}=?" for c in values)
                conn.execute(
                    f"UPDATE orgs SET {cols} WHERE org_id=?", (*values.values(), org_id)
                )
            return store.row_to_org(self._org_row(conn, org_id))

    def delete_org(self, org_id: str, *, actor: str) -> None:
        if org_id == self._default_org_id:
            raise ValueError("the default org cannot be deleted")
        with self._tx() as conn:
            row = self._org_row(conn, org_id)
            if row["deleted_at"] is not None:
                return
            (live,) = conn.execute(
                "SELECT COUNT(*) FROM api_keys WHERE org_id=? AND deleted_at IS NULL",
                (org_id,),
            ).fetchone()
            if live:
                raise ValueError(
                    f"org {row['name']!r} still has {live} key(s); delete or move them first"
                )
            conn.execute(
                "UPDATE orgs SET deleted_at=? WHERE org_id=?", (self._clock(), org_id)
            )

    def default_org_id(self) -> str:
        return self._default_org_id

    # -- keys

    def create_key(
        self,
        *,
        name: str,
        org_id: str,
        created_by: str,
        expiry_days: int | None,
        limits: dict[str, float] | None = None,
        allowed_models: list[str] | None = None,
        routing_policy: RoutingPolicy | None = None,
        notes: str = "",
        is_admin_key: bool = False,
        admin_username: str | None = None,
        cloud_fallback_local_model: str | None = None,
    ) -> MintedKey:
        name = _name(name, "key")
        models = _models(allowed_models, "allowed_models")
        policy = _policy(routing_policy)
        fallback = _optional_str(cloud_fallback_local_model, "cloud_fallback_local_model")
        if not isinstance(notes, str):
            raise ValueError("notes must be a string")
        if is_admin_key:
            if expiry_days is not None:
                raise ValueError("an admin key never expires; pass expiry_days=None")
            if not admin_username:
                raise ValueError("an admin key needs the admin_username it belongs to")
        elif admin_username is not None:
            raise ValueError("admin_username is only set on an admin key")
        key_limits = None if limits is None else _limits(limits, "limits")

        with self._tx() as conn:
            org_row = self._org_row(conn, org_id)
            if org_row["deleted_at"] is not None:
                raise ValueError(f"org {org_id} has been deleted")
            org = store.row_to_org(org_row)
            if key_limits is None:
                key_limits = dict(org.default_key_limits)
            if not is_admin_key:
                if expiry_days is None:
                    expiry_days = org.default_key_expiry_days
                if expiry_days is None:
                    raise ValueError(
                        "a key needs an expiry: one of "
                        f"{', '.join(map(str, KEY_EXPIRY_DAYS))} days"
                    )
                expiry_days = _expiry_days(expiry_days, "expiry_days")
            now = self._clock()
            expires_at = None if expiry_days is None else now + expiry_days * DAY_S
            while True:
                key_id, secret, raw = keyformat.mint()
                if conn.execute(
                    "SELECT 1 FROM api_keys WHERE key_id=?", (key_id,)
                ).fetchone() is None:
                    break
            conn.execute(
                "INSERT INTO api_keys(key_id, secret_hash, name, org_id, created_by, created_at,"
                " expires_at, status, notes, is_admin_key, admin_username, allowed_models_json,"
                " routing_policy, limits_json, cloud_fallback_local_model)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key_id,
                    keyformat.hash_secret(secret),
                    name,
                    org_id,
                    created_by,
                    now,
                    expires_at,
                    KeyStatus.ACTIVE.value,
                    notes,
                    int(bool(is_admin_key)),
                    admin_username,
                    store.dump_list(models),
                    store.dump_policy(policy),
                    store.dump_limits(key_limits),
                    fallback,
                ),
            )
            key = store.row_to_key(self._key_row(conn, key_id))
        return MintedKey(key=key, secret_once=raw)

    def get_key(self, key_id: str) -> ApiKey | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM api_keys WHERE key_id=?", (key_id,)
            ).fetchone()
        return None if row is None else store.row_to_key(row)

    def list_keys(
        self, *, org_id: str | None = None, include_deleted: bool = False
    ) -> list[ApiKey]:
        clauses, args = [], []
        if org_id is not None:
            clauses.append("org_id=?")
            args.append(org_id)
        if not include_deleted:
            clauses.append("deleted_at IS NULL")
        sql = "SELECT * FROM api_keys"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY created_at, key_id", args).fetchall()
        return [store.row_to_key(r) for r in rows]

    def authenticate(
        self, raw_key: str, *, ip: str | None, now: float | None = None
    ) -> ApiKey:
        invalid = AuthError(AuthErrorCode.INVALID_API_KEY, "the API key is not valid")
        parsed = keyformat.parse(raw_key)
        if parsed is None:
            raise invalid
        key_id, secret = parsed
        now = self._now(now)
        with self._lock:
            row = self._conn.execute(
                "SELECT k.*, o.deleted_at AS org_deleted_at FROM api_keys k "
                "LEFT JOIN orgs o ON o.org_id = k.org_id WHERE k.key_id=?",
                (key_id,),
            ).fetchone()
            if row is None or row["deleted_at"] is not None:
                raise invalid
            if not keyformat.verify(secret, row["secret_hash"]):
                raise invalid
            if row["org_deleted_at"] is not None:
                raise invalid
            if row["status"] == KeyStatus.DISABLED.value:
                raise AuthError(
                    AuthErrorCode.API_KEY_DISABLED, f"API key {key_id} has been disabled"
                )
            if row["status"] == KeyStatus.EXPIRED.value or (
                row["expires_at"] is not None and row["expires_at"] <= now
            ):
                if row["status"] != KeyStatus.EXPIRED.value:
                    self._conn.execute(
                        "UPDATE api_keys SET status=? WHERE key_id=? AND status=?",
                        (KeyStatus.EXPIRED.value, key_id, KeyStatus.ACTIVE.value),
                    )
                raise AuthError(AuthErrorCode.API_KEY_EXPIRED, f"API key {key_id} has expired")
            key = store.row_to_key(row)
            last = row["last_used_at"]
            if last is None or now - last >= LAST_USED_WRITE_S or row["last_used_ip"] != ip:
                self._conn.execute(
                    "UPDATE api_keys SET last_used_at=?, last_used_ip=? WHERE key_id=?",
                    (now, ip, key_id),
                )
            return replace(key, last_used_at=now, last_used_ip=ip)

    def update_key(self, key_id: str, *, actor: str, **fields) -> ApiKey:
        unknown = set(fields) - _KEY_FIELDS
        if unknown:
            hint = " (expiry cannot change; rotate or create a new key)" if (
                unknown & {"expires_at", "expiry_days"}
            ) else ""
            raise ValueError(
                f"unknown key field(s) {', '.join(sorted(unknown))}{hint}; "
                f"editable: {', '.join(sorted(_KEY_FIELDS))}"
            )
        values: dict = {}
        if "name" in fields:
            values["name"] = _name(fields["name"], "key")
        if "notes" in fields:
            if not isinstance(fields["notes"], str):
                raise ValueError("notes must be a string")
            values["notes"] = fields["notes"]
        if "limits" in fields:
            values["limits_json"] = store.dump_limits(_limits(fields["limits"], "limits"))
        if "allowed_models" in fields:
            values["allowed_models_json"] = store.dump_list(
                _models(fields["allowed_models"], "allowed_models")
            )
        if "routing_policy" in fields:
            values["routing_policy"] = store.dump_policy(_policy(fields["routing_policy"]))
        if "cloud_fallback_local_model" in fields:
            values["cloud_fallback_local_model"] = _optional_str(
                fields["cloud_fallback_local_model"], "cloud_fallback_local_model"
            )
        with self._tx() as conn:
            self._live_key_row(conn, key_id)
            if values:
                cols = ", ".join(f"{c}=?" for c in values)
                conn.execute(
                    f"UPDATE api_keys SET {cols} WHERE key_id=?", (*values.values(), key_id)
                )
            return store.row_to_key(self._key_row(conn, key_id))

    def disable_key(self, key_id: str, *, actor: str, reason: str | None = None) -> ApiKey:
        with self._tx() as conn:
            self._live_key_row(conn, key_id)
            conn.execute(
                "UPDATE api_keys SET status=?, disabled_at=?, disabled_by=?, disabled_reason=?"
                " WHERE key_id=?",
                (KeyStatus.DISABLED.value, self._clock(), actor, reason, key_id),
            )
            return store.row_to_key(self._key_row(conn, key_id))

    def enable_key(self, key_id: str, *, actor: str) -> ApiKey:
        with self._tx() as conn:
            row = self._live_key_row(conn, key_id)
            if row["status"] == KeyStatus.EXPIRED.value or (
                row["expires_at"] is not None and row["expires_at"] <= self._clock()
            ):
                raise ValueError(f"key {key_id} has expired; create a new key instead")
            conn.execute(
                "UPDATE api_keys SET status=?, disabled_at=NULL, disabled_by=NULL,"
                " disabled_reason=NULL WHERE key_id=?",
                (KeyStatus.ACTIVE.value, key_id),
            )
            return store.row_to_key(self._key_row(conn, key_id))

    def rotate_key(self, key_id: str, *, actor: str) -> MintedKey:
        secret = secrets.token_urlsafe(KEY_SECRET_BYTES)
        with self._tx() as conn:
            self._live_key_row(conn, key_id)
            conn.execute(
                "UPDATE api_keys SET secret_hash=? WHERE key_id=?",
                (keyformat.hash_secret(secret), key_id),
            )
            key = store.row_to_key(self._key_row(conn, key_id))
        return MintedKey(key=key, secret_once=f"{KEY_PREFIX}{key_id}_{secret}")

    def delete_key(self, key_id: str, *, actor: str) -> None:
        with self._tx() as conn:
            row = self._key_row(conn, key_id)
            if row["deleted_at"] is None:
                conn.execute(
                    "UPDATE api_keys SET deleted_at=? WHERE key_id=?", (self._clock(), key_id)
                )

    def sweep_expired(self, *, now: float | None = None) -> list[str]:
        now = self._now(now)
        with self._tx() as conn:
            ids = [
                r["key_id"]
                for r in conn.execute(
                    "SELECT key_id FROM api_keys WHERE status=? AND expires_at IS NOT NULL"
                    " AND expires_at <= ? AND deleted_at IS NULL AND is_admin_key=0"
                    " ORDER BY key_id",
                    (KeyStatus.ACTIVE.value, now),
                )
            ]
            conn.executemany(
                "UPDATE api_keys SET status=? WHERE key_id=?",
                [(KeyStatus.EXPIRED.value, i) for i in ids],
            )
        return ids

    # -- policy

    def effective_allowlist(self, key: ApiKey, org: Org, exposed: list[str]) -> list[str]:
        org_list = list(org.allowed_models) if org.allowed_models is not None else list(exposed)
        if key.allowed_models is None:
            return org_list
        allowed = set(org_list)
        return [m for m in key.allowed_models if m in allowed]

    def validate_against_org(
        self,
        *,
        org: Org,
        limits: dict[str, float],
        allowed_models: list[str] | None,
        exposed: list[str],
    ) -> None:
        for name in sorted(limits or {}):
            if name in org.limits and float(limits[name]) > org.limits[name]:
                raise LimitExceedsOrg(
                    name,
                    f"{name} {_num(float(limits[name]))} exceeds the org's "
                    f"{name} {_num(org.limits[name])}",
                )
        if allowed_models is not None:
            org_list = set(org.allowed_models if org.allowed_models is not None else exposed)
            extras = sorted(set(allowed_models) - org_list)
            if extras:
                raise AllowlistExceedsOrg(extras)
