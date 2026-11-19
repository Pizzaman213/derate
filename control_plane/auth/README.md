# auth

Who may use the `/api` surface, and the record of what they did with it:
admin accounts, their browser sessions, the login lockout, and the audit log.
`AuthService` implements `contracts/auth.py::AuthPort` over one SQLite file,
`auth.db` under `paths.data_dir()`. API keys and orgs are `control_plane/keys`;
limits and pricing are `control_plane/limits`. Every admin action anywhere in
the product lands in this package's `audit` table through `AuthService.audit()`.

One rule governs the package: **it fails closed.** A store that cannot be
opened -- a directory, an uncreatable parent, a file that is not SQLite, a file
written by another schema version -- raises `AuthUnavailable`, and so does any
SQLite error afterwards; the gateway answers 503 `auth_unavailable`. A schema
mismatch is refused rather than reset, unlike `inventory/db.py`, because an
admin's password hash and the audit log exist nowhere else.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `service.py` | 799 | `AuthService`: admins, argon2id hashing, password policy, login + lockout + delay, sessions, revocation, audit append (hash-chained, mirrored, sunk), chain verification and paging |
| `store.py` | 233 | `auth.db`: schema v4 and its in-place migration, the `inventory/db.py` connect pattern, the fail-closed open, JSON helpers, the chain hash and `walk_chain` |
| `verify.py` | 64 | `python3 -m control_plane.auth.verify [--db PATH]`: the chain, read-only; exit 1 on a break |
| `__init__.py` | 5 | the export surface: `AuthService`, `hash_cookie` |

## How the gateway uses this

`gateway/authz.py::AuthStores` opens it on first use as `ctx.auth_stores.auth`
(`create_app` never touches the disk). `authz.AuthGuard` resolves the
`derate_session` cookie through `session()` for every `/api` route outside the
open list and answers 401 `session_required` without one;
`authz.require_session` does the same for the shell websocket. The routes in
`gateway/auth_api.py` (`/api/auth/*`, `/api/admins`) and `POST /api/setup/admin`
in `gateway/setup_api.py` are the only callers of the admin and login methods;
every explicit audit site in the gateway goes through `authz.audit_request`,
which attributes the row to the session and adds the request id, user agent
and session id; `GET /api/audit` reads `audit_page()` and
`GET /api/audit/integrity` reads `verify_chain()`. `AuthStores` constructs the
service with an `auditlog.AuditMirror` under `<logs dir>/audit` and, when
`DERATE_AUDIT_SINK` is set, an `auditlog.AuditSink` whose heartbeat reads
`head_hash()`. The mirror also keeps `<logs dir>/audit/HEAD` (the newest row's
id, hash and the row count), which `verify_chain()` checks so deleting the
newest rows is BROKEN rather than a shorter valid chain. The gateway's
lifespan opens the store at boot (so the sink runs from then) and
`gateway/chainwatch.py` verifies the chain then and every 60 s.
`docs/FORENSICS.md` is how an investigator uses all of it.

## Tables

| Table | Holds |
|---|---|
| `admins` | `username` (PK), argon2id `password_hash`, `created_at`, `created_by` (`NULL` = the setup admin) |
| `sessions` | `sid_hash` = sha256 of the cookie value (the cookie itself is never stored), `last_seen_at`, `expires_at`, `login_ip` (where it signed in; v4, copied from `ip` for older rows), `ip` and `user_agent` (where it was last seen: `authz.current_session` moves them through `touch_session()` and writes `session_ip_changed` when they change) |
| `login_attempts` | one row per verified attempt, keyed on the identifier as typed (case-folded), `ok` 0/1; rows older than the lock window are pruned on each login |
| `audit` | append-only; no code path deletes from it. v3 adds `request_id`, `user_agent`, `session_id` (12 hex of the session hash), `status`, `prev_hash`, `row_hash` |
| `meta` | `schema_version` |

## Rules

