# limits

What a key -- and the org it belongs to -- may spend, and the check that
enforces it before a request is dispatched. `LimitService` implements
`contracts/auth.py::LimitPort`; `PricingStore` implements `PricingPort`. Both
live in one file, `limits.db`, under `paths.data_dir()`.

A limit is named by a string from `LIMIT_TYPES`: fourteen fixed names plus
`model:<served_name>:<suffix>` for the per-model ones. A refusal names it as
`<scope>:<limit_type>` (`key:requests_cycle`, `org:model:gpt-4o:rpm`) -- the
`limit_source` a 429 carries and a telemetry row records. The messages render
verbatim, so each one says which limit, how much of it is used, and when it
frees.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `service.py` | 890 | `LimitService`: the check order, reservations, settle, `states()`, raise and reset, the rpm/tpm windows |
| `pricing.py` | 194 | `PricingStore`: the `local_pricing` table and `cost()`, the one place a dollar figure and its `CostBasis` come from |
| `types.py` | 123 | `parse_limit_type()`, each base's `LimitKind`, and the counter metric each cycle limit reads |
| `_db.py` | 70 | the `inventory/db.py` connect pattern, the shared `meta` table, the per-service schema-version key |
| `cycle.py` | 33 | `cycle_bounds()`: 30-day cycles anchored at `created_at` |
| `__init__.py` | 20 | the export surface |

## How the gateway uses this

`gateway/authz.py::AuthStores` opens both on first use as
`ctx.auth_stores.limits` and `ctx.auth_stores.pricing`. `openai_api.py::_serve`
calls `check_and_reserve` before dispatch -- a refusal becomes the 403/400/413/429
a client sees, with `X-RateLimit-*` on a 429 -- and `settle` once the response
is over, with the cost from `PricingStore.cost()`. `gateway/keys_api.py` serves
`states()` as `limit_states`, `POST /api/keys/{key_id}/raise` and
`/reset-cycle`, and `/api/pricing`; `GET /v1/key` reads `cycle_bounds` and
`states()` for the calling key.

## The check, in order

`check_and_reserve(principal, model=, target_kind=, body=, route_limits=)`
stops at the first refusal:

1. **Allowlist** -- `KeyPort.effective_allowlist(key, org, [model])` must
   contain the model, else 403 `model_not_allowed`.
2. **Per-request caps**, the tighter of key and org (key wins a tie).
   `max_output_tokens_per_request`: a `max_tokens` / `max_completion_tokens` /
   `max_output_tokens` above it is 400 `max_output_tokens_exceeded`; none at
   all comes back as `inject_max_tokens`. `max_prompt_tokens_per_request`: the
   prompt estimate above it is 413 `max_prompt_tokens_exceeded`.
3. **`max_concurrent`, `rpm`, `tpm`** and the per-model `rpm`/`tpm`, key then
   org, then the served name's own route limit -- 429.
4. **Cycle limits**, key then org -- 429.

The route limit is `RoutingConfig.limits` (`rpm`/`tpm` only), passed in by the
proxy from `Router.limits_for(model)`. It is a third scope, `route:<model>`,
with one shared 60 s window per served name and nothing else: no cycle, no
override, no `LimitState` row. A refusal names it `route:rpm` / `route:tpm`.

A count blocks at `used >= limit`, and `used` includes every live
reservation, so a `requests_cycle` of 5 admits exactly 5 and thirty-two
concurrent requests cannot each see the same headroom. A token or dollar
limit admits while under and lets the one request that crosses it finish; it
settles in full and the next one is refused.

The prompt estimate is `gateway/admission.py:221`'s formula (chars/4 over
`messages`, `prompt`, `input`, plus 4 per message), reimplemented in
`estimate_prompt_tokens()` because the original is an instance method reading
`self._settings` -- plus `IMAGE_TOKENS_ESTIMATE` (1000) per image part, which
used to count as zero and let an image-heavy prompt past every prompt cap. The
output estimate is the request's own ceiling, else the injected cap, else 256,
times `completions_requested()` (the larger of `n` and `best_of`): every output
ceiling is per completion, so `max_output_tokens_per_request` is checked
against the ceiling times that count, and an injected ceiling is the cap
divided by it.

