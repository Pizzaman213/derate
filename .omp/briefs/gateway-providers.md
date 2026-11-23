# gateway, providers brief

Owns: the one HTTP surface (`/v1`, `/api`, the built UI) and the credential gate in front of it; remote upstreams as route targets and the key material they hold.
Code: control_plane/gateway/, control_plane/providers/, control_plane/redaction.py, control_plane/logfiles.py   Tests: tests/unit/test_gateway*.py, tests/unit/test_proxy*.py, tests/unit/test_providers*.py, tests/unit/test_logfiles.py   Read first: control_plane/gateway/README.md (failover, inbound auth, layout), control_plane/providers/README.md (allowlist, keys, failure behaviour), CLAUDE.md "Rules that are not style"

An agent reads Depth 1 for every part it touches, and goes deeper only as far as the task needs.
Every `path::symbol` reference below is verified by the gate, so the brief cannot name code that
does not exist.

## Depth 1 -- 30 seconds
- `create_app` builds one FastAPI app: routers for `/v1` and `/api`, `/healthz`, then the built UI mounted at `/` last. `control_plane/gateway/app.py::create_app`.
- Every HTTP request passes `AuthGuard` first: a session cookie on `/api`, a derate key on `/v1`, nothing else is open except one hardcoded list. A new route is gated by default. `control_plane/gateway/authz.py::AuthGuard`.
- A `/v1` request is chosen, admitted, sent, and re-offered to another target on a retryable failure. It never gets half of two answers. `control_plane/gateway/openai_api.py::_serve`, `control_plane/gateway/proxy.py::UpstreamProxy`.
- A remote provider is a route target like a local deployment. It serves only the models an operator switched on, and its key is a reference name, never a value. `control_plane/providers/service.py::ProviderService`.
- Never let a key reach a client, a log line, a disk write or a screen. `control_plane/redaction.py::Redactor` is the shared scrubber.

