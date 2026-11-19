# auth-keys — progress ledger

Resume rule: if this file exists, skip `done` units, rerun the last done unit's
gate, continue. Nothing is `done` without a verbatim gate line.

Plan: `.omp/work/auth-keys/PLAN.md`. Lock: `.omp/work/auth-keys/LOCK` (gitignored).
Coordinator for this run: `:18120` (agent port `:18130` — `:8081` is taken by
another process; `DERATE_AGENT_PORT=18130`). Clean run (W5): `:18121` / `:18131`.

## Units

| id | depends_on | status | owner | owned paths | gate command | gate last line | timestamp | notes |
|---|---|---|---|---|---|---|---|---|
| U0.1 | — | done | orchestrator | `.omp/work/auth-keys/**`, `.gitignore` | `python3 -m pytest -q -m "not slow"` | `1 failed, 2034 passed, 14 skipped, 9 deselected, 2 xfailed, 7 warnings in 187.72s (0:03:07)` | 2026-11-17 19:23 | baseline; the 1 failure is the peer's DERATE_GALLERY_ONLY |
| U0.2 | U0.1 | done | orchestrator | `.omp/work/auth-keys/PLAN.md` | file exists | `PLAN.md 299 lines` | 2026-11-17 19:23 |  |
| U0.3 | U0.2 | done | orchestrator | `control_plane/contracts/{auth,telemetry}.py`, `contracts/__init__.py`, `contracts/routes.py`, `contracts/derived.py`, `envspec.py`, `requirements.txt`, `pyproject.toml`, generated docs | `python3 -m pytest -q tests/unit/test_contracts_manifest.py tests/unit/test_single_source.py` | `1 failed, 23 passed in 7.21s` | 2026-11-17 19:23 | the 1 failure = baseline DERATE_GALLERY_ONLY; sets written: limit_types 18, telemetry_fields 63, v1_routes 8, api_routes 68, http_test_files 21, setup_routes 5+1 |
| U1.auth | U0.3 | done | worker | `control_plane/auth/**`, `tests/unit/test_auth*.py` | `python3 -m pytest -q tests/unit/test_auth*.py` | `32 passed in 1.03s` | 2026-11-17 19:28 | fail closed on schema mismatch (no wipe); change_password/remove_admin end all that admin's sessions; extra `hasher=` kwarg on open for tests; ESTATE must list auth.db + -wal/-shm |
| U1.keys | U0.3 | done | worker | `control_plane/keys/**`, `tests/unit/test_keys*.py` | `python3 -m pytest -q tests/unit/test_keys*.py` | `68 passed in 1.51s` | 2026-11-17 19:29 | unknown ids raise KeyError (gateway → 404), bad args ValueError (→ 400); validate_against_org is the gateway's call before save; KEY_PATTERN in keys/format.py — redaction.py copies it + derived.py row |
| U1.limits | U0.3 | done | worker | `control_plane/limits/**`, `tests/unit/test_limits*.py` | `python3 -m pytest -q tests/unit/test_limits*.py` | `92 passed in 1.77s` | 2026-11-17 19:43 | stores open lazily (first use raises AuthUnavailable, retries); settle never raises, must be called in finally (None = release); local unpriced passes $ limits; target_kind=None skips local_/cloud_ pools; prompt estimate copied from admission.py:221 (needs instance) into limits/service.py::estimate_prompt_tokens |
| U1.telem | U0.3 | done | worker | `control_plane/telemetry/**` except `config.py`, `tests/unit/test_telemetry_keys*.py` | `python3 -m pytest -q tests/unit/test_telemetry.py tests/unit/test_telemetry_keys*.py` | `82 passed in 4.50s` | 2026-11-17 19:36 | vLLM metric names read off ghcr.io/pizzaman213/derate/vllm-audio:latest: vllm:spec_decode_num_{drafts,draft_tokens,accepted_tokens,accepted_tokens_per_pos}(_total), vllm:prefix_cache_{hits,queries}, cached_tokens; sglang image absent → not_reported (blocked: no sglang image on box). config.py + retention getattr cleanup + derived row applied by orchestrator |
| W1 gate | U1.* | done | orchestrator | `telemetry/config.py`, `contracts/derived.py` | `python3 -m pytest -q -m "not slow"` | `1 failed, 2257 passed, 14 skipped, 9 deselected, 2 xfailed, 7 warnings in 199.01s (0:03:19)` | 2026-11-17 19:43 | applied: telemetry/config.py (30 d raw, 1d rollups), retention getattr cleanup, derived.py request_record_fields row, registry/storage.py ESTATE rows; contracts regenerated. 1 failure = baseline DERATE_GALLERY_ONLY |
| U2.gateway | W1 | done | worker | `control_plane/gateway/**` except `app.py`/`main.py`, `tests/unit/test_gateway_auth*.py`, `tests/unit/conftest.py` | `python3 -m pytest -q tests/unit/test_gateway_auth*.py` | `171 passed in 11.43s` | 2026-11-17 20:30 | `pytest tests/unit/test_gateway_auth*.py`; http_test_files set → `1063 passed, 1 skipped, 7 warnings in 249.93s (0:04:09)`. Chat via session = admin key: every admin owns a personal admin key (minted at setup/admin-add, secret discarded, never expires); /api/chat/{completions,speech,transcriptions,models,voices} run the /v1 handlers with Principal(key=<admin key>, via=SESSION) — rows carry a real key_id, count against limits, show on /keys, no key reaches the browser; removing an admin disables their key ('admin removed'). Worker applied app.py (http_guard registered BEFORE _csrf_guard so CSRF stays outermost) + redaction.py (KEY_PATTERN in _KEY_PATTERNS); edited test_gateway.py (RunningServer wraps app with conftest.with_credentials; real key for the OpenAI-client and realtime socket tests). Unit data dir from tmp_path_factory (test_tts_runtime asserts its tmp_path is empty) |
| W2 gate | U2.gateway | done | orchestrator | `gateway/app.py`, `redaction.py`, `registry/storage.py`, `pyproject.toml` | FULL + `curl` 401 probe | `1 failed, 2430 passed, 14 skipped, 9 deselected, 2 xfailed, 7 warnings in 350.85s (0:05:50)` | 2026-11-17 20:30 | 1 failure = baseline DERATE_GALLERY_ONLY. Applied derived.py drt_key_pattern row; contracts regenerated; sets v1_routes 9 / api_routes 105 (unchanged on re-derive). :18120 restarted (archive migrated to v2, reroll pending): `curl localhost:18120/v1/models` → 401 missing_api_key; garbage key → 401 invalid_api_key; /api/topology no cookie → 401 session_required; POST /api/setup/admin → 200 {admin,key,org,secret_once}; again → 410 setup_closed; /v1/models with key → 200 |
| W3 prep | W2 | done | orchestrator | `ui/src/state/routes.ts`, `ui/src/shell/**`, `ui/src/api/{client,types,redact}.ts`, `ui/src/check/auth.mjs`, `ui/check.mjs`, `.omp/**` pack rows | `npm run typecheck` + `router.check.mjs` | `router: 16 URLs, all checks pass` | 2026-11-17 20:01 | routes.ts/router.check.mjs, state/auth.tsx (AuthProvider), AppShell gate (pollers under the gate), Header nav + sign-out, client.ts (fetchOk/req/reqUnscrubbed, 401 event, all auth/key methods, /api/chat/* paths), types.ts mirrors, redact.ts drt_ value scrub, check/auth.mjs + 7 coordinator verifiers import it, check.mjs probe, screens.check.mjs (cookie, anon login page, keys/orgs detail, drt_ assertion). Pack: 6 icons (icon-smith, icons:lint 48 pass), RevealModal registered/specced/checked (reveal-modal.check 28 pass), focus trap shared via components/focusTrap.ts |
| U3.login | W3 prep | done | worker | `ui/src/tabs/login/**`, `ui/src/tabs/SetupTab.tsx`, `ui/src/tabs/setup/**` | `node ui/src/tabs/login/login.check.mjs` | `all 29 checks passed` | 2026-11-17 20:01 | hermetic `node src/tabs/login/login.check.mjs`; tsc exit 0; ui:static clean; state.check.mjs (coordinator) pending W2-live run at W3 gate. ApiError.headers request applied by orchestrator |
| U3.keys | W3 prep | done | worker | `ui/src/tabs/keys/**` | `node ui/src/tabs/keys/keys.check.mjs` | `all 53 checks passed` | 2026-11-17 20:10 | keys.check 53 pass; whole-tree tsc clean; vite build to /tmp ok; ui:static clean; generated.check 91 pass after styleguide:build; table.check (coordinator) pending W2-live; chart: dashboard/Chart.tsx ChartGrid (nulls lift the pen, p95 band) |
| U3.orgs | W3 prep | done | worker | `ui/src/tabs/orgs/**` | `node ui/src/tabs/orgs/orgs.check.mjs` | `all 62 checks passed` | 2026-11-17 20:05 | orgs.check 62 pass; tsc 0 errors in tabs/orgs; ui:static clean; vite build to /tmp ok; table.check (coordinator) pending W2-live; limit-type picker duplicated in orgs/ and keys/ — dedupe at W3 gate; pack:sync needed for OrgDetail/OrgFields rows |
| U3.audit | W3 prep | done | worker | `ui/src/tabs/audit/**`, `ui/src/tabs/settings/{AdminsCard,PricingCard}.tsx` | `node ui/src/tabs/audit/audit.check.mjs` | `all 36 checks passed` | 2026-11-17 20:04 | audit.check 36 pass; settings/pricing.check 22 pass; log.check 25 pass vs a private :18133 coordinator (worker-started, stopped); ui:static clean; SettingsTab: AdminsCard on new 'Admins' sub-tab, PricingCard under Policy (applied by orchestrator) |
| W3 gate | U3.* | done | orchestrator | — | FULL + `screens` + `reveal.check.mjs` | `52 passed, 2 failed` | 2026-11-17 20:43 | UI `npm run check -- --strict` vs :18120 with check creds: the 2 failures are baseline (history.check: $HISTORY_FIXTURES unset; models/rows.check). Backend FULL: `1 failed, 2430 passed, 14 skipped, 9 deselected, 2 xfailed, 7 warnings in 345.19s (0:05:45)` (baseline DERATE_GALLERY_ONLY). screens.check `all 84 checks passed` (login, keys, keys-detail, orgs, orgs-detail, audit PNGs opened; orgs-detail width fixed by U3Orgs: .card→.card2). reveal.check `all 10 checks passed`; login/state 11, keys/table 14, orgs/table 14, audit/log 27 pass. `grep -c drt_ ui/screens/console.txt` → 0. Live probes: 0.5B model on :18120 → key request 200, cap-100 key max_tokens 99999 → 400 `max_tokens 99999 is over key:max_output_tokens_per_request of 100; ask for 100 or fewer`; session chat row key_id=18tcy1qkzb (check-admin's admin key); archive row: key_id/org_id set, context_bucket 0-8k, cost_basis unpriced, spec_decode_error not_enabled, cache_error not_reported → added `--enable-prompt-tokens-details` to deploy/flags.py _VLLM_COMMAND (flag read off the pinned image's vllm/entrypoints/launchers/cli_args.py); deploy tests `495 passed, 4 skipped` |
| U4.docs | W3 | done | worker | `docs/guide/**`, READMEs | READMEs exist + grep | `docs: 13 files` | 2026-11-17 20:43 | U4Docs: the-endpoint.md, first-run.md, troubleshooting.md, auth/keys/limits READMEs, 4 new tab READMEs, tabs/README.md, docs/index.md. Orchestrator: CLAUDE.md (reveal exception, authz rule, layout rows, conftest, check creds), TODO.md, README.md, LICENSE note, repo-map, control_plane/README.md + docs/index.md counts (12→15), compose.yaml/shell_config.py/test_shell.py no-auth claims |
| U4.load | W3 | done | orchestrator | `tests/load/**` | `python3 -m tests.load rps --duration 10 --json …/load-after.json` | `derated request rate: 100.0 rps` | 2026-11-17 21:00 | p99 ratios after/base at 25/50/100/250 rps: 1.71x / 1.04x / 1.88x / 0.59x — all under the 2x failure line |
| W5 | W4 | done | orchestrator | — | FULL from clean venv on :18121 + walkthrough | `1 failed, 2428 passed, 16 skipped, 9 deselected, 2 xfailed, 8 warnings in 342.21s (0:05:42)` | 2026-11-17 21:13 | clean copy /tmp/derate-clean + venv /tmp/derate-venv (pip install -r requirements.txt), npm ci + build; :18121. Backend 1 failure = baseline DERATE_GALLERY_ONLY. UI --strict `51 passed, 3 failed` = the 3 baseline failures (generated/style guide — since rebuilt green, history fixtures, models/rows). screens `all 84 checks passed`, reveal `all 10 checks passed`, console.txt drt_ hits 0 |

## Ownership map

### W0
orchestrator only.

### W1
| unit | owns | reads |
|---|---|---|
| U1.auth | `control_plane/auth/**`, `tests/unit/test_auth*.py` | `contracts/auth.py`, `inventory/db.py` |
| U1.keys | `control_plane/keys/**`, `tests/unit/test_keys*.py` | `contracts/auth.py`, `inventory/db.py` |
| U1.limits | `control_plane/limits/**`, `tests/unit/test_limits*.py` | `contracts/auth.py`, `contracts/telemetry.py`, `gateway/admission.py` |
| U1.telem | `control_plane/telemetry/**` minus `config.py`, `tests/unit/test_telemetry_keys*.py` | `contracts/telemetry.py`, telemetry README |

### W2
| unit | owns | reads |
|---|---|---|
| U2.gateway | `control_plane/gateway/**` minus `app.py`/`main.py`, `tests/unit/test_gateway_auth*.py`, `tests/unit/conftest.py` | gateway README, W1 READMEs |

### W3
| unit | owns |
|---|---|
| U3.login | `ui/src/tabs/login/**`, `ui/src/tabs/SetupTab.tsx`, `ui/src/tabs/setup/**` |
| U3.keys | `ui/src/tabs/keys/**` |
| U3.orgs | `ui/src/tabs/orgs/**` |
| U3.audit | `ui/src/tabs/audit/**`, `ui/src/tabs/settings/AdminsCard.tsx`, `ui/src/tabs/settings/PricingCard.tsx` |

### W4
| unit | owns |
|---|---|
| U4.docs | `docs/guide/**`, READMEs under `control_plane/{auth,keys,limits}`, `ui/src/tabs/{login,keys,orgs,audit}` |

## Baseline


- backend `python3 -m pytest -q -m "not slow"` (2026-11-17 19:20): `1 failed, 2034 passed, 14 skipped, 9 deselected, 2 xfailed, 7 warnings in 187.72s (0:03:07)` — pre-existing failure `test_single_source.py::test_every_environment_variable_in_the_tree_is_declared` (`DERATE_GALLERY_ONLY` in `.omp/tests/ui/{layout.check.mjs,run.mjs}`, the live design-pack peer's uncommitted work). Logged, not fixed.
- UI `npm run typecheck` exit 0; `npm run build` exit 0 (`✓ built in 1.75s`); `DERATE_CHECK_ORIGIN=http://localhost:18120 npm run check -- --strict` → `40 passed, 3 failed` (exit 1); failing verifiers listed below once captured.
- UI baseline failing verifiers (pre-existing, logged not fixed; full output was /tmp/authkeys-ui-baseline.txt): `../.omp/tests/ui/generated.check.mjs` (design pack: `style guide: design/index.html matches its sources`), `src/state/history.check.mjs` (`the gateway does rank dead rows above live ones`), `src/tabs/models/rows.check.mjs` (`a 2 MB metadata stub is not reported as a finished download`, `a fully cached repo is`, `and classifying from the step-down would have marked it — which is the bug`). Summary line: `40 passed, 3 failed`.

## Conflicts

- 2026-11-17: `ui/dist` is ALSO served by the peer coordinator on :18088 (`DERATE_UI_DIR=/home/connor/derate/ui/dist`, read off /proc/1066072/environ). A `npx vite build` of the auth-gated UI would blank its screens (its server has no /api/auth/*). Resolution: ui/dist was restored from a HEAD build (`git worktree add --detach /tmp/derate-head HEAD`, `npx vite build --outDir ui/dist`; hash `index-MG_l3k_w.js` equal on :18088); this run's UI builds go to `/tmp/derate-authkeys/ui-dist` and :18120 points there. `npm run build` (writes ui/dist) is deferred to W5. Remove the worktree at W5 (`git worktree remove --force /tmp/derate-head`).
- Design-pack peer active on `.omp/design/DESIGN.md`, `.omp/design/screens/*`, `.omp/graph/*` (mtimes < 30 min at W3 prep). Pack registration (icons, RevealModal) deferred to a quiet window per the plan's contingency.
- 2026-11-17 W2: launching `Qwen/Qwen2.5-0.5B-Instruct-GPTQ-Int4` on :18120 died in `flashinfer_autotune`: `PermissionError: [Errno 13] ... /cache/huggingface/derate-runtime-cache/vllm/flashinfer_autotune_cache/.../autotune_configs.json` — 2290 files under `~/.cache/huggingface/derate-runtime-cache` were `root:root` (written 2026-09-08 by a root container), unreadable by the launched container (user 1000:1000). Pre-existing environment fault, unrelated to auth. Fixed by `docker run --rm -v ~/.cache/huggingface/derate-runtime-cache:/c alpine chown -R 1000:1000 /c` (2290 → 0 foreign-owned files). Whatever writes as root there will do it again; not in this plan's scope.

## Notes


- argon2-cffi: this box's Python is PEP 668 externally-managed; every dependency lives in `~/.local` (user site). Installed with `python3 -m pip install --user --break-system-packages argon2-cffi==25.1.0` → `Successfully installed argon2-cffi-25.1.0 argon2-cffi-bindings-26.2.0`.
- U0.3 deviation: `AuthPort.login` returns `tuple[Session, str]` (the session and the cookie value) — the plan's `-> Session` cannot hand back the cookie, which is stored only hashed (`Session.session_id` = sha256).
- U0.3: `AuthError` subclasses `InvalidCredentials`, `LoginLocked`, `LimitExceedsOrg`, `AllowlistExceedsOrg`, `AuthUnavailable` are in `contracts/auth.py` beside `AuthError`; `Reservation` is a frozen dataclass there (opaque to callers).
- U0.3: `routes.py::_walk` now reflects `APIWebSocketRoute`; `WEBSOCKET /v1/realtime` appears in routes.json. `WEBSOCKET /api/nodes/{node_id}/shell` exists only when `DERATE_SHELL` is set (shell_api.py:68), so it is not in routes.json; U2 covers it by a dedicated test with the shell enabled.
- U0.3: the `request_record_fields` DerivedFact row is committed commented-out in `derived.py`; it is uncommented at the W1 gate.
- Coordinator :18120 needs `DERATE_AGENT_PORT=18130` (:8081 is taken).
- U1.telem engine-metric probe: `docker run --rm --entrypoint sh ghcr.io/pizzaman213/derate/vllm-audio:latest -c "grep -rhoE 'vllm:spec_decode[a-z_]+|vllm:prefix_cache_[a-z_]+|cached_tokens' /usr/local/lib/python3*/dist-packages/vllm | sort -u"` → cached_tokens, vllm:prefix_cache_hits, vllm:prefix_cache_queries, vllm:spec_decode_num_accepted_tokens(_total), vllm:spec_decode_num_accepted_tokens_per_pos, vllm:spec_decode_num_draft_tokens(_total), vllm:spec_decode_num_drafts. The plan's assumed names hold.
- Rollup migration: v1 `rollup_requests` dropped, re-rolled from the raw window (`meta.reroll_pending`). Pre-migration hourly rollups older than raw retention are lost (this box has days of history).
- U4.load (2026-11-17). Baseline measured from a HEAD worktree (`/tmp/derate-head`, same box, same harness) because U0.1 step 4 was not run before code changed. The default ladder could not be compared: HEAD's first request is ~55 ms cold, so at 1 or 5 rps (10-50 samples, p99 = max) the ladder's own stop rule ends the baseline at its first rung (`STOP: p99 57.3 ms is 14x the idle p50 of 4.1 ms`, twice). Both sides were therefore run with `--rungs 25,50,100,250 --duration 10`:

| rps | base p50 ms | after p50 ms | base p99 ms | after p99 ms | p99 ratio | base cpu% | after cpu% |
|---|---|---|---|---|---|---|---|
| 25 | 3.31 | 3.96 | 4.73 | 8.07 | 1.71x | 8 | 10 |
| 50 | 3.25 | 3.80 | 5.43 | 5.65 | 1.04x | 14 | 18 |
| 100 | 3.18 | 3.66 | 5.43 | 10.23 | 1.88x | 25 | 32 |
| 250 | 2.61 | 3.12 | 112.38 | 65.84 | 0.59x | 55 | 72 |

  The first after-run measured p99 23.1 ms at 100 rps (4.3x, event-loop lag p99 21.9 ms): the auth gate was `@app.middleware("http")` (BaseHTTPMiddleware). Fixed by rewriting it as plain ASGI (`authz.AuthGuard`), which also has to turn an `AuthUnavailable` raised by a handler past the gate into the 503 (`test_an_unopenable_store_fails_closed` caught that). Per-request store cost measured in-process: authenticate 15 us, check_and_reserve 9 us, settle 37 us.
- tests/load now authenticates: gateway_app.py creates the first admin through `POST /api/setup/admin` in-process and writes `{authorization, cookie}` to `--creds-out` (0600, fresh temp DERATE_DATA_DIR unless set); harness.py reads it into `GATEWAY_HEADERS` (httpx clients, driver jobs); driver.py writes `job["headers"]` into its pre-encoded request; probes.py's hammer client carries them; loadtest.py gains `--admin-user/--admin-password` for its /api policy pin.

## W5 walkthrough (clean :18121, 2026-11-17)

1. `POST /api/setup/admin` → `200 ['admin', 'key', 'org', 'secret_once']`
2. `GET /v1/models` no key → `401 missing_api_key`
3. with key → `200`
4. key `five` (`requests_cycle=5`), 4 requests → `200 200 200 200`; row `{'used': 4.0, 'effective_limit': 5.0, 'fraction': 0.8, 'near': True, 'at': False}`; `/keys` shows "Near limit: requests_cycle 80%" (`/tmp/derate-clean/ui/screens/keys.png`)
5. 5th → `200`, 6th → `429 limit_exceeded limit_source=key:requests_cycle used 5 limit 5`, headers `x-ratelimit-limit: 5`, `-remaining: 0`, `-reset: 1797559328`
6. `POST /raise {requests_cycle,10}` → `200`, then request `200`
7. `POST /reset-cycle` → `200`, `used= [0.0]`
8. `POST /disable` → `200`; `/v1` → `401 api_key_disabled`
9. 1-day key, `sqlite3 keys.db "UPDATE api_keys SET expires_at=strftime('%s','now')-60 ..."` → `401 api_key_expired`; `GET /api/keys/zegvbka9yk` → `expired {'requests': 1, 'tokens': 34}`

Bug found by step 9 and fixed: 1h/1d rollups are written only once the bucket closes, so `usage(bucket='1d')` omitted today (a new key read 0 until midnight UTC). `query.usage` now folds the open coarse bucket from 1m rollups; regression `test_usage_counts_the_open_day_and_hour` fails before / passes after. Also `cache_read_tokens` now populated (16) on local rows after adding `--enable-prompt-tokens-details`.
