# keys

Orgs and the `derate-` API keys every `/v1` request has to carry.
`KeyService` implements `contracts/auth.py::KeyPort` over one SQLite file,
`keys.db` under `paths.data_dir()`, and is opened on first use by
`gateway/authz.py::AuthStores` as `ctx.auth_stores.keys`.

The package is pure state. It writes no audit rows -- the gateway does, since
it holds the acting admin's session and the before/after it wants recorded --
and it logs nothing: the raw key exists only in the `MintedKey` that
`create_key` and `rotate_key` return, and is stored as `sha256(secret)`.

**How the gateway uses this.** `authz.authenticate_key` reads the bearer token
off every `/v1` request (and the realtime websocket) and calls `authenticate`;
the `AuthError` code becomes the 401 body, the key and its org become the
request's `Principal`. `gateway/keys_api.py` serves `/api/orgs*` and
`/api/keys*` over the rest of the port, calling `validate_against_org` with
`/v1/models`' served names before every create and patch; `app.py` runs
`sweep_expired` every `KEY_SWEEP_INTERVAL_S` (60 s).

## Layout

| File | Lines | What it owns |
|---|---|---|
| `service.py` | 640 | `KeyService`: orgs, keys, `authenticate`, the expiry sweep, the allowlist and org-ceiling rules |
| `store.py` | 185 | `keys.db`: connect (the `inventory/db.py` pattern), schema, `meta`, row → `Org`/`ApiKey` |
| `format.py` | 58 | the key format: `mint`, `parse`, `hash_secret`, `verify`, `KEY_PATTERN` -- standard library only |
| `__init__.py` | 14 | the export surface |

## The key

`derate-<key_id>_<secret>`: `key_id` is 10 chars of `[a-z0-9]` (`secrets.choice`)
and is what the row is found by; `secret` is `secrets.token_urlsafe(32)`, so it
may itself contain `_` -- `parse` splits once. `KEY_PATTERN` is the regex the
redactors scrub by value.

## `authenticate`

The order is the contract, because each step answers a different `/v1` code:

1. does not parse → `invalid_api_key`
2. unknown id, or the key is soft-deleted → `invalid_api_key`
3. secret does not match (`hmac.compare_digest`) → `invalid_api_key`
4. its org is soft-deleted → `invalid_api_key`
5. `status=disabled` → `api_key_disabled` (so disabled *and* expired says disabled)
6. `status=expired`, or `expires_at <= now` → `api_key_expired`, and the row flips to `expired`

Success records `last_used_at`/`last_used_ip`, at most once per 5 s per key
unless the ip changed. The row is read on every call, so an `expires_at`
edited from the `sqlite3` CLI takes effect on the next request.

## Rules

- **Expiry** is one of `KEY_EXPIRY_DAYS`, else `ValueError` naming the options.
  `None` falls back to the org's `default_key_expiry_days`, and is final only
  for an admin key (`is_admin_key=True`, `admin_username` set), which never
  expires and which `sweep_expired` skips. Expiry is not editable: rotate, or
  create a new key.
- **Limits** are named from `LIMIT_TYPES` or `model:<served name>:<suffix>`
  (the served name may contain `:`), with values `> 0`. A key created with
  `limits=None` takes the org's `default_key_limits`.
- **Rotate** replaces `secret_hash` only; the old secret fails on the next call.
- **Delete** is soft for keys and orgs. A deleted key stays readable through
  `get_key` (its stats still need it) and leaves `list_keys` unless
  `include_deleted`. An org with live keys, and the default org, cannot be
  deleted. Org names are unique among live orgs.
- **Default org**: created as `default` (by `system`) the first time the file
  is opened; its id is kept in `meta.default_org_id`.
- **`effective_allowlist`**: the org's list, or what `/v1/models` exposes when
  the org has none; a key's own list is filtered to that. An explicit org list
  is *not* narrowed to what is exposed right now.
- **`validate_against_org`**: every limit the org also sets must be ≤ the
  org's (`LimitExceedsOrg`, naming the field and both numbers), and an explicit
  key allowlist must be a subset of the org's effective list
  (`AllowlistExceedsOrg`, the extras sorted). `update_key`/`create_key` do not
  call it: the gateway does, with the exposed list in hand.

## Errors

`ValueError` (a sentence naming what to change) for a bad argument; `KeyError`
for an unknown key or org id; `AuthError` with one of the codes above from
`authenticate`; `AuthUnavailable` when the file cannot be opened or was
written by a newer schema -- never reset, since it holds every credential.