## Depth 2 -- the rules
- **Routers register above the `StaticFiles` mount.** A mount at `/` catches every path not matched by an earlier route, so a later router never runs and `/api/...` answers `index.html` (also `/install.sh`, `/v1/realtime`). Enforced by order in `control_plane/gateway/app.py::create_app`; pinned by the `*_is_reachable_even_when_the_ui_is_mounted` tests in `tests/unit/test_gateway.py`.
- **Deep UI paths fall back to the document; `/api`, `/v1`, `/assets`, `/healthz`, `/install.sh` never do.** Screens live in the path and no file exists under them, but a misspelled API path answering 200 with HTML shows as a JSON parse error, and a missing hashed asset must be a 404 or a broken deploy looks like a blank page. `control_plane/gateway/app.py::_UIStatics` (its `_NO_FALLBACK` set); tests in `tests/unit/test_gateway_runtime.py::test_the_deep_path_fallback_does_not_swallow_a_missing_asset`.
- **The gate is plain ASGI, and no check goes in as `@app.middleware`.** One registered after `AuthGuard` runs before it, so its refusal carries no request id, no security headers and no access row. `BaseHTTPMiddleware` also doubled p99 on streams. See `control_plane/gateway/authz.py::AuthGuard` and the ordering comments in `control_plane/gateway/app.py::create_app`.
- **Gate decisions read `scope["path"]`, never `request.url`.** Starlette builds `request.url` from the caller's `Host` header and re-parses it, so `Host: x#` emptied the path the gate checked and an anonymous caller reached `/api` handlers. The fix refuses a malformed `Host` (400 `invalid_host`) and decides on `scope["path"]`. Check your tree has it: `grep -n host_is_valid control_plane/gateway/authz.py`. Tests: `tests/unit/test_gateway_auth_coverage.py`.
- **A route is open only by naming it in one list.** `control_plane/gateway/authz.py::OPEN_ROUTES` plus `control_plane/gateway/authz.py::SETUP_ROUTE`; `.omp/work/auth-keys/sets/setup_routes.json` mirrors them and `tests/unit/test_gateway_auth_coverage.py` walks every route in `routes.json` and fails on one that answers without credentials. Websockets check for themselves (`control_plane/gateway/authz.py::require_session`, `control_plane/gateway/authz.py::require_key`) and close 1008 before `accept`.
- **Fail closed when a store will not open.** A broken auth store answers 503 `auth_unavailable` on every gated path before a missing cookie could be reported as the problem. `control_plane/gateway/authz.py::AuthStores`, `control_plane/gateway/authz.py::unavailable`.
- **A cross-site write is refused before anything reads its cookie.** `control_plane/gateway/csrf.py::is_cross_site_write` runs inside the guard so the refusal row carries the request id. Trusted origins come from `control_plane/gateway/csrf.py::trusted_origins`.
- **Only the client's `content-type`, `accept`, `user-agent` and `openai-beta` go upstream.** `Authorization` and `Cookie` are never forwarded; a remote target gets its own provider's key and a local one gets none. `control_plane/gateway/proxy.py::_FORWARDED_REQUEST_HEADERS`; client routing fields (`models`, `route`, `fallbacks`, `provider`) are stripped in `control_plane/providers/service.py::_CLIENT_ROUTING_FIELDS`.
- **Everything is rebuilt per attempt.** Body, auth header, provider key and admission commitment come from the original request on each failover attempt; hoisting any of it carries one target's rewrite or key onto the next. `control_plane/gateway/openai_api.py::_serve` (its `_attempt_on`).
- **Retryable means transport failure or 5xx; every 4xx is terminal; nothing is retried after the first body byte.** A 422 is the backend working. After the chain is exhausted the client gets the last upstream 5xx verbatim. `PoolTimeout` is ours, not the backend's: it never trips the breaker (503 `upstream_pool_exhausted`). See `control_plane/gateway/errors.py::upstream_pool_exhausted`, `control_plane/gateway/breaker.py::CircuitBreaker`.
- **An upstream response is always closed, and under cancellation.** Starlette cancels the body's anyio scope when the client hangs up, so the `finally` close must be shielded; an unstarted generator runs no cleanup, so the proxy tracks open responses and a janitor reaps never-read ones (never a running stream: `upstream_read_timeout_s` is `None` on purpose). `control_plane/gateway/proxy.py::close_quietly`, `control_plane/gateway/proxy.py::UpstreamProxy`.
- **The breaker only subtracts from health, only transport failures trip it, and reads have no side effects.** `state()`/`is_open()` look, `begin()` claims, and no `await` may sit between `Router.select` and `begin()`. `control_plane/gateway/breaker.py::CircuitBreaker`, `control_plane/gateway/router.py::Router.select`.
- **A provider serves what it was asked to, not what it publishes.** `enabled_models` has three states (empty = nothing, a set = exactly those, `None` = whole catalogue for old records) and `serves()` is the only reader. Every surface (`/v1/models`, chat picker, routing) builds from `servable()`; only `catalogue()` is unfiltered. `control_plane/providers/runtime.py::ProviderRuntime.serves`, `control_plane/providers/service.py::ProviderService.servable`.
- **A provider's key is a name, resolved at request time and nowhere else.** `api_key_ref` may only be the minted `DERATE_<ID>_API_KEY` or an operator's `DERATE_SECRET_<NAME>`; the env is never read for any other name, or a session could point `base_url` at itself and send the cluster token as a bearer. `control_plane/providers/service.py::ref_allowed`, `control_plane/providers/secrets.py::env_resolvable`. The client does not follow redirects for the same reason.
- **`base_url` is validated because the key goes there.** https unless Ollama/custom, no link-local/multicast/unspecified IP literal, a fixed-home kind keeps its hostname. `control_plane/providers/service.py::validate_base_url`; tests in `tests/unit/test_proxy_provider_hardening.py::test_base_url_refused`.
- **Three layers keep keys out of everything.** Only the reference is stored; `Redactor` scrubs every resolved value and known key shape from errors, logs and headers; `assert_no_key_material` raises over every serialized response. `control_plane/providers/serialization.py::assert_no_key_material`; `tests/unit/test_providers.py::test_no_key_material_in_any_output` must stay green.
- **Provider JSON is an explicit allowlist, not a dataclass dump.** A new field on `Provider` does not reach the screen until named. `control_plane/gateway/serialize.py::provider_payload`, `control_plane/providers/serialization.py::provider_public_dict`; `api_key` is always `"***"`.
- **Redaction lives outside `providers/` on purpose.** Every node writes log files and the worker path may not import the provider stack. `control_plane/redaction.py` is standard library only; `control_plane/providers/secrets.py` re-exports it. `tests/unit/test_logfiles.py::test_importing_logfiles_does_not_pull_the_provider_stack` pins it.
- **An unhealthy provider is a timed state, and 401/403 is the exception.** Backoff expires into `half_open`, so one request can clear it; `auth_rejected` latches until the key changes because retrying a refused key burns quota. 429 cooldowns are per `(provider_id, upstream_id)`, capped by `RETRY_AFTER_MAX_S`. `control_plane/providers/runtime.py::ProviderRuntime.half_open`, `control_plane/providers/runtime.py::ProviderRuntime.note_rate_limit`.
- **One log folder, two files, one filter.** `proxy.log` is the proxy path's loggers, filtered at the handler (a traceback's continuation lines carry no logger name); add a logger to `PROXY_LOGGERS`, never a second file. The redacting filter is on the handler. An unwritable folder is a warning, never a failed start. `control_plane/logfiles.py::PROXY_LOGGERS`, `control_plane/logfiles.py::install`.

