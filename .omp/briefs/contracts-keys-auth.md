# contracts, keys, auth, limits brief

Owns: the frozen shapes every package codes against and their generators; orgs and `drt_` API keys; admin accounts, sessions, lockout and the audit log; rate limits, quotas and local pricing.
Code: control_plane/contracts/, control_plane/keys/, control_plane/auth/, control_plane/limits/   Tests: tests/unit/test_keys.py, test_limits.py, test_contracts.py, test_contracts_manifest.py, test_single_source.py, test_gateway_auth.py, test_gateway_auth_coverage.py, test_gateway_auth_leak.py   Read first: `control_plane/contracts/README.md`, `control_plane/keys/README.md`, `control_plane/auth/README.md`, `control_plane/limits/README.md`, CLAUDE.md "Rules that are not style" (the credential-gate paragraphs)

An agent reads Depth 1 for every part it touches, and goes deeper only as far as the task needs.
Every `path::symbol` reference below is verified by the gate, so the brief cannot name code that
does not exist. Long form lives in the four READMEs above; this page only says where to act.

## Depth 1 -- 30 seconds
- `control_plane/contracts/` holds the frozen shapes (`auth.py` is the auth half: `ApiKey`, `Org`, `AuditAction`, `LIMIT_TYPES`, the ports). Changing one is announced and made in one place, never to unblock yourself. Then regenerate three files.
- Three SQLite files under `data_dir()`: `keys.db` (`KeyService`), `auth.db` (`AuthService`), `limits.db` (`LimitService` + `PricingStore`). All fail closed: a store that cannot open, or is from a newer schema, raises `AuthUnavailable` (gateway answers 503). Never reset one, they hold the only copy of credentials and counters.
- These packages are pure state. They write no audit rows for keys/orgs/limits and log no secret. The gateway audits, because it holds the session (`control_plane/gateway/authz.py::audit_request`).
- A raw key exists only in `MintedKey` (shown once). Stored form is `sha256(secret)`. Never add a path that renders, logs or lists one.
- Every `/api` route is gated by default in `control_plane/gateway/authz.py::AuthGuard`; opening one is an edit to a hardcoded list plus a test set.