**Login always verifies once.** An unknown username is verified against a
dummy argon2id hash made at open, so timing does not say which usernames exist.
An empty or oversized identifier (over `EMAIL_MAX_LEN`) or password (over
`LOGIN_PASSWORD_MAX_LEN`) verifies the dummy once and records nothing. The only
login that verifies nothing is a locked one -- verifying while locked would
make the lock an oracle -- and it records no attempt, so hammering a lock does
not extend it; each such attempt is a `gateway.security` warning.

**Lockout is keyed on the identifier as typed, per address.** Typed, not the
admin it resolves to, so a 429 never links an email to its username.
`LOGIN_LOCK_THRESHOLD` failures inside `LOGIN_LOCK_WINDOW_S` from one
(identifier, address) pair lock that pair until
`last_failure + min(LOGIN_LOCK_MAX_S, LOGIN_LOCK_BASE_S * 2**(n - threshold))`;
`LOGIN_LOCK_MAX_S` is 900, the window, because failures age out of it and no
hold can outlast them. Failures for the identifier from addresses that are not
themselves locked add up, but they only ever *delay*: past the threshold they
hold the identifier for at most `LOGIN_LOCK_USERNAME_MAX_S` (300 s), and
`login_delay_s(ip, username)` adds `min(that hold, LOGIN_DELAY_MAX_S)` to the
sleep, after which the password is verified normally. It used to refuse, and
two addresses failing four times each could then renew a hold on the real
admin forever. Failures from one address across identifiers never refuse
either: `login_delay_s` adds `min(2**(n - threshold), LOGIN_DELAY_MAX_S)` and
`auth_api.login` sleeps it, since behind NAT one address is many people. Only
the pair lock answers 429. Its onset writes one `login_locked` row. A success
clears nothing; failures age out. The attempt is written as a failure in the
same `BEGIN IMMEDIATE` that reads the lock state and flipped to `ok=1` on
success, so concurrent guesses cannot each slip past an unlocked check.

**Re-authentication.** Adding or removing an admin and rotating an admin key
take the caller's `current_password` in the body; `AuthService.reauthenticate`
runs the same verify, lockout and attempt path as a login and mints nothing. A
missing or wrong one is a 401 `invalid_credentials` and a `login_failed` row
under the caller's name -- so a stolen cookie alone cannot mint a second admin
that survives the victim's password change.

**Passwords** are at least `PASSWORD_MIN_LEN` and may not contain the username
or the email's local part (either at least three characters), nor be one of a
short embedded list of the commonest passwords that meet the length rule.

**The audit chain.** Each v3 row's `row_hash` is
`sha256(prev_hash + "\n" + json([store.CHAIN_COLUMNS values]))`, computed inside
the write transaction against the newest row's hash, so the chain is serialised
by the store's lock. Rows from before v3 keep `NULL` and the chain starts at
the first v3 row. After the transaction commits each row is appended to the
JSONL mirror and queued for the sink; a failure there is logged, never raised.

**Sessions** expire `SESSION_MAX_S` after login regardless of activity, or
after `SESSION_IDLE_S` without one. `last_seen_at` is written at most once a
minute, so idle expiry is accurate to that. An expired row is deleted when it
is looked up.

**Password change, admin removal and `revoke_all` end every session of that
admin,** including the one that made the change (`DELETE /api/auth/sessions` is
"sign out everywhere"). A successful login rehashes the stored hash when the
hasher's parameters have moved (`check_needs_rehash`).

**Audit payloads never carry a password or a hash.** The service itself emits
`setup_admin_created` (`created_by=None`), `admin_added`, `admin_removed`,
`password_changed`, `login`, `login_failed` (actor = the attempted username),
`login_locked`, `logout` and `sessions_revoked`; everything else is written by
the gateway through `audit()`.
`audit_page` filters by time range, exact actor, exact action, and a literal
substring of `target_id` or `target_kind`, newest first.