## Depth 3 -- how to change it
**Add an `/api` route**
1. Put it in the router closest to its subject (`control_plane/gateway/keys_api.py::create_router`, `control_plane/gateway/internal_api.py::create_router`, ...). Do not add it by hand to any open list; a new route is session-gated by default.
2. A new router file must be included in `control_plane/gateway/app.py::create_app` above the static mount, with the same comment as its neighbours.
3. Serialize through an allowlist (`control_plane/gateway/serialize.py`), return refusals as `control_plane/gateway/errors.py::error_response`, and audit every mutation.
4. Regenerate: `python3 -m control_plane.contracts.routes --write`, then the `derived_by` command in `.omp/work/auth-keys/sets/api_routes.json` and paste its output into `members`; then `python3 -m control_plane.contracts.document --write`.
5. Tests: a handler test beside `tests/unit/test_gateway_ui_api.py`; `tests/unit/test_gateway_auth_coverage.py` already proves it answers 401 with no cookie.

**Open a route to anonymous callers (rare; needs a stated reason)**
1. Add `(method, path)` to `control_plane/gateway/authz.py::OPEN_ROUTES`.
2. Add the same row, with `why`, to `.omp/work/auth-keys/sets/setup_routes.json` (`always_open`).
3. Make the handler read the stores itself, never trust the path alone. Test with `@pytest.mark.raw_http` (see `tests/unit/conftest.py`).