## Depth 2 -- the rules
- **One home per fact; copies are tested.** `derived.py` names each derived fact, its canonical `module:attr`, and every copy. Why: the drift was never in the definitions, it was in copies (TS unions, a second formatter, the key regex). Enforced by `tests/unit/test_single_source.py::test_every_copy_still_agrees_with_its_canonical_definition`. Add a row to `control_plane/contracts/derived.py::FACTS`, never an assertion in the test.
- **Contracts are announced, and a field's default is a compatibility promise.** `contracts/` has 51 importers; a renamed or required-in-the-middle field breaks every stored record at once. New fields are trailing and defaulted. Tripwire: `tests/unit/test_contracts.py`; do not relax an assertion to make it pass.
- **The generated files must be current.** `manifest.json`, `routes.json` and `docs/CONTRACTS.md` are reflected from live objects, not written. `tests/unit/test_contracts_manifest.py::test_the_contract_manifest_matches_the_contracts` fails with the exact command to run. Never hand-edit them.
- **Reflection skips the describing modules.** `control_plane/contracts/manifest.py::_NOT_CONTRACTS` keeps `manifest`, `derived`, `routes`, `document` out, because a generator's own constants once leaked a developer's absolute path into the manifest. Annotations are kept as written, so `int` vs `int | None` shows in the diff.
- **The TypeScript mirror is only checked by one verifier.** UI unions copy Python enums; `ui/src/api/contracts.check.mjs` diffs them and is skipped without python (a skip is not a pass; use `npm run check -- --strict`).
- **Key format is `drt_<key_id>_<secret>`.** 10-char `[a-z0-9]` id finds the row; the secret may contain `_`, so `parse` splits once. `control_plane/keys/format.py::KEY_PATTERN` is canonical; `control_plane/redaction.py` copies it on purpose (every node imports the scrubber and it may not import `keys/`), and `redact.ts` is a third copy.
- **`authenticate` has a fixed order, each step is a different `/v1` code.** Unparseable / unknown / deleted / wrong secret / deleted org -> `invalid_api_key`; disabled -> `api_key_disabled`; expired -> `api_key_expired` (and the row flips). Why: clients branch on the code. Secret compare is `hmac.compare_digest`. Enforced in `control_plane/keys/service.py::KeyService.authenticate`, pinned by `tests/unit/test_gateway_auth_coverage.py::test_every_v1_route_refuses_each_bad_key_with_its_own_code`.
- **Expiry is from `KEY_EXPIRY_DAYS` only; `None` is for an admin key.** Not editable after create: rotate or recreate. Why: an expiry that can be edited is not an expiry. Enforced in `KeyService.create_key`.
- **A key's limits and allowlist may not exceed its org's.** `KeyService.validate_against_org` raises `LimitExceedsOrg` / `AllowlistExceedsOrg` naming the field. The service does not call it from `create_key`/`update_key`; the gateway does, with the served-model list in hand. A new write path must call it too.
- **Delete is soft for keys and orgs.** A deleted key stays readable by `get_key` (stats need it). An org with live keys, and the default org, cannot be deleted.
- **Login always verifies once; a locked login verifies nothing.** An unknown user is verified against a dummy hash (no timing oracle); verifying while locked would make the lock an oracle. Enforced in `control_plane/auth/service.py::AuthService.login` and `AuthService._attempt`.
- **Lockout refuses only per (identifier, address) pair; everything else only delays.** Why: a bare-username lock let two addresses renew a hold on the real admin forever (denial of service). `LOGIN_LOCK_MAX_S` equals the window, failures age out. Tested by `tests/unit/test_single_source.py::test_login_lock_max_is_the_window_it_can_actually_reach`; the username cap is `control_plane/contracts/derived.py::FACTS` row `login_lock_username_cap`.
- **Admin-level changes re-ask for the password.** Add/remove admin and rotating an admin key take `current_password`; `AuthService.reauthenticate` runs the login verify, lockout and attempt path. Why: a stolen cookie alone must not mint a second admin.
- **The audit log is append-only and hash-chained.** `row_hash = sha256(prev_hash + "\n" + json(CHAIN_COLUMNS))`, computed inside the write transaction; no code deletes from `audit`. The mirror's `HEAD` file makes deleting the newest rows BROKEN, not a shorter valid chain. Payloads never carry a password or hash. See `control_plane/auth/store.py::chain_hash`, `control_plane/auth/store.py::walk_chain`.
- **Sessions: absolute and idle expiry, and a password change ends all of them** (`SESSION_MAX_S`, `SESSION_IDLE_S` in `contracts/auth.py`). Only the sha256 of the cookie is stored.
- **A limit refusal names `<scope>:<limit_type>`; check order is the contract.** Allowlist -> per-request caps -> concurrency/rpm/tpm (key, org, route) -> cycle limits. Messages render verbatim. Enforced in `control_plane/limits/service.py::LimitService.check_and_reserve`.
- **Every admitted request holds a reservation that must be settled in a `finally`.** A leaked one holds a `max_concurrent` slot until restart. `None` means "never ran" only until `mark_started`; a started request keeps its estimate. `settle` never raises: the response is already served, and the next check fails closed.
- **Unpriced is not zero.** A request without a complete price row increments `unpriced_requests`, never a dollar counter. The one source of a dollar figure and its `CostBasis` is `control_plane/limits/pricing.py::PricingStore.cost`.
- **Cycles are 30 days anchored at `created_at`;** `raise_for_cycle` overrides for this cycle only, `reset_cycle` deletes this cycle's counters. Neither audits; the gateway does.

## Depth 3 -- how to change it
**Add a limit type**
1. Add the name to `control_plane/contracts/auth.py::LIMIT_TYPES` (and `MODEL_LIMIT_SUFFIXES` if per-model).
2. Give it a kind in `control_plane/limits/types.py::KIND`; a cycle limit also needs `control_plane/limits/types.py::CYCLE_METRIC` and a counter written in `LimitService.settle`; dollar bases go in `USD_BASES`.
3. Add the check in `LimitService.check_and_reserve` at the right point in the order, with a message that names limit, used and when it frees.
4. Mirror it in `ui/src/tabs/keys/rows.ts` (`FIXED_LIMIT_TYPES`).
5. Tests: a scenario in `tests/unit/test_limits.py` (the set-driven test fails until there is one). Regenerate: the `derived_by` command in `.omp/work/auth-keys/sets/limit_types.json`, then `python3 -m control_plane.contracts.manifest --write` and `... document --write`.