Which cycle limits apply depends on the target. `local_*` binds only a LOCAL
target, `cloud_*` only a REMOTE one, and `target_kind=None` binds neither. The
gateway never passes `None` for a served name that has targets: a name with
both kinds is admitted as REMOTE (the stricter assumption), and
`check_target_kind(reservation, kind)` re-checks the other kind's limits once
the router has picked a target -- the reservation's own estimate was booked
under REMOTE, so it never counts against the LOCAL limits it is re-checked
for. A dollar limit does not stop a LOCAL request whose model has no complete
price row: it will settle unpriced and cannot spend a dollar. That is what
makes the cloud fallback work for `budget_usd_cycle` at all -- when
`cloud_tokens_cycle`, `cloud_usd_cycle` or `budget_usd_cycle` refuses a REMOTE
request and the key has `cloud_fallback_local_model`, the refusal carries
`fallback_model`, and the gateway re-checks with that model and
`target_kind="local"`.

## Reservations and settle

Every admitted request holds a reservation until `settle()` is called with
what it used (`UsageActuals`) or with `None` (it never ran: everything is
given back, nothing is counted). **Every reservation must be settled**, in a
`finally`: a leaked one holds a `max_concurrent` slot and its estimate until
the process restarts. A second settle is a no-op.

`None` means "never ran" only until `mark_started(reservation)`: the gateway
calls it when the first chunk of an answer exists. A started request settled
with `None` -- a transfer that broke after bytes reached the client -- keeps
its rpm/tpm window entry and counts its reserved estimate, because an
upstream produced those bytes and a provider billed for them. A client that
hangs up mid-stream is settled with what was seen (the proxy's own count),
never voided.

Settle adds every metric for both the key and the org in one transaction, at
the cycle each is in *at settle time*: `tokens_total`, `tokens_input`,
`tokens_output`, `requests`, `local_tokens` or `cloud_tokens`,
`model:<served>:tokens`, and -- only when the cost has a basis other than
`unpriced` -- `usd`, `local_usd` or `cloud_usd`, `model:<served>:usd`. An
unpriced request increments `unpriced_requests` instead and never counts
toward a dollar limit. Settle never raises: the response has already been
served, so a store failure is logged and the next check fails closed.

A reservation's dollar estimate comes from `PricingStore.cost()` with no
provider rates, so it is non-zero only for a priced local model. Concurrent
REMOTE requests can therefore overshoot a dollar limit by what is in flight;
the counters are exact once they settle.

## Cycles, raises and resets

`cycle_bounds(anchor, now)`: `n = floor((now - anchor) / 30 d)`, never below
0; `cycle_start = anchor + n * 30 d` is the counter rows' key and comes out
bit-identical for every instant in the cycle; `resets_at` is the next cycle's
first instant. The anchor is the key's or org's `created_at`.

`raise_for_cycle()` writes an `overrides` row keyed by this cycle's start:
the effective limit changes, the counters do not, and the next cycle reads
the configured value again. Only a limit that is configured can be raised.
`reset_cycle()` deletes this cycle's counter rows and appends a
`cycle_resets` row; older cycles stand. Neither writes the audit log -- the
gateway does, with the session's actor.

## Windows and concurrency

rpm/tpm are 60-second sliding deques in memory, per `(scope, id)` and per
`(scope, id, model)`. One entry is shared by all four windows a request sits
in, so settle replaces its token estimate with the actual once. A refused
request adds nothing. A window's `resets_at` is when its oldest entry ages
out; `max_concurrent`'s is now. Neither survives a restart, and neither needs
to.

## The store

`limits.db` is shared by `PricingStore` (`local_pricing`) and `LimitService`
(`cycle_counters`, `overrides`, `cycle_resets`). Each holds its own
connection, creates only its own tables, and keeps its own version key in the
shared `meta` table (`pricing_schema_version`, `limits_schema_version`). A
file from another version is refused rather than reset, because a counter is
the only record that a cycle's budget was spent.

Both are opened lazily: `open()` never touches the disk, so `create_app`
cannot fail on a bad data directory. The first call that needs the store
opens it; one that cannot be opened raises `AuthUnavailable` (the gateway
answers 503 `auth_unavailable`) and is tried again on the next call. A check
with no limits configured never reads the store.