**Add or change a `/v1` endpoint**
1. A three-line wrapper over `control_plane/gateway/openai_api.py::_proxy` (JSON) or `control_plane/gateway/openai_api.py::_proxy_multipart` (uploads); both meet in `control_plane/gateway/openai_api.py::_serve`, which owns limits, fallback and settling.
2. Decide the modality (`control_plane/contracts/modality.py`); `_proxy` refuses a mismatch before anything is sent. A mismatch error is `control_plane/gateway/errors.py::wrong_modality`.
3. A restricted key must get the same 403 for a model that exists and one that does not: use `control_plane/gateway/authz.py::model_allowed` before the catalogue.
4. Settle the limit reservation exactly once (`_serve`'s `_settle_limits`); a remote target meters through `control_plane/gateway/proxy.py::UpstreamProxy.forward_provider`.
5. Regenerate routes as above (`v1_routes.json`). Tests in `tests/unit/test_gateway.py`, metering in `tests/unit/test_gateway_proxy_metering.py`.

**Add a provider kind**
1. Add the member to `control_plane/contracts/providers.py::ProviderKind` (a contract change: announce it), then `python3 -m control_plane.contracts.manifest --write` and the document step.
2. Add its `KindSpec` (`control_plane/providers/kinds.py::KindSpec`, reached via `control_plane/providers/kinds.py::spec_for`): base URL, auth style, pricing unit, paths. Leave prices `None` unless the upstream publishes them.
3. If it names a fixed home, `control_plane/providers/service.py::validate_base_url` already pins the hostname; if it hosts a LAN box, add it to the LAN kinds there.
4. A model-list shape the parser does not know goes in `control_plane/providers/discovery.py::parse_models`.
5. Tests beside `tests/unit/test_providers.py` and the stub (`control_plane/providers/stub.py::build_stub_service`). Include a key-in-error-body case.

**Add a field to a provider response**
1. Add it to `control_plane/providers/serialization.py::provider_public_dict` and `control_plane/gateway/serialize.py::provider_payload` by name (allowlist).
2. Run it through `assert_no_key_material`; extend `tests/unit/test_providers.py::test_no_key_material_in_any_output` if the field can hold text.
3. Mirror the shape in `ui/src/api/types.ts` (see the UI brief).

**Add a logger to the proxy log, or a gateway tunable**
1. A logger: append its prefix to `control_plane/logfiles.py::PROXY_LOGGERS`; test in `tests/unit/test_logfiles.py`.
2. A tunable: add it to `control_plane/gateway/settings.py::GatewaySettings` with a working default; an env var also goes in `control_plane/envspec.py::VARIABLES`, then run the three contracts generators (`manifest`, `routes`, `document`) because `docs/CONTRACTS.md` lists environment variables.

## Pitfalls
- **Host steered the gate.** `request.url.path` is built from the `Host` header; `Host: x#` gave an empty path and anonymous callers reached `GET /api/keys`. In this tree `control_plane/gateway/authz.py::_refusal` and `_cross_site_refusal` still read `request.url.path` unless the `invalid_host` fix has landed (`grep -n host_is_valid control_plane/gateway/authz.py`). Never add a decision that reads `request.url`.
- **The API-token guard predates the rule above.** `control_plane/gateway/app.py::create_app` registers `_api_token_guard` with `@app.middleware`, so it answers before `AuthGuard` with no request id or headers. Do not copy that pattern for a new check.
- **A router below the static mount quietly answers HTML.** The failure is a JSON parse error in the browser with a 200 in the log. If a new `/api` route returns `index.html`, check registration order first.
- **PoolTimeout counted as a transport failure took the gateway down (2026-09-08).** Leaked connections filled the pool, three timeouts benched two healthy backends, and the half-open probe died on the same empty pool. Keep it off the breaker. See the gateway README, section 1a.
- **A fixed header-hold timer protected the wrong model.** 130,815 holds fired, all on the 30B (76-130s prefill), none on the 0.5B. Recovery is decided by bytes the client has seen, not a clock. `control_plane/gateway/proxy.py::UpstreamProxy.forward` (its `reopen` callback).
- **One transient `ReadTimeout` took an Ollama box off the air for a working day (2026-09-08)** while it answered pings in 11 ms, because unhealthy was a latch and routing never offered it a request. Keep it timed; see `ProviderRuntime.half_open`.
- **An entropy check for secrets refused all 96 Mistral-Small-24B-Instruct-2501 variants.** A model id is exactly the shape it gets wrong, so ids are validated against what the upstream publishes, not with `_screen_free_text`. `control_plane/providers/service.py::_screen_free_text`.
- **A provider ref could name `DERATE_TOKEN`.** With `base_url` pointed at itself the refresh sent the cluster token as a bearer. A stored record with an old name reports `key_state: missing`; do not widen `env_resolvable`.
- **Upstreams echo credentials in 401 bodies.** Error text must pass the shared redactor, and it must be the shared instance: a fresh `Redactor` only scrubs values it has been told. `control_plane/gateway/errors.py::detail`, `control_plane/providers/service.py::ProviderService._error_message`.
- **A pasted key shadowed by an env var.** The environment is read first, so writing a key under a name an env var already holds with a different value is refused; otherwise the provider authenticates as somebody else. See the Keys section of the providers README.
- **`/data` and the log folder differ.** Logs default to `<project root>/logs`, which in the image is a layer, not the data volume; set `DERATE_LOG_DIR` to keep them across `docker rm`.
- **Tests sign in for you.** `tests/unit/conftest.py` mints a real admin and key for every `TestClient`; a test about a 401 must use `@pytest.mark.raw_http` or it silently tests the signed-in path.

## Functions
- `control_plane/gateway/app.py::create_app` -- composition root: middleware order, router order, static mount. Read before adding a router or middleware.
- `control_plane/gateway/app.py::_UIStatics` -- the built UI with the deep-path fallback and cache rules.
- `control_plane/gateway/authz.py::AuthGuard` -- the credential gate and access-row writer for every HTTP request and socket.
- `control_plane/gateway/authz.py::OPEN_ROUTES` -- the one list of routes reachable with no session.
- `control_plane/gateway/authz.py::authenticate_key` -- resolve a bearer to a `Principal` or raise the coded refusal.
- `control_plane/gateway/authz.py::current_session` -- the live admin session for a request, or None.
- `control_plane/gateway/authz.py::model_allowed` -- whether a key may name a model at all; call before any catalogue lookup.
- `control_plane/gateway/authz.py::require_session` -- the websocket session gate (close 1008).
- `control_plane/gateway/authz.py::audit_request` -- write an audit row with the request's actor and address.
- `control_plane/gateway/authz.py::client_ip` -- the client address, honouring `X-Forwarded-For` only from trusted proxies.
- `control_plane/gateway/csrf.py::is_cross_site_write` -- the cross-site write predicate.
- `control_plane/gateway/headers.py::SecurityHeaders` -- nosniff, no framing, CSP; outermost so refusals carry them.
- `control_plane/gateway/openai_api.py::_proxy` -- JSON `/v1` entry: body bound, model lookup, modality check, then `_serve`.
- `control_plane/gateway/openai_api.py::_serve` -- limits, failover chain, hedging, settle; the heart of `/v1`.
- `control_plane/gateway/proxy.py::UpstreamProxy` -- one client per origin, response ownership, per-attempt accounting.
- `control_plane/gateway/proxy.py::UpstreamProxy.forward_provider` -- send a request to a remote provider target and meter it.
- `control_plane/gateway/router.py::Router.select` -- pick a target under the policy with failed ones excluded.
- `control_plane/gateway/router.py::Router.parkable` -- whether a request with no target may wait for one.
- `control_plane/gateway/targets.py::build_index` -- merge local deployments and remote provider models into one target index.
- `control_plane/gateway/errors.py::error_response` -- the OpenAI-shaped error body; use it for every refusal.
- `control_plane/gateway/errors.py::detail` -- an exception as a scrubbed one-liner for a message.
- `control_plane/gateway/serialize.py::provider_payload` -- the allowlisted provider JSON for `/api`.
- `control_plane/providers/service.py::ProviderService.route_targets` -- remote models as `RouteTarget`s, admitting set only.
- `control_plane/providers/service.py::ProviderService.open_upstream` -- open a metered, scrubbed upstream response (use `forward` for bytes only).
- `control_plane/providers/service.py::ProviderService.resolve_key` -- the secret for a provider, at request time only.
- `control_plane/providers/service.py::ref_allowed` -- may this provider hold this `api_key_ref`.
- `control_plane/providers/service.py::validate_base_url` -- refuse a `base_url` the stored key must not go to.
- `control_plane/providers/secrets.py::SecretStore` -- `secrets.json` at mode 0600; `put`, `get`, `delete`.
- `control_plane/redaction.py::Redactor` -- `remember` a value, `scrub` text, `assert_clean` before emitting.
- `control_plane/logfiles.py::install` -- set up `node.log` and `proxy.log` with the redacting filter.