**Add a field to `ApiKey` or `Org`** (contract change, announce it)
1. Add it trailing and defaulted in `control_plane/contracts/auth.py::ApiKey`.
2. Add the column in `control_plane/keys/store.py` (new files) AND an in-place `ALTER TABLE` for existing files, copying the `_ADDED_COLUMNS` pattern in `control_plane/auth/store.py::open_database`; bump `control_plane/keys/store.py::SCHEMA_VERSION`. Never reset an old file.
3. Read it in `control_plane/keys/store.py::row_to_key`; accept it in the field set in `KeyService.update_key`/`create_key`, validating with the `_limits` / `_models` helpers.
4. Update the gateway dict (`control_plane/gateway/authz.py::key_dict`) and `ui/src/api/types.ts`.
5. Tests: `tests/unit/test_keys.py` (roundtrip, a reopen of the old file), `tests/unit/test_gateway_auth.py`. Regenerate the manifest and document.

**Add an audit action**
1. Add a member after its neighbours in `control_plane/contracts/auth.py::AuditAction`.
2. Write the row from the gateway via `authz.audit_request`, with `before`/`after` and no secret.
3. Add it to the UI union in `ui/src/api/types.ts` and to `ui/src/tabs/audit/diff.ts::ACTION_ORDER` (a `Record<AuditAction, number>`, so `tsc` fails until it is there).
4. Regenerate manifest and document; test the row in `tests/unit/test_gateway_auth.py`.

**Add a derived fact or a copy of one**
1. Add a `DerivedFact` to `control_plane/contracts/derived.py::FACTS` with `canonical`, `why`, `copies` (resolvable) and `restated_at` (cannot be compared).
2. Prefer converting a restating site into an import. `tests/unit/test_single_source.py` picks the row up with no edit. Regenerate the manifest (it reflects `FACTS`).

**Add or change an `/api` route that touches keys/auth**
1. Handler in `control_plane/gateway/keys_api.py` (or `auth_api.py`); register above the `StaticFiles` mount. It is session-gated by default. Do NOT touch `OPEN_ROUTES` unless it must work with no session.
2. Audit it; ask `current_password` only for admin-level changes.
3. Add the route to `.omp/work/auth-keys/sets/api_routes.json` via its `derived_by` command, after `python3 -m control_plane.contracts.routes --write`, then `... document --write`.
4. Test: the coverage walk in `tests/unit/test_gateway_auth_coverage.py` fails on a route that answers without credentials; add a behaviour test in `tests/unit/test_gateway_auth.py`.

**Change what a `KeyService` row stores**
1. Edit `control_plane/keys/store.py` DDL and `SCHEMA_VERSION`, with an in-place migration (never `DROP`).
2. Keep `control_plane/contracts/auth.py::KeyPort` and `KeyService` signatures in step (`tests/unit/test_keys.py::test_implements_keyport` is a runtime check). Any fake that implements the port must gain the same methods.

## Pitfalls
- **A contract changed, nothing under `control_plane/` broke, the UI did.** The TS union mirror is only checked by `contracts.check.mjs`; run `cd ui && npm run check -- --strict`.
- **`CLAUDE.md` says `derate-` in places, the code is `drt_`.** Trust `control_plane/contracts/auth.py::KEY_PREFIX`; `KEY_PATTERN` is what the redactors scrub by.
- **Hand-editing `manifest.json` or `routes.json`.** It passes locally and fails the freshness test on the next regeneration. Edit the thing reflected.
- **`docs/CONTRACTS.md` is a third generated file.** Forgetting `--write` on `contracts.document` is the usual stale-test failure after a manifest or routes change.
- **A limit refused or counted at the wrong time.** Count limits block at `used >= limit` including live reservations; token and dollar limits let the crossing request finish. Do not "fix" that without reading the limits README.
- **Forgetting `settle`.** A path that returns early before `settle` leaks a concurrency slot until restart.
- **Estimating the prompt differently.** `estimate_prompt_tokens` copies `gateway/admission.py`'s formula and adds a per-image estimate; a second estimator means caps and admission disagree.
- **Verifying a password while locked, or refusing on a bare username.** Both turn the lockout into an oracle or a denial of service on the real admin.
- **Resetting a store on a version mismatch.** The three stores here refuse (`AuthUnavailable`); `inventory/db.py`'s reset-on-mismatch is not the pattern for credentials or counters.
- **Putting a secret in an audit `before`/`after`, a log line or an error message.** `ApiKey` has no secret field, keep it that way; `tests/unit/test_gateway_auth_leak.py` greps for a raw key everywhere.
- **Copying `org.default_key_limits` over something that should rank higher.** `create_key` with `limits=None` copies the org defaults; a new layer must decide who wins, field by field, and test it.

