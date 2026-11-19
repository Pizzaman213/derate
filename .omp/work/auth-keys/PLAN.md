# auth-keys PLAN (worker reference copy; the orchestrator's approved plan, verbatim)

Ownership map + per-unit fast gates: see PROGRESS.md § Ownership map / Units. Workers NEVER edit SHARED files (contracts, generated docs, requirements/pyproject, ui/package.json, CLAUDE/TODO/README, ui/src/state/routes.ts, ui/src/state/router.tsx, ui/src/shell/**, ui/src/api/client.ts, ui/src/api/types.ts, ui/src/api/redact.ts, control_plane/redaction.py, control_plane/gateway/app.py, control_plane/gateway/main.py, control_plane/telemetry/config.py, .omp/work/auth-keys/**) — return exact diffs as requests.

# auth-keys — inbound auth, admins, orgs, API keys, limits, per-key telemetry + stats UI

## Context

derate's gateway has no inbound authentication (`csrf.py` docstring: "the /api surface has no authentication"; `proxy.py:604-612` re-adds the client's own `Authorization` onto LOCAL backends). This plan closes TODO.md "Inbound authentication on the gateway" end to end: admin accounts + sessions, orgs, `drt_` API keys with per-key/org limits, per-request identity + routing/cache/spec-decode telemetry with new rollup dimensions, a `/api/usage` stats API, and `/login`, `/keys`, `/orgs`, `/audit` screens. Work is run as an orchestrator with worker subagents in waves W0–W5, a lock, a ledger, and named sets; **nothing is "done" without a verbatim gate line**.

Box facts observed this session (2026-11-17): `:8088` is **down**; `:8080/:8081` are someone else's; `18088` (pid 1066072, `DERATE_DATA_DIR=/tmp/derate-pack`) is another session's coordinator — never touch it. Free port choice for this run: **18120** (W0) and **18121** (W5 clean). `argon2-cffi` is **not installed** (`ModuleNotFoundError`), PyPI is reachable (`pip index versions argon2-cffi` → 25.1.0). Python 3.12.3, sqlite 3.45.1, node 20.20.2, Playwright chromium present. `git status`: only `.omp/tests/ui/run.mjs` dirty (a design-pack peer is active under `.omp/charts/` — mtimes < 30 min). `.omp/work/` does not exist → no lock.

## Fixed decisions (apply everywhere; do not re-decide)

- **Chat via session = each admin owns a personal admin key.** Creating an admin mints an `ApiKey(is_admin_key=True, admin_username=<u>, org_id=<default org>, expires_at=None, name=<username>)` whose secret is random and never revealed (the admin can `rotate` it later to obtain a usable secret with the normal one-time reveal). `POST /api/chat/completions|speech|transcriptions` (session-gated) run the same `_serve` path with `Principal(key=<admin key>, via="session")`. Why: telemetry rows then carry a real `key_id`, `/keys` shows admin usage, no second credential type, no key ever reaches the browser. Record this in the ledger notes of U2.gateway.
- **Storage:** three SQLite files under `paths.data_dir()`: `auth.db` (admins, sessions, login_attempts, audit), `keys.db` (orgs, api_keys), `limits.db` (cycle_counters, overrides, cycle_resets, local_pricing). Same connect pattern as `inventory/db.py:177-200` (`timeout=30, isolation_level=None, check_same_thread=False`, `journal_mode=WAL`, `busy_timeout=30000`, `row_factory=sqlite3.Row`, `meta.schema_version`, an `RLock` around every access). Opened lazily on first use; an unopenable store makes every gated route answer **503 `auth_unavailable`** (fail closed). Register all three in `registry/storage.py::ESTATE`.
- **Services are built inside `create_app`** (like `ctx.inventory`): `ctx.auth = AuthService.open(data_path("auth.db"))`, `ctx.keys = KeyService.open(data_path("keys.db"))`, `ctx.pricing = PricingStore.open(data_path("limits.db"))`, `ctx.limits = LimitService.open(data_path("limits.db"), keys=ctx.keys, pricing=ctx.pricing)`. No `GatewayDeps` port, no stub, no toggle: the stub app `routes.py` builds is secured the same way.
- **Key format:** `drt_<key_id>_<secret>`; `key_id` = 10 chars `[a-z0-9]` via `secrets.choice`; `secret` = `secrets.token_urlsafe(32)`. Parse with `s.removeprefix("drt_").split("_", 1)`. Store `sha256(secret).hexdigest()`; verify with `hmac.compare_digest`. Lookup by `key_id`.
- **Session cookie** `derate_session`: value `secrets.token_urlsafe(32)`, stored hashed (sha256) in `sessions`; `HttpOnly; SameSite=Strict; Path=/`; `Secure` iff `request.url.scheme == "https"`; idle TTL 7 d, absolute 30 d. CSRF: keep `csrf.py` unchanged (Origin check + SameSite=Strict).
- **Open routes (no session, by design, forever):** `GET /healthz`, `GET /install.sh`, `POST /api/nodes/join` (worker join; body token — `registry/bootstrap.py:80`), `GET /api/auth/state`, `POST /api/auth/login`. **Setup-only route (closes permanently once an admin exists → 410 `setup_closed`):** `POST /api/setup/admin`. Everything else under `/api` (incl. `/api/docs`, `/api/openapi.json`, `/api/setup`, `/api/setup/complete`, `/api/shell/status`, the shell websocket) needs a session; everything under `/v1` (incl. `WS /v1/realtime`, multipart `/v1/audio/transcriptions`) needs a key.
- **401 codes on /v1 (distinct):** `missing_api_key`, `invalid_api_key` (unknown or soft-deleted), `api_key_disabled`, `api_key_expired`; header `WWW-Authenticate: Bearer realm="derate"`. Key is read only from `Authorization: Bearer drt_…` (header also for the websocket; no query-string key, it would land in access logs). `/api` without session → 401 `session_required`.
- **Client IP** = `request.client.host` (no `X-Forwarded-For` trust).
- **Limits:** key value ≤ org value, validated server-side with 400 `limit_exceeds_org` whose message names the field. `limit_source` string = `"<scope>:<limit_type>"`, e.g. `key:tokens_total_cycle`, `org:model:gpt-4o:rpm`. 429 body `{"error":{"message","type":"rate_limit_error","code":"limit_exceeded","limit_source","used","limit","resets_at"}}` + `X-RateLimit-Limit/-Remaining/-Reset` (Reset = epoch seconds; for rpm/tpm the window end; for `max_concurrent` = now). Per-request caps: `max_output_tokens_per_request` → 400 `max_output_tokens_exceeded` (message names the cap) or injected into the body as `max_tokens` when absent; `max_prompt_tokens_per_request` → 413 `max_prompt_tokens_exceeded`, estimate via `AdmissionController.estimate_prompt_tokens(body)` (`admission.py:221`).
- **Cycle:** `n = floor((now - anchor)/(30*86400))`, `cycle_start = anchor + n*30d`, `resets_at = cycle_start + 30d`, anchor = key/org `created_at` (UTC epoch floats). Reset = delete this cycle's counter rows (schedule unchanged). Raise = `overrides(scope, scope_id, limit_type, cycle_start, value)` row; effective limit = override if present for the current cycle else configured; counters untouched.
- **Cloud fallback:** key field `cloud_fallback_local_model: str|None` (must be an allowed model served by a LOCAL target). When set and the blocking limit_source is `*:cloud_tokens_cycle`/`*:cloud_usd_cycle`/`*:budget_usd_cycle` for a request whose `model_requested` is remote-served, re-dispatch to that model; telemetry `fallback_used=True, fallback_reason="cloud_pool_exhausted"`, `model_served=<local>`.
- **Allowlist:** `allowed_models: list[str]|None`; `None` = "what `/v1/models` exposes right now" (org) / "inherit org" (key). Key list must be ⊆ effective org list → else 400 `allowlist_exceeds_org`.
- **Routing policy override:** `routing_policy: RoutingPolicy|None` on org and key (key wins). Applied by adding `policy: RoutingPolicy|None = None` to `Router.select(served_name, prefix_key=None, exclude=None)` (`router.py:373`) which substitutes `config.policy` the same way `exclude` rebuilds `RoutingConfig`.
- **Expiry in the walkthrough** is forced by editing the stored `expires_at` with the `sqlite3` CLI (no clock knob ships): `sqlite3 <data>/keys.db "UPDATE api_keys SET expires_at=strftime('%s','now')-60 WHERE key_id='<id>'"`.
- **One-time reveal** comes back as `secret_once` on exactly three responses: `POST /api/setup/admin`, `POST /api/keys`, `POST /api/keys/{key_id}/rotate`. `client.ts` fetches those three through `reqUnscrubbed()`; everything else goes through `req()` which scrubs. `redaction.py` and `redact.ts` learn `\bdrt_[a-z0-9]{10}_[A-Za-z0-9_\-]{32,}` and scrub it by value everywhere else.
- **Telemetry rollup migration:** `archive.py` `SCHEMA_VERSION` 1→2: `ALTER TABLE requests ADD COLUMN …` for each new column; `DROP TABLE rollup_requests` and create the new one with the wider PK; set `meta.reroll_pending=1` so the next `retention.compact()` re-rolls the whole raw window once. Pre-migration hourly rollups older than raw retention are lost (this box has days, not months, of history; recorded in the ledger).
- **Tests keep their 150 `TestClient(create_app(...))` call sites.** New `tests/unit/conftest.py` (owned by U2.gateway) adds two autouse fixtures: (1) if `DERATE_DATA_DIR` is unset, `monkeypatch.setenv` it to `tmp_path/"data"`; (2) patch `fastapi.testclient.TestClient.request` so a request to a gated `/api` or `/v1` path with no `Cookie`/`Authorization` first bootstraps once per client: `POST /api/setup/admin {"username":"test-admin","password":"test-admin-pw-123456"}` (or `POST /api/auth/login` if 410) and keeps the cookie + the returned default key; `/v1` requests get `Authorization: Bearer <default key>`. Tests that assert 401/410 use `@pytest.mark.raw_http` to opt out. The fixture's docstring says this is the real auth path, exercised on every request, not a bypass.
- **Workers never touch SHARED files** (list in the prompt: contracts, generated docs, requirements/pyproject, `ui/package.json`, CLAUDE/TODO/README, `routes.ts`, `router.tsx`, `ui/src/shell/**`, `client.ts`, `types.ts`, `redact.ts`, `redaction.py`, `gateway/app.py`, `gateway/main.py`, `telemetry/config.py`, `.omp/work/auth-keys/**`). They return exact diffs as requests; the orchestrator applies them.

## W0 — orchestrator, sequential

### U0.1 bootstrap

1. `mkdir -p .omp/work/auth-keys/sets`; add the line `.omp/work/auth-keys/LOCK` under the "Design pack" block of `.gitignore` (:25-32; shared file, orchestrator) — the ledger and sets stay tracked, only the lock is machine-local. Write `LOCK` = JSON `{session_id, pid, host, started_at, heartbeat_at, current_unit}`; refresh `heartbeat_at` at every wave boundary and ≤10 min.
2. Write `PROGRESS.md` with the unit table (id, depends_on, status, owner, owned paths, gate command, gate last line, timestamp, notes) for every unit below, all `todo`, plus an "Ownership map" section per wave and a "Conflicts" section. Resume rule: if it already exists, skip `done` units, rerun the last done unit's gate, continue.
3. Boot the W0 coordinator: `DERATE_DATA_DIR=/tmp/derate-authkeys DERATE_LOG_DIR=/tmp/derate-authkeys/logs DERATE_PORT=18120 DERATE_UI_DIR=/home/connor/derate/ui/dist DERATE_ROLE=coordinator python3 -m control_plane.node` as a named service (`ready.port: 18120`). Restart rule for later waves: `kill PID`, wait for the port, then `kill -9`; never `pkill -f`.
4. Baseline gates, recorded verbatim in the ledger as-is (pre-existing failures logged, not fixed): `python3 -m pytest -q -m "not slow"`; `cd ui && npm run typecheck && npm run build && DERATE_CHECK_ORIGIN=http://localhost:18120 npm run check -- --strict`. Load baseline: `python3 -m tests.load rps --duration 10 --json .omp/work/auth-keys/load-baseline.json` (note the p99 columns).
5. Write the sets (each file: `{"derived_by": "<command>", "members": [...]}`):
   - `sets/limit_types.json` — hand-listed from the prompt: the 15 fixed names + the four templates `model:<id>:tokens_cycle`, `model:<id>:usd_cycle`, `model:<id>:rpm`, `model:<id>:tpm`. After U0.3 its derive command becomes `python3 -c "from control_plane.contracts.auth import LIMIT_TYPES; import json; print(json.dumps(sorted(LIMIT_TYPES)))"` and the file is regenerated from it.
   - `sets/setup_routes.json` — hand-written with two arrays: `before_first_admin: ["POST /api/setup/admin"]`, `always_open: ["GET /healthz","GET /install.sh","POST /api/nodes/join","GET /api/auth/state","POST /api/auth/login"]`, each member with a one-line `why`.
   - `sets/v1_routes.json`, `sets/api_routes.json`, `sets/telemetry_fields.json`, `sets/http_test_files.json` are written in U0.3 (they need the regenerated routes.json / contracts). `http_test_files.json` derive: `grep -l "TestClient(" tests/unit/*.py`.
6. Peer check before every wave: `git status --short` + `find <paths about to be owned> -mmin -15 -type f` — a hit in a path you did not touch = live peer → stop and report. Known live peer today: `.omp/charts/**`, `.omp/tests/ui/run.mjs`.

Gate line: `python3 -m pytest -q -m "not slow"` → last line verbatim.

### U0.2 PLAN.md

Write `.omp/work/auth-keys/PLAN.md` = this document's "Fixed decisions", "Data model", the U0.3 contract surface, and an "Ownership map" table (unit → owned paths → readable paths → fast gate) for every W1–W4 unit as listed below, so workers can be pointed at a repo path (workers read `.omp/work/auth-keys/PLAN.md` and the READMEs listed per unit). No gate beyond the file existing.

### U0.3 contracts change + regenerate

Create **`control_plane/contracts/auth.py`** (auto-reflected by `manifest.py::_contract_modules()`; `from __future__ import annotations`; re-export names in `contracts/__init__.py::__all__`):

```python
KEY_PREFIX = "drt_"; KEY_ID_LEN = 10; KEY_SECRET_BYTES = 32
KEY_EXPIRY_DAYS = (1, 10, 20, 30, 50, 100, 365)
CYCLE_DAYS = 30
SESSION_COOKIE = "derate_session"; SESSION_IDLE_S = 7 * 86400; SESSION_MAX_S = 30 * 86400
LOGIN_LOCK_THRESHOLD = 5; LOGIN_LOCK_WINDOW_S = 900; LOGIN_LOCK_BASE_S = 30; LOGIN_LOCK_MAX_S = 3600
NEAR_LIMIT_FRACTION = 0.8
PASSWORD_MIN_LEN = 12
LIMIT_TYPES = ("tokens_total_cycle","tokens_input_cycle","tokens_output_cycle","requests_cycle",
  "budget_usd_cycle","local_tokens_cycle","local_usd_cycle","cloud_tokens_cycle","cloud_usd_cycle",
  "rpm","tpm","max_concurrent","max_output_tokens_per_request","max_prompt_tokens_per_request",
  "model:<id>:tokens_cycle","model:<id>:usd_cycle","model:<id>:rpm","model:<id>:tpm")
MODEL_LIMIT_SUFFIXES = ("tokens_cycle","usd_cycle","rpm","tpm")   # "model:<served_name>:<suffix>"

class KeyStatus(str, Enum): ACTIVE="active"; DISABLED="disabled"; EXPIRED="expired"
class LimitScope(str, Enum): KEY="key"; ORG="org"
class PrincipalVia(str, Enum): API_KEY="api_key"; SESSION="session"
class AuthErrorCode(str, Enum):
    MISSING_API_KEY="missing_api_key"; INVALID_API_KEY="invalid_api_key"; API_KEY_DISABLED="api_key_disabled"
    API_KEY_EXPIRED="api_key_expired"; SESSION_REQUIRED="session_required"; INVALID_CREDENTIALS="invalid_credentials"
    LOGIN_LOCKED="login_locked"; SETUP_CLOSED="setup_closed"; AUTH_UNAVAILABLE="auth_unavailable"
    LIMIT_EXCEEDED="limit_exceeded"; MAX_OUTPUT_TOKENS_EXCEEDED="max_output_tokens_exceeded"
    MAX_PROMPT_TOKENS_EXCEEDED="max_prompt_tokens_exceeded"; LIMIT_EXCEEDS_ORG="limit_exceeds_org"
    ALLOWLIST_EXCEEDS_ORG="allowlist_exceeds_org"; MODEL_NOT_ALLOWED="model_not_allowed"
class AuditAction(str, Enum):  # one per admin action in the prompt
    LOGIN="login"; LOGIN_FAILED="login_failed"; LOGOUT="logout"; PASSWORD_CHANGED="password_changed"
    ADMIN_ADDED="admin_added"; ADMIN_REMOVED="admin_removed"; SETUP_ADMIN_CREATED="setup_admin_created"
    ORG_CREATED="org_created"; ORG_UPDATED="org_updated"; ORG_DELETED="org_deleted"
    KEY_CREATED="key_created"; KEY_UPDATED="key_updated"; KEY_DISABLED="key_disabled"; KEY_ENABLED="key_enabled"
    KEY_ROTATED="key_rotated"; KEY_DELETED="key_deleted"
    LIMIT_CHANGED="limit_changed"; LIMIT_RAISED="limit_raised"; CYCLE_RESET="cycle_reset"; PRICING_CHANGED="pricing_changed"

@dataclass(frozen=True) class Admin: username: str; created_at: float; created_by: str | None
@dataclass(frozen=True) class Session: session_id: str; username: str; created_at: float; last_seen_at: float; expires_at: float; ip: str | None
@dataclass(frozen=True) class Org:
    org_id: str; name: str; created_at: float; created_by: str
    allowed_models: list[str] | None = None; limits: dict[str, float] = field(default_factory=dict)
    default_key_limits: dict[str, float] = field(default_factory=dict); default_key_expiry_days: int | None = None
    routing_policy: RoutingPolicy | None = None; deleted_at: float | None = None
@dataclass(frozen=True) class ApiKey:
    key_id: str; name: str; org_id: str; created_by: str; created_at: float; expires_at: float | None; status: KeyStatus
    disabled_at: float | None = None; disabled_by: str | None = None; disabled_reason: str | None = None
    last_used_at: float | None = None; last_used_ip: str | None = None; notes: str = ""
    is_admin_key: bool = False; admin_username: str | None = None
    allowed_models: list[str] | None = None; routing_policy: RoutingPolicy | None = None
    limits: dict[str, float] = field(default_factory=dict); cloud_fallback_local_model: str | None = None
    deleted_at: float | None = None
@dataclass(frozen=True) class MintedKey: key: ApiKey; secret_once: str     # the one exception; never logged
@dataclass(frozen=True) class Principal: key: ApiKey; org: Org; via: PrincipalVia; ip: str | None; user_agent: str | None
@dataclass(frozen=True) class LimitState:
    limit_type: str; scope: LimitScope; scope_id: str; limit: float; effective_limit: float
    used: float; remaining: float; resets_at: float | None; fraction: float; near: bool; at: bool
@dataclass(frozen=True) class LimitDecision:
    allowed: bool; status: int = 200; code: str = ""; message: str = ""; limit_source: str | None = None
    used: float | None = None; limit: float | None = None; resets_at: float | None = None
    inject_max_tokens: int | None = None; fallback_model: str | None = None
@dataclass(frozen=True) class UsageActuals:   # what settle() reports back
    prompt_tokens: int; completion_tokens: int; cost_usd: float | None; cost_basis: CostBasis; target_kind: str; model_served: str
@dataclass(frozen=True) class AuditEntry:
    id: int; ts: float; actor: str; action: AuditAction; target_kind: str; target_id: str
    before: dict | None; after: dict | None; ip: str | None
@dataclass(frozen=True) class LocalPrice: model: str; input_usd_per_mtok: float | None; output_usd_per_mtok: float | None; set_by: str; set_at: float
```

Protocols (also in `contracts/auth.py`; every method synchronous; stores are SQLite + RLock, called via `asyncio.to_thread` from routes only where noted):

```python
class AuthPort(Protocol):
    def admin_exists(self) -> bool
    def create_admin(self, username, password, *, created_by: str | None, ip: str | None) -> Admin      # argon2id; raises ValueError on weak pw/duplicate
    def list_admins(self) -> list[Admin]
    def remove_admin(self, username, *, actor, ip) -> None                                              # ValueError if it would leave zero admins
    def change_password(self, username, old_password, new_password, *, actor, ip) -> None
    def login(self, username, password, *, ip) -> Session                                               # raises LoginLocked(retry_after_s) / InvalidCredentials; always runs the hash
    def session(self, cookie_value: str, *, now: float | None = None) -> Session | None                 # touches last_seen_at; None when missing/expired
    def logout(self, cookie_value: str, *, ip) -> None
    def audit(self, actor, action: AuditAction, target_kind, target_id, *, before=None, after=None, ip=None) -> None
    def audit_page(self, *, start=None, end=None, actor="", action="", target="", page=1, page_size=100) -> tuple[list[AuditEntry], int]
class KeyPort(Protocol):
    def create_org(self, name, *, actor, **fields) -> Org
    def get_org(self, org_id) -> Org | None;  def list_orgs(self, *, include_deleted=False) -> list[Org]
    def update_org(self, org_id, *, actor, **fields) -> Org;  def delete_org(self, org_id, *, actor) -> None   # soft
    def default_org_id(self) -> str
    def create_key(self, *, name, org_id, created_by, expiry_days: int | None, limits=None, allowed_models=None,
                   routing_policy=None, notes="", is_admin_key=False, admin_username=None, cloud_fallback_local_model=None) -> MintedKey
    def get_key(self, key_id) -> ApiKey | None;  def list_keys(self, *, org_id=None, include_deleted=False) -> list[ApiKey]
    def authenticate(self, raw_key: str, *, ip: str | None, now: float | None = None) -> ApiKey   # raises AuthError(code) with the four codes; updates last_used_*
    def update_key(self, key_id, *, actor, **fields) -> ApiKey
    def disable_key(self, key_id, *, actor, reason=None) -> ApiKey;  def enable_key(self, key_id, *, actor) -> ApiKey
    def rotate_key(self, key_id, *, actor) -> MintedKey;  def delete_key(self, key_id, *, actor) -> None   # soft
    def sweep_expired(self, *, now=None) -> list[str]            # marks status=expired; called by a 60 s task
    def effective_allowlist(self, key: ApiKey, org: Org, exposed: list[str]) -> list[str]
    def validate_against_org(self, *, org: Org, limits: dict, allowed_models, exposed: list[str]) -> None   # raises LimitExceedsOrg(field) / AllowlistExceedsOrg
class PricingPort(Protocol):
    def get(self, model) -> LocalPrice | None;  def list(self) -> list[LocalPrice]
    def set(self, model, input_usd_per_mtok, output_usd_per_mtok, *, actor) -> LocalPrice;  def clear(self, model, *, actor) -> None
    def cost(self, *, model, target_kind, prompt_tokens, completion_tokens, metered_usd: float | None,
             provider_in_per_mtok: float | None, provider_out_per_mtok: float | None) -> tuple[float | None, CostBasis]
class LimitPort(Protocol):
    def check_and_reserve(self, principal: Principal, *, model: str, target_kind: str | None, body: dict, now=None) -> tuple[LimitDecision, Reservation | None]
    def settle(self, reservation: Reservation, actuals: UsageActuals | None, *, now=None) -> None     # None actuals = release only
    def states(self, scope: LimitScope, scope_id: str, *, now=None) -> list[LimitState]
    def raise_for_cycle(self, scope, scope_id, limit_type, value, *, actor, now=None) -> LimitState
    def reset_cycle(self, scope, scope_id, *, actor, now=None) -> None
    def cycle_bounds(self, anchor: float, now: float) -> tuple[float, float]   # (cycle_start, resets_at)
```

`Reservation` is an opaque dataclass `(reservation_id, key_id, org_id, model, started_at, estimated_prompt, estimated_output, slots)`. Exceptions `AuthError(code: AuthErrorCode, message, retry_after_s=None)` live in `contracts/auth.py` too.

Create **`control_plane/contracts/telemetry.py`**:

```python
class ContextBucket(str, Enum): B0_8K="0-8k"; B8_32K="8-32k"; B32_128K="32-128k"; B128K_PLUS="128k+"
class CostBasis(str, Enum): METERED="metered"; ESTIMATED="estimated"; UNPRICED="unpriced"
class FieldError(str, Enum): NOT_REPORTED="not_reported"; AMBIGUOUS_CONCURRENCY="ambiguous_concurrency"; NOT_ENABLED="not_enabled"
class FallbackReason(str, Enum): CLOUD_POOL_EXHAUSTED="cloud_pool_exhausted"; UPSTREAM_FAILED="upstream_failed"
REQUEST_RECORD_FIELDS = (<the 29 existing names in order>, then:
  "key_id","key_name","org_id","client_ip","user_agent",
  "model_requested","model_served","attempt_chain","fallback_used","fallback_reason",
  "finish_reason","cancelled","limit_source",
  "generation_ms","tokens_per_s","tpot_mean_ms","tpot_p95_ms","tail_ms",
  "reasoning_tokens","cache_read_tokens","cache_write_tokens","max_tokens_requested","message_count","context_bucket",
  "prefix_cache_hit_rate","cache_error",
  "spec_num_drafts","spec_draft_tokens","spec_accepted_tokens","spec_acceptance_rate","spec_mean_accepted_len","spec_accepted_per_position","spec_decode_error",
  "cost_basis")
USAGE_BUCKETS = ("1m","1h","1d"); USAGE_GROUP_BY = ("key_id","org_id","model","node","provider","status","context_bucket")
```

Existing `policy` field is the "route policy"; no second field. Add a `DerivedFact("request_record_fields", canonical="control_plane.contracts.telemetry:REQUEST_RECORD_FIELDS", copies=("control_plane.telemetry.records:REQUEST_RECORD_FIELDS",), compare="members")` to `derived.py` — U1.telem adds `REQUEST_RECORD_FIELDS = tuple(f.name for f in fields(RequestRecord))` in `records.py`; until then the copy is missing and `test_single_source` fails: **acceptable only inside W1; the W1 gate requires it green.**

Routes: fix `contracts/routes.py::_walk` so `type(route).__name__ in ("WebSocketRoute","APIWebSocketRoute")` yields `{"methods":["WEBSOCKET"],"path":…}` (today `APIWebSocketRoute` has no `.methods` and is dropped, so `/v1/realtime` is missing from routes.json). Add in `document.py` nothing (sections auto-pick enums/dataclasses/constants/protocols).

Env vars (`envspec.py` `_v(...)` rows): `DERATE_CHECK_ADMIN_USER` (default `""`, owner `ui/src/check/auth.mjs`), `DERATE_CHECK_ADMIN_PASSWORD` (`""`), `DERATE_CHECK_API_KEY` (`""`). No server-side env var is added (no toggles).

`requirements.txt` + `pyproject.toml` `dependencies`: add `argon2-cffi==25.1.0` (both lists, same pin). Then `python3 -m pip install argon2-cffi==25.1.0` (the one install, orchestrator).

Regenerate: `python3 -m control_plane.contracts.manifest --write && python3 -m control_plane.contracts.routes --write && python3 -m control_plane.contracts.document --write`. Then write `sets/v1_routes.json` / `sets/api_routes.json` from `control_plane/contracts/routes.json` (`gateway` entries whose path starts `/v1` / `/api`; command recorded) — these are regenerated again after U2.gateway adds routes, and the coverage tests read the sets files, so the gate always checks the current full set. `sets/telemetry_fields.json` derive: `python3 -c "from control_plane.contracts.telemetry import REQUEST_RECORD_FIELDS as F; import json; print(json.dumps(list(F)))"`.

Gate: `python3 -m pytest -q tests/unit/test_contracts_manifest.py tests/unit/test_single_source.py` (the `request_record_fields` copy row is added at the end of W1, so U0.3's gate is run with that fact row commented out and the row is added in the W1 gate; note it in the ledger).

## Data model (per store; all `created_at`/`*_at` REAL epoch UTC; JSON columns TEXT)

**auth.db** — `admins(username PK, password_hash, created_at, created_by)`; `sessions(sid_hash PK, username, created_at, last_seen_at, expires_at, ip)`; `login_attempts(id PK, ts, username, ip, ok INTEGER)` (lockout reads the last 15 min: ≥5 failures for the username **or** the ip → locked for `min(3600, 30 * 2**(failures-5))` s, 429 `login_locked` with `retry_after_s`); `audit(id PK AUTOINCREMENT, ts, actor, action, target_kind, target_id, before_json, after_json, ip)` — no delete path exists.

**keys.db** — `orgs(org_id PK, name, created_at, created_by, allowed_models_json, limits_json, default_key_limits_json, default_key_expiry_days, routing_policy, deleted_at)`; `api_keys(key_id PK, secret_hash, name, org_id, created_by, created_at, expires_at, status, disabled_at, disabled_by, disabled_reason, last_used_at, last_used_ip, notes, is_admin_key INTEGER, admin_username, allowed_models_json, routing_policy, limits_json, cloud_fallback_local_model, deleted_at)`; index on `(org_id)`, `(admin_username)`.

**limits.db** — `cycle_counters(scope, scope_id, cycle_start, metric, value REAL, PK(scope,scope_id,cycle_start,metric))` with metrics `tokens_total, tokens_input, tokens_output, requests, usd, local_tokens, local_usd, cloud_tokens, cloud_usd, model:<id>:tokens, model:<id>:usd, unpriced_requests`; `overrides(scope, scope_id, limit_type, cycle_start, value, actor, created_at, PK(scope,scope_id,limit_type,cycle_start))`; `cycle_resets(scope, scope_id, cycle_start, reset_at, actor)` (audit trail only); `local_pricing(model PK, input_usd_per_mtok, output_usd_per_mtok, set_by, set_at)`. In-memory: rpm/tpm sliding 60 s deques per `(scope, scope_id[, model])`, `max_concurrent` counters, reservations dict. Counting at check = persisted + Σ live reservations. Settle does one `UPDATE … value = value + ?` per metric inside one transaction under the RLock.

## W1 — four workers in parallel (depends_on U0.3). Each codes against the Protocols with in-memory fakes of the others; fast gate `python3 -m pytest -q <own tests>`

### U1.auth — owns `control_plane/auth/**`, `tests/unit/test_auth*.py`
`auth/store.py` (SQLite as above), `auth/service.py::AuthService.open(path, *, clock=time.time) -> AuthService` implementing `AuthPort`; argon2id via `argon2.PasswordHasher()` (defaults), `verify` + `check_needs_rehash`; `login` always calls `ph.verify` against a fixed dummy hash when the user is unknown; `PASSWORD_MIN_LEN` enforced; `remove_admin` refuses the last admin; `audit()` appends. `auth/README.md` (Layout table). Tests: lockout per username and per ip with backoff, session idle/absolute expiry, last-admin refusal, audit rows written for every `AuditAction` the service emits, hash never equals password, `admin_exists` flips once.

### U1.keys — owns `control_plane/keys/**`, `tests/unit/test_keys*.py`
`keys/store.py`, `keys/service.py::KeyService.open(path, *, clock=time.time)` implementing `KeyPort`; `keys/format.py` (`mint() -> (key_id, secret, raw)`, `parse(raw) -> (key_id, secret) | None`, `hash_secret`, `verify`) — pure, importable by tests; `create_org("default", …)` on first open when no org exists (`default_org_id()`); `expiry_days` must be in `KEY_EXPIRY_DAYS` or `None` only when `is_admin_key` → else `ValueError`; `authenticate` order: parse fail → `invalid_api_key`; unknown/deleted → `invalid_api_key`; hash mismatch → `invalid_api_key`; `status=disabled` → `api_key_disabled`; `expires_at < now` → `api_key_expired` (and flips status); success updates `last_used_at/ip`. `rotate_key` replaces `secret_hash` only. `README.md`. Tests: every expiry option round-trips, 2 days is refused, soft delete keeps the row and stats-relevant fields, rotate kills the old secret immediately, narrowing validation names the field, admin key never expires.

### U1.limits — owns `control_plane/limits/**`, `tests/unit/test_limits*.py`
`limits/cycle.py` (`cycle_bounds`), `limits/types.py` (`parse_limit_type(name) -> (base, model|None)`, validates against `LIMIT_TYPES`/`MODEL_LIMIT_SUFFIXES`), `limits/pricing.py::PricingStore.open(path)` implementing `PricingPort` (`cost()`: metered → (metered, METERED); local with price row having both rates → estimate; provider rates → estimate; else (None, UNPRICED)), `limits/service.py::LimitService.open(path, *, keys: KeyPort, pricing: PricingPort, clock=time.time)` implementing `LimitPort`; `limits/README.md`. `check_and_reserve` order: allowlist (`model_not_allowed` 403) → per-request caps (400/413) → in-memory `max_concurrent`, `rpm`, `tpm` (+ model variants) for key then org → cycle counters for key then org (estimated prompt via `body` char/4, estimated output = `max_tokens` or 256) → on block compute `fallback_model` per the cloud-fallback rule. `states()` returns one `LimitState` per configured limit with `near = fraction >= NEAR_LIMIT_FRACTION`, `at = fraction >= 1`. Tests must iterate **`sets/limit_types.json`** (read the file) proving each type can be set, blocks at its threshold, and reports `resets_at`; plus: counters survive `LimitService.open` twice on the same file; 32 threads reserving concurrently never oversubscribe `max_concurrent`; a stream that crosses mid-flight settles and the next request blocks; raise-for-cycle does not reset the counter and expires at cycle end; reset zeroes only the current cycle; unpriced requests do not count toward `*_usd_*` and are counted in `unpriced_requests`.

### U1.telem — owns `control_plane/telemetry/**` except `config.py`, new `control_plane/telemetry/engine_metrics.py`, `tests/unit/test_telemetry_keys*.py`
- `records.py`: add every field in `REQUEST_RECORD_FIELDS` to `RequestRecord` (defaults `None`/`False`/`""` as typed; `attempt_chain: list[dict] | None`, `spec_accepted_per_position: list[int] | None`), add `REQUEST_RECORD_FIELDS` constant, extend `RequestTrace` with `principal` fields (`key_id, key_name, org_id, client_ip, user_agent, model_requested, max_tokens_requested, message_count`) and `record(...)` kwargs for the rest; `context_bucket` computed from `prompt_tokens` in `record()`; `prefix_cache_hit_rate = cache_read_tokens / prompt_tokens` when both known else `None` + `cache_error`.
- `archive.py`: schema v2 migration (ALTER ADD COLUMN for each new column; `attempt_chain`/`spec_accepted_per_position` as JSON TEXT), new `rollup_requests` PK `(step, bucket, served_name, target_id, node_id, provider_id, key_id, org_id, context_bucket, status)` with measures `n, ok, failed, cancelled, fallback, tokens, prompt_tokens, completion_tokens, reasoning_tokens, cache_read_tokens, cache_write_tokens, cost_usd, metered_usd, estimated_usd, unpriced_n, errors_json (error_class→count), spec_drafts, spec_draft_tokens, spec_accepted_tokens, spec_per_position_json, spec_error_json, cache_error_json, ttft_hist, duration_hist, queue_hist (parked_ms), tpot_hist, tps_hist`; `meta.reroll_pending`.
- `retention.py`: `STEP_1D=86400`, `_roll_requests` for 1m from raw, 1h from 1m, 1d from 1h (hists merged via `hist.merged`, never averaged); honour `reroll_pending`; expire 1d at `ROLLUP_1D_RETENTION_S`.
- `query.py`: `STEP_NAMES["1d"]=86400`; new `usage(archive, *, start, end, bucket, group_by: list[str], key_ids, org_ids, models) -> dict` (per bucket+group: requests, errors by class, error/fallback/cancel rates, tokens in/out/reasoning/cache_read/cache_write, cache hit rate = Σcache_read/Σprompt, spec acceptance overall + per position, avg/p50/p95/p99 of ttft, latency(duration), queue, tpot, tokens/s via `Hist.pct`, avg prompt/output tokens, cost + basis + unpriced count; missing buckets absent, never zero-filled); `key_requests(archive, key_id, *, start, end, status, model, error, page, page_size)`; `usage_csv(rows)` / `requests_csv(rows)` writers. Existing `requests()` keeps its shape by summing across the new dimensions.
- `engine_metrics.py::EngineMetricsPoller(http, deployments_fn, interval_s=1.0)`: polls `<backend_url>/metrics` of every READY local text deployment; **first read the real metric names from the pinned image** before writing the parser: `docker run --rm --entrypoint sh ghcr.io/pizzaman213/derate/vllm-audio:latest -c "grep -rhoE 'vllm:spec_decode[a-z_]+|vllm:prefix_cache_[a-z_]+|cached_tokens' /usr/local/lib/python3*/dist-packages/vllm | sort -u"` (sglang image is not on this box → `not_reported` for sglang and the command recorded as blocked); a deployment whose scrape has no spec counters → `spec_decode_error=not_enabled`; `attribute(deployment_id, start_ts, end_ts, alone: bool) -> SpecSample | FieldError` returns counter deltas only when `alone` and both bounding samples exist, else `ambiguous_concurrency` / `not_reported`. Never raises; never awaited on the request path.
- Return to the orchestrator the `config.py` diff: `REQUESTS_RAW_RETENTION_S = 30 * DAY_S` with the byte-math comment redone (260 B/row → 9.4 GB at 100 req/s over 30 d, under `ARCHIVE_MAX_BYTES` 16 GiB only at demo rates; the size cap is what protects it), `ROLLUP_1D_RETENTION_S = 400 * DAY_S`, `BUCKET_1D_S = 86400`.
- Tests: null+error-code rule (every new field is `None` when absent, never 0), context bucket edges (8192, 32768, 131072), per-position merge, 1d from 1h exact percentile, migration from a v1 archive file fixture, `usage()` group_by each member of `USAGE_GROUP_BY`, poller attribution rules.

W1 wave gate (orchestrator): apply the `config.py` + `derived.py` requests; FULL backend `python3 -m pytest -q -m "not slow"`; regenerate contracts; update `PROGRESS.md`; heartbeat.

## W2 — U2.gateway (one worker; depends_on all W1)

Owns `control_plane/gateway/**` except `app.py`/`main.py`, `tests/unit/test_gateway_auth*.py`, `tests/unit/conftest.py`. Reads: gateway README, PLAN.md, W1 READMEs.

1. **`gateway/authz.py`** (new): `async def http_guard(request, call_next)` middleware body (installed by app.py request) — path under `/api`: open-list / setup-only checks, then `ctx.auth.session(cookie)` → `request.state.session`; else 401 `session_required`. Path under `/v1`: `Authorization` → `ctx.keys.authenticate` → `request.state.principal = Principal(...)` with org from `ctx.keys.get_org`; errors → 401 with the distinct code; store errors → 503 `auth_unavailable`. Helpers `require_session(websocket)` and `require_key(websocket)` for the two websocket handlers (close 1008 before `accept`). `/api/setup/admin` after an admin exists → 410 `setup_closed`.
2. **`auth_api.py`** (new router): `GET /api/auth/state → {admin_exists, authenticated, username}`, `POST /api/auth/login {username,password}` (sets cookie; 401 `invalid_credentials`; 429 `login_locked` + `Retry-After`), `POST /api/auth/logout`, `GET /api/auth/me`, `POST /api/auth/password {old,new}`, `GET/POST /api/admins`, `DELETE /api/admins/{username}` (removing an admin disables their admin key). `POST /api/setup/admin {username,password}` in `setup_api.py`: creates admin, default org (via `ctx.keys.default_org_id()`), the admin's personal admin key (not revealed), a normal default key named `"default"` with `expiry_days=365` → response `{admin, org, key, secret_once}` + session cookie.
3. **`keys_api.py`** (new): `GET/POST /api/orgs`, `GET/PATCH/DELETE /api/orgs/{org_id}`, `GET /api/orgs/{org_id}/limits`; `GET/POST /api/keys` (`?org_id&status&near_limit&expiring_days&q`; list rows include `limit_states` and 30-day `requests/tokens/cost` from `ctx.telemetry.archive` via `usage()` with `group_by=["key_id"]`, `null` when no archive), `GET/PATCH/DELETE /api/keys/{key_id}`, `POST …/disable {reason?}`, `/enable`, `/rotate`, `/raise {limit_type,value}`, `/reset-cycle`; `GET /api/keys/{key_id}/requests` (+`?format=csv`); `GET /api/usage` (+`?format=csv`), `GET /api/audit`; `GET/PUT/DELETE /api/pricing/{model}` + `GET /api/pricing`. Every mutation calls `ctx.auth.audit(actor=session.username, …)` with before/after dicts. `GET /v1/key` in `openai_api.py` returns the principal's key (no hash), org name, expiry, effective allowlist, `limit_states` for key and org, current cycle usage.
4. **`openai_api.py`**: `_serve` takes `principal` from `request.state`; allowlist + `ctx.limits.check_and_reserve` before `_dispatch` (refusals via `_refused` so a telemetry row with `limit_source` is written); inject `max_tokens`; fallback re-dispatch; pass `policy=` override into `router.select`; `trace` gets identity fields; on completion/cancel call `ctx.limits.settle(reservation, UsageActuals(...))` with cost from `ctx.pricing.cost(...)` (provider rates from `selection.provider` models); `/api/chat/completions|speech|transcriptions` routes (session → admin key principal); `WS /v1/realtime` → `require_key` and record a request row at close.
5. **`proxy.py`**: `_forward_request_headers` also drops `cookie`; delete the LOCAL `Authorization` re-add at `:607-612`; `_StreamAccounting` records per-chunk timestamps → `tpot_mean_ms`, `tpot_p95_ms`, `tail_ms`, `generation_ms`, `tokens_per_s`, `finish_reason`, `cancelled`; `UsageSniffer` additionally parses `usage.prompt_tokens_details.cached_tokens`, `usage.completion_tokens_details.reasoning_tokens`, Anthropic `cache_read_input_tokens`/`cache_creation_input_tokens`, OpenRouter `native_tokens_cached`; `settle()` passes `attempt_chain` (one entry per attempt from the dispatcher), `cost_usd`, `cost_basis`; after settle schedule `ctx.engine_metrics.attribute(...)` via a background task that updates the row through `ctx.sink.request` re-emit with the same `(request_id, attempt_no)` (archive `INSERT OR REPLACE` keeps it idempotent) — never awaited inline.
6. **`shell_api.py`**: `require_session(websocket)` before relay.
7. **`tests/unit/conftest.py`** per Fixed decisions. **`tests/unit/test_gateway_auth_coverage.py`** (`@pytest.mark.raw_http`): reads `.omp/work/auth-keys/sets/v1_routes.json`, `api_routes.json`, `setup_routes.json`; for every v1 route asserts 401 with each of {no key, garbage, disabled key, expired key} and the exact codes (websocket via `TestClient.websocket_connect` expecting `WebSocketDisconnect` 1008; multipart via `files=`); for every api route not in `always_open` asserts 401 `session_required` without a cookie; `POST /api/setup/admin` is 410 after the first admin; `always_open` members do not 401. **`test_gateway_auth_leak.py`**: runs a request over every v1 route with a real key against an app whose `DERATE_LOG_DIR`/`DERATE_DATA_DIR` are `tmp_path`, then asserts the raw key string is absent from `node.log`, `proxy.log`, `journal.db`, `archive.db` (byte search) and present nowhere in any `/api/keys*` response. Plus `test_gateway_auth.py`: 429 shape + headers, 400/413 caps, injected `max_tokens`, cloud fallback telemetry, `/v1/key` shape, chat-via-session attribution to the admin key, `Authorization`/`Cookie` never forwarded (MockTransport inspects headers).
8. Returns as requests: `app.py` diff (`ctx.auth/keys/pricing/limits/engine_metrics` construction, `app.middleware("http")(authz.http_guard)` registered **after** `_csrf_guard` so CSRF stays outermost, `include_router(auth_api)`, `include_router(keys_api)` above the UI mount, lifespan start/stop of `KeyService` expiry sweep task and `EngineMetricsPoller`), `redaction.py` (`drt_` pattern + `has_known_key_shape`), `storage.py::ESTATE` rows, and `pyproject.toml` `[tool.pytest.ini_options] markers += "raw_http: no automatic admin session or API key is attached"` (beside the existing `slow` marker).

W2 gate (orchestrator): apply requests; regenerate contracts + `sets/v1_routes.json`/`api_routes.json` (now with the new routes) and re-run; FULL backend; restart the 18120 coordinator; `curl -s -o /dev/null -w '%{http_code}' localhost:18120/v1/models` → `401`.

## W3 — UI. Orchestrator prep first, then four workers

### Orchestrator prep (shared files)
- `routes.ts`: `Dest` += `'login' | 'keys' | 'orgs' | 'audit'`; `SEGMENT` += `login→'login', keys→'keys', orgs→'orgs', audit→'audit'`; `Route` += `key: string | null`, `org: string | null` parsed from `segments[1]` on `keys`/`orgs` and emitted only there (mirror the `model` handling); `router.check.mjs` pairs for `/login`, `/keys`, `/keys/k1`, `/orgs`, `/orgs/o1`, `/audit`, and "a key id is dropped off `/keys`".
- `shell/AppShell.tsx`: `useAuthGate()` replaces `useFirstRunRedirect`: `GET /api/auth/state` → `!admin_exists` → `setup`; `!authenticated` and dest≠`setup` → `login` (replace); `login`/`setup` render before the frame (no chrome). Move `MetricsProvider`/`TelemetryProvider` under the gate (`main.tsx`) so nothing polls before login. Sections: `KeysTab`, `OrgsTab`, `AuditTab`. `Header.tsx` `DESTS` += keys, orgs, audit, with a logout control calling `backend.logout()`.
- `client.ts`: `req()` on 401 dispatches `window.dispatchEvent(new CustomEvent('derate:unauthenticated'))` (AppShell navigates to login) and throws; new `reqUnscrubbed<T>()` used only by `setupAdmin`, `createKey`, `rotateKey`; `Backend` methods for every W2 route (`authState, login, logout, me, changePassword, admins, addAdmin, removeAdmin, setupAdmin, orgs, org, createOrg, patchOrg, deleteOrg, keys, key, createKey, patchKey, deleteKey, disableKey, enableKey, rotateKey, raiseLimit, resetCycle, keyRequests, usage, audit, pricing, setPricing, clearPricing`); `chatStream/speech/transcribe` paths → `/api/chat/*`.
- `types.ts`: mirror every enum from `contracts/auth.py` + `contracts/telemetry.py` as ordered string unions (`contracts.check.mjs` compares them) and the response shapes (`ApiKeyRow`, `Org`, `LimitState`, `UsageBucket`, `AuditEntry`, `LocalPrice`, `MintedKey`).
- `redact.ts`: add value scrub `/\bdrt_[a-z0-9]{10}_[A-Za-z0-9_-]{32,}/g → '***'` on every string; header comment names the three `reqUnscrubbed` responses as the one exception.
- `ui/src/check/auth.mjs` (new): `login(origin) -> cookieHeader` using `DERATE_CHECK_ADMIN_USER/PASSWORD`; `ui/check.mjs` coordinator probe sends that cookie; `screens.check.mjs` adds the cookie via `context.addCookies` and walks the new dests; `screens.check.mjs` also opens `/keys/<id>` and `/orgs/<id>` from live ids; new assertion: no string matching the `drt_` regex in any page text.
- Design pack (orchestrator, via the `icon-smith` agent for glyphs): icons `key`, `building`, `scroll` (audit), `log-out`, `lock`, `user`; `RevealModal` component (`ui/src/components/RevealModal.tsx`, props `{secret: string; keyId: string; onDone(): void}`; `Copyable` + "I saved it" button; unmounts and never re-renders the secret) registered in `registry.json`, spec in `DESIGN.md`, check in `.omp/tests/ui/components/reveal-modal.check.mjs`; `npm run icons:build && npm run icons:lint && npm run tokens:check` in `.omp`; dated `CHANGELOG.md` entry. Peer check on `.omp/**` first (live peer today).
- Coordinator for checks: create the check admin + key on 18120 once (`POST /api/setup/admin`), export `DERATE_CHECK_ADMIN_USER=check-admin DERATE_CHECK_ADMIN_PASSWORD=<generated> DERATE_CHECK_API_KEY=<default key>` in the orchestrator's shell for every `npm run check`.

### Workers (parallel; fast gate `node <own>.check.mjs` + `npx vite build`)
- **U3.login** owns `ui/src/tabs/login/**` + `ui/src/tabs/SetupTab.tsx` + `ui/src/tabs/setup/**`: `LoginTab` (username/password, 401 message, 429 lockout message with retry time); setup gains a first step `admin` (username, password ×2, min length) posting `backend.setupAdmin` and showing `RevealModal` for the default key before continuing. `login.check.mjs` hermetic (lockout message formatting) + coordinator (`/api/auth/state` shape).
- **U3.keys** owns `ui/src/tabs/keys/**`: `KeysTab` (table columns + filters + search per prompt; lamp mapping `active→live`, `near→warn hollow`, `at→warn`, `disabled→idle`, `expired→fault`; tightest limit = max `fraction`; "Near limit: <type> <pct>%" / "At limit: <type>, resets <time>"), create flow (name, org, expiry select from `KEY_EXPIRY_DAYS`, optional limits, "Models & routing" subpanel) → `RevealModal`; row actions with confirm + optional reason; `KeyDetail` (`/keys/<id>`) with `Chart` from `tabs/dashboard/Chart.tsx` for the series listed in the prompt (30-day default window, `bucket=1h`), per-field error chips rendered verbatim ("error: not_reported (412 requests)"), breakdowns, recent requests table + CSV link. `keys.check.mjs` hermetic: cycle/near/at math, lamp state, null-vs-0, error-code text; coordinator: table rows equal `/api/keys` rows.
- **U3.orgs** owns `ui/src/tabs/orgs/**`: `OrgsTab` + `OrgDetail` (same charts rolled up, org limits editor, allowlist editor, default key settings). `orgs.check.mjs` (same split).
- **U3.audit** owns `ui/src/tabs/audit/**`, `ui/src/tabs/settings/AdminsCard.tsx`, `ui/src/tabs/settings/PricingCard.tsx`: filterable audit table (actor/action/target/time) with before→after; Admins card (list/add/remove, change own password); Local model pricing card (`$/1M in`, `$/1M out`, `null = unpriced`). `audit.check.mjs`. (`SettingsTab.tsx` wiring of the two cards is a one-line diff returned to the orchestrator.)

W3 gate (orchestrator): FULL; `screens.check.mjs` under `--strict`; `ui/src/tabs/keys/reveal.check.mjs` (coordinator+browser): create a key through the UI, assert the secret is in the modal once, absent after "I saved it", absent after reload and from `GET /api/keys/{id}`.

## W4

- **U4.docs** worker owns `docs/guide/**`, READMEs in `control_plane/auth`, `keys`, `limits`, `ui/src/tabs/{login,keys,orgs,audit}`: `the-endpoint.md` (keys required, how to mint one, `/v1/key`), `first-run.md` (admin step + reveal), Layout tables accurate. Orchestrator: CLAUDE.md (dated reveal exception under "Rules that are not style"; new layout rows for `auth/ keys/ limits/`; `tests/unit/conftest.py` note), TODO.md (tick inbound auth; add encrypted per-node backups of telemetry + auth/key DBs, TOTP 2FA, keep one-click spec decode), README.md (keys required paragraph).
- **U4.load** orchestrator: `tests/load/gateway_app.py` bootstraps an admin + key in-process after `create_app` (one `TestClient` call to `/api/setup/admin`) and hands `Authorization` to `driver.py` request writer; `harness.py` sends the session cookie on `PUT /api/routing/loadtest`; run `python3 -m tests.load rps --duration 10 --json .omp/work/auth-keys/load-after.json`; report p99 delta vs `load-baseline.json` in `PROGRESS.md`; a regression > 2× is a failure to fix, otherwise a report.

## W5 — FINAL (orchestrator)

1. Clean: `export DERATE_DATA_DIR=/tmp/derate-authkeys-clean`; `python3 -m venv /tmp/derate-venv && /tmp/derate-venv/bin/pip install -r requirements.txt pytest`; `cd ui && npm ci && npm run build`; coordinator on **18121** from the venv (`DERATE_PORT=18121 DERATE_UI_DIR=… DERATE_LOG_DIR=/tmp/derate-authkeys-clean/logs`).
2. FULL from clean (venv python; `DERATE_CHECK_ORIGIN=http://localhost:18121`), then `npm run screens`; open `ui/screens/{login,keys,keys-detail,orgs,orgs-detail,audit}.png`.
3. Scripted walkthrough, each step a `curl`/`sqlite3` command with the quoted output: `POST /api/setup/admin` → 200 with `secret_once`; `GET /v1/models` no key → `401 missing_api_key`; with key → 200; create key with `requests_cycle=5`, send 4 requests → `/api/keys` row `near=true` + screenshot of `/keys` showing "Near limit: requests_cycle 80%"; 5th → 200 then 6th → `429 limit_source=key:requests_cycle`; `POST /raise {requests_cycle, 10}` → 200 then request → 200; `POST /reset-cycle` → `used=0`; `POST /disable` → `/v1` → `401 api_key_disabled`; create a 1-day key, `sqlite3 … UPDATE api_keys SET expires_at=…` → `401 api_key_expired` and `GET /api/keys/<id>` still returns its 30-day stats.
4. Print the URL `http://<host>:18121/login` + the walkthrough admin login; list PNG paths; final report (one UNIT line per unit from the ledger, every set with size + derive command, every conflict, blocked items with the command). Remove `LOCK`. Do not commit.

## Critical files & anchors

- `control_plane/gateway/app.py:500-625` — `app.state.ctx`, `_csrf_guard` (:530), include order (:551-582), UI mount last; the auth middleware and two routers go here (orchestrator applies the worker's diff).
- `control_plane/gateway/openai_api.py:296-360` (`_serve`, `RequestTrace` mint, `_refused`) and `:1006` (`WS /v1/realtime`) — principal attach, limits, telemetry identity.
- `control_plane/gateway/proxy.py:268` (`_forward_request_headers`) and `:604-612` (LOCAL `Authorization` re-add to delete), `:628`/`:997` (`settle`).
- `control_plane/telemetry/archive.py:28-205` (`SCHEMA_VERSION`, `_SCHEMA`, `_REQUEST_COLS`) and `retention.py::_roll_requests` — migration + new rollup dimensions.
- `control_plane/contracts/routes.py::_walk` — websocket routes are currently dropped (`APIWebSocketRoute` has no `.methods`).

## Verification

- Every unit line: `UNIT <id>: done | gate: \`<command>\` -> \`<last output line verbatim>\` | files: <n>`.
- FULL (each wave boundary): `python3 -m pytest -q -m "not slow"`; `cd ui && npm run typecheck && npm run build && DERATE_CHECK_ORIGIN=http://localhost:18120 npm run check -- --strict` with `DERATE_CHECK_ADMIN_USER/PASSWORD/API_KEY` exported.
- Set-driven coverage (W2): `python3 -m pytest -q tests/unit/test_gateway_auth_coverage.py tests/unit/test_gateway_auth_leak.py` — iterate the JSON sets; a set is never narrowed.
- Behaviour probes (W2, against 18120): `curl -s localhost:18120/v1/models` → `{"error":{"code":"missing_api_key",…}}` 401; `curl -s -H "Authorization: Bearer drt_zzzzzzzzzz_x" …` → `invalid_api_key`; `curl -s -X POST localhost:18120/v1/chat/completions -H "Authorization: Bearer $KEY" -d '{"model":"m","max_tokens":99999,…}'` with a `max_output_tokens_per_request=100` key → 400 naming `100`.
- Telemetry (W1): `python3 -m pytest -q tests/unit/test_telemetry.py tests/unit/test_telemetry_keys*.py`; then on 18120 after one chat request: `sqlite3 /tmp/derate-authkeys/telemetry/archive.db "select key_id, org_id, context_bucket, cost_basis, spec_decode_error from requests order by ts desc limit 1"` → non-null `key_id`, `spec_decode_error` ∈ {`not_enabled`,`not_reported`} on this box (no spec-decode deployment).
- UI (W3): `npm run screens` PNGs opened; `reveal.check.mjs` passes; `grep -rn "drt_" ui/screens/console.txt` → nothing.
- Load (W4): numbers from `load-after.json` vs `load-baseline.json` in the ledger.

## Assumptions & contingencies

- Rollup history: dropping `rollup_requests` and re-rolling from the 30-day raw window is acceptable (the box has little history). If the user wants pre-migration hourly rollups kept, instead rename the old table to `rollup_requests_v1` and have `query.requests()` union it for buckets older than the raw window; `usage()` still reads only v2.
- The pinned vLLM image exposes `vllm:spec_decode_num_drafts_total`, `vllm:spec_decode_num_draft_tokens_total`, `vllm:spec_decode_num_accepted_tokens_total`, `vllm:spec_decode_num_accepted_tokens_per_pos` and sets `usage.prompt_tokens_details.cached_tokens` when prefix caching is on — **unverified; U1.telem reads them off the image first.** If a name differs, use the image's; if `cached_tokens` is absent, record `cache_error=not_reported` for local targets and add the enabling flag to `deploy/flags.py` as a request to the orchestrator only if the image's source shows one exists.
- If the design-pack peer is still editing `.omp/**` when W3 prep starts (mtime < 15 min on files not ours), stop and report rather than editing `registry.json`/`icons.json`; the rest of W3 prep (routes, shell, client) proceeds, and the pack registration is deferred to the first quiet window within the same wave.
- `argon2-cffi` install needs PyPI; verified reachable today. If it fails at execution time, stop U0.3 and report the pip error verbatim (no fallback hasher: argon2id is non-negotiable).
- Session cookie `Secure` is scheme-derived; behind a TLS terminator that speaks plain HTTP to derate the cookie is not `Secure`. No `X-Forwarded-Proto` trust is added (no proxy configuration exists to validate it against).