## Functions
- `control_plane/contracts/auth.py::ApiKey` -- the key row (no secret field); `Org`, `MintedKey` (the only secret-carrying shape) beside it.
- `control_plane/contracts/auth.py::KeyPort` -- the protocol `KeyService` implements; `AuthPort`, `LimitPort`, `PricingPort` are the others.
- `control_plane/contracts/auth.py::AuthUnavailable` -- the fail-closed error; raise it for any unopenable or wrong-version store.
- `control_plane/contracts/manifest.py::build` -- reflects types, enums, constants, derived facts and env into the manifest; used by the freshness test.
- `control_plane/contracts/manifest.py::resolve` -- turns a `module:attr` string into the live object; use it in tests over a derived fact.
- `control_plane/contracts/routes.py::build` -- every method+path both apps answer; what `routes.json` is checked against.
- `control_plane/contracts/document.py::render` -- the markdown lookup surface (`docs/CONTRACTS.md`) from the two manifests.
- `control_plane/contracts/derived.py::by_name` -- look up a derived fact by name before restating one.
- `control_plane/keys/format.py::mint` -- new `(key_id, secret, raw)`; with `parse`, `hash_secret` and `verify` the only key-format code.
- `control_plane/keys/service.py::KeyService.authenticate` -- bearer token to `ApiKey` or `AuthError` with the right code; records last use at most every 5 s.
- `control_plane/keys/service.py::KeyService.create_key` -- mints a key, applies org defaults, returns the one-time `MintedKey`.
- `control_plane/keys/service.py::KeyService.effective_allowlist` -- the models a key may see; call it, do not re-derive the org-narrowing rule.
- `control_plane/keys/service.py::KeyService.validate_against_org` -- the org ceiling check; call on every create/patch path with the exposed model list.
- `control_plane/keys/service.py::validate_limit_name` -- accepts a fixed `LIMIT_TYPES` name or `model:<id>:<suffix>`.
- `control_plane/keys/store.py::row_to_key` -- the one place a row becomes an `ApiKey`.
- `control_plane/auth/service.py::AuthService.login` -- verify, lockout, session mint; the only login path.
- `control_plane/auth/service.py::AuthService.reauthenticate` -- the password check for admin-level changes; mints nothing.
- `control_plane/auth/service.py::AuthService.audit` -- append a chained audit row; the gateway reaches it through `authz.audit_request`.
- `control_plane/auth/service.py::AuthService.verify_chain` -- recompute the whole chain and the `HEAD` check; `head_hash` and `audit_page` read it.
- `control_plane/auth/service.py::hash_cookie` -- sha256 of a cookie value, the only form stored.
- `control_plane/auth/store.py::walk_chain` -- verifies rows in id order; also run by `python3 -m control_plane.auth.verify`.
- `control_plane/auditlog.py::AuditMirror` -- the JSONL mirror and `HEAD` file written after each commit.
- `control_plane/limits/service.py::LimitService.check_and_reserve` -- the admission check; returns a decision and a reservation to settle.
- `control_plane/limits/service.py::LimitService.settle` -- counts actuals for key and org in one transaction; pair with `mark_started` and `check_target_kind`.
- `control_plane/limits/service.py::LimitService.states` -- `LimitState` rows (used, limit, resets) for the UI and `/v1/key`.
- `control_plane/limits/cycle.py::cycle_bounds` -- `(cycle_start, resets_at)` for an anchor; the key of every counter row.
- `control_plane/limits/types.py::parse_limit_type` -- `(base, served_name | None)`; the way to read any limit name.
- `control_plane/limits/pricing.py::PricingStore.cost` -- dollars and `CostBasis` for a request; the only source of either.
- `control_plane/gateway/authz.py::AuthStores` -- lazy opener of the four stores as `ctx.auth_stores`; `create_app` never touches disk.
- `control_plane/gateway/authz.py::OPEN_ROUTES` -- the hardcoded list of routes open without a session; read by the coverage test.
