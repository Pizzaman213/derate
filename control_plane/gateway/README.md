# Gateway (Agent G)

One OpenAI endpoint, seven routing policies, admission control, and the request
path's answer to a node dying under it. The frozen contracts are 00-architecture
sections 4.5, 4.7 and 4.8; the behaviour below is recorded in that document's
integration appendix.

This file covers the failover machinery only. For the policies themselves see
`policies.py`, which is the frozen surface and deliberately untouched by any of
this.

"One endpoint" means one base URL, not one route: the surface is
`/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/audio/speech`
and `/v1/audio/transcriptions`. The first four are three-line wrappers over the
same `_proxy` dispatcher, which takes the upstream path as an argument;
transcriptions is a multipart upload rather than JSON, so it enters through
`_proxy_multipart` instead, and both meet again in `_serve`. Everything
below applies to all of them unchanged.

`/v1/realtime` is the exception to that sentence. It is a WebSocket, and a
session cannot be re-offered to another target halfway through -- the upstream
holds conversation state we never saw -- so none of the failover machinery
below applies to it. See `realtime.py`. What differs per family is which
*models* may be named: `Modality` (`contracts/modality.py`) records whether a
served name answers text or audio, and `_proxy` refuses a mismatch before
anything is sent. Text and embeddings are one family on purpose — one vLLM
server answers both from the same weights.

## What happens when a node dies

Routing in section 4.5 is a *selection-time* decision. `FAILOVER` and
`LOCAL_FIRST` choose a different target for the **next** request; neither has
anything to say about the request already in flight to a node that has just
gone. Three pieces close that gap.

### 1. The request is re-offered to another target

`openai_api._proxy` holds the parsed body for the life of the call and runs
`_dispatch()`, which selects, sends, and on a retryable failure selects again
with the failed target excluded.

```
attempt 1: d-a  -> ConnectError
  d-a excluded, breaker told
attempt 2: d-b  -> 200, tokens stream
client sees: one 200, and no hint that d-a existed
```

Retryable is a transport failure or any 5xx. **Every 4xx is terminal** — a 422
is the backend working correctly, and asking a different node is the wrong
question. An attempt also stops being retryable the moment its first body chunk
is yielded, because the status line is then committed to the client.

Per attempt, everything is rebuilt from the original request: the body, the
auth header, the provider key, and the admission commitment. Hoisting any of it
out of the loop would carry one target's body rewrite -- or one provider's API
key -- onto the next target.

When the chain is exhausted the client gets the **last upstream 5xx verbatim**:
status, headers, bytes, and nothing of ours added on top. Only when no target
was ever reached does the gateway answer with its own 502
`upstream_unreachable`. Rule 2 holds at the end of the chain, not just the
first hop.

`httpx.PoolTimeout` is the exception, and it is not a transport failure. It
means *this gateway* had no free upstream connection within
`upstream_connect_timeout_s` -- a fact about us, not about the backend, which
may be answering in microseconds. It is still retryable across targets, since
another origin has its own pool, but it never reaches the circuit breaker and it
answers 503 `upstream_pool_exhausted` rather than 502 `upstream_unreachable`.
Counting it as a transport failure is what took the whole gateway down on
2026-09-08: leaked connections filled the pool, three PoolTimeouts benched two
healthy backends, and the half-open probe died on the same empty pool and
re-benched them for as long as anyone kept asking.

### 1a. An upstream response is always closed

One `AsyncClient` per origin, each bounded by `upstream_per_origin_limit`, so a
backend that exhausts its slots cannot take the others with it. That only holds
if connections come back, and there are two ways they did not:

- **The cleanup ran inside a cancelled scope.** Starlette cancels the anyio
  scope a `StreamingResponse` body runs in the instant the client hangs up, and
  an anyio cancel scope is *level*-triggered -- every later `await` in it raises
  at once. The bare `await response.aclose()` in the generator's `finally`
  therefore never ran. `close_quietly` shields it. `asyncio.shield` does not
  work here; the cancellation is anyio's, not `Task.cancel`'s.
- **The generator never started.** Starlette sends the response line before it
  first iterates, so a client that vanishes in between leaves the body generator
  constructed and unstarted -- and an unstarted async generator runs no cleanup
  when it is collected. There is no `finally` to shield, so `UpstreamProxy`
  tracks every open response and a janitor closes the ones whose body was never
  read. It reaps *only* those: a running stream is never touched however long it
  takes, because reaping on age would be a read timeout by the back door and
  `upstream_read_timeout_s` is `None` on purpose.

### 1b. Recovery is decided by bytes, not by a clock

An attempt used to stop being retryable the moment `upstream_header_hold_s`
expired. That was the wrong line to draw. At that moment the client has a status
line and **zero body bytes**, so nothing it has seen would be contradicted by
streaming a different target's body underneath the same headers.

The measure is now what the *client* has seen. `forward` takes a `reopen`
callback and, when an upstream dies with nothing yielded yet, asks the dispatcher
for another target and finishes from there; the client sees one uninterrupted
response. Past the first byte the old rule stands unchanged -- rule 2 above --
because splicing two completions together is worse than a truncated one.

Why it matters more than it sounds: the hold is 10s, and on this cluster it
fired 130,815 times, **every one of them the 30B** (76-130s prefill) and none of
them the 0.5B (5s). Protection keyed on a fixed timer meant the fast model was
always covered and the slow one never was -- backwards, since the slow request
is the expensive one to lose.

`failover_deadline_s` deliberately does not apply after commit. It exists to stop
stacking retries on a request nobody is waiting for; a client that has held for a
two-minute prefill is the opposite case. The bound there is attempts.

### 1c. Hedging, off by default

With `hedge_after_s` set, a leader that has produced nothing by then is raced
against a second target and the first usable response wins. `policies.hedge_candidate`
refuses far more often than it accepts: never under `LOCAL_FIRST`, where sending
to the remote is a saturation decision and not a speed one, and never onto a
target below `weak_target_floor`, which would lose every race and burn the
capacity to learn nothing.

The loser must be handed back explicitly. Its body generator is never started, so
no `finally` runs -- `Attempt.discard` settles the accounting, releases the KV
commitment and returns the connection. `forward` also unwinds if it is cancelled
while waiting on the first chunk, which is where a losing hedge is usually
killed. Without both, every hedged request would leak exactly what §1a describes.

### 2. Headers are held until the first chunk

`proxy.forward` waits for the first body byte before constructing the
`StreamingResponse`, bounded by `upstream_header_hold_s`. A node that dies
during prefill is therefore still someone else's to answer.

One chunk is held, never the stream. The client's first byte arrives exactly
when it always would have; only the status line moves to that moment. Past the
budget the headers go out and the generator waits out the rest of the prefill --
a long prefill is not a failure, and the timeout must never cancel the read.

### 3. The dead target is benched

`breaker.py`. Three consecutive transport failures open a circuit for 30s; then
one half-open probe decides whether it closes or serves another cooldown.

This exists because `deploy/manager.py` needs two 5s health polls to notice a
dead backend. For those ~10 seconds `RouteTarget.healthy` is still true and
every arriving request is routed into the hole and waits out the connect
timeout. The breaker is the gateway's own, faster answer, and it also catches
what the health poll cannot: a backend that answers `/health` and 500s
inference.

Two rules keep it from doing harm:

- **It only ever subtracts from health.** Agent F stays authoritative for
  putting a target back, so the two cannot disagree into routing at something
  known dead.
- **Only transport failures trip it.** A backend answering 500 is reachable.
  Benching it for saying so would let one malformed request take every replica
  of a model out of rotation at once.

Reads are side-effect-free. `Router._refresh_live` serves `GET /api/routing` as
well as real requests, so if looking at a circuit consumed its probe, a UI open
in a browser would eat every one and no benched target would ever recover.
`state()` and `is_open()` look; `begin()` claims, and only a dispatch calls it.

There is no `await` between `router.select()` and `breaker.begin()`, which is
what lets the single-token half-open probe be safe without a lock. Preserve
that if either moves.

## Parking

Section 4.5's rule is 503 rather than queueing *indefinitely*. `parking.py` is
the bounded case that leaves room for: a model that was serving a moment ago
whose node has just gone. Such a request is held up to `park_grace_s` and
dispatched the instant a target returns, FIFO, so the longest wait gets the
first recovered slot.

Two conditions must **both** hold, in `Router.parkable`:

| Situation | Answer |
|---|---|
| Rate limited, draining, memory critical | instant 503, as before |
| Unknown model | 404 |
| Still launching for the first time | 503 `model_not_ready` |
| Served a moment ago, node just died | held, then dispatched or 503 |

Load shedding is a decision, not an outage; queueing behind one hides exactly
what an operator needs to see. A model that has never served already has an
honest answer of its own.

Be clear-eyed about the win. With failover working, a node death usually
produces a successful response on attempt 2 and no parking is involved. Parking
only matters when a model has **one** target and it died -- and a real relaunch
takes minutes, well past the grace period. Its genuine case is a transient blip
that self-heals inside the window. Set `park_grace_s = 0` for the fast refusal
instead.

## Retry budget

`budget.py`. Retrying every 5xx is right when a node has died and wrong when the
request itself is what the backend objects to, and the two are
indistinguishable from here. Rather than guess, this bounds the cost of
guessing wrong: retries may not exceed `retry_budget_ratio` of real traffic over
a trailing window.

The floor matters as much as the ratio. Ten percent of one request is zero, and
a single node dying on a quiet cluster is exactly when failover has to work.

## Tunables

All in `settings.py`, all with working defaults.

| Setting | Default | |
|---|---|---|
| `failover_max_attempts` | 2 | A third attempt only ever answers "this request is the problem" |
| `failover_deadline_s` | 30.0 | Wall clock over the whole chain |
| `upstream_header_hold_s` | 10.0 | How long the status line waits for a first byte |
| `breaker_failure_threshold` | 3 | One failure is a coincidence |
| `breaker_cooldown_s` | 30.0 | Past a vLLM restart, inside a demo's patience |
| `retry_budget_ratio` | 0.1 | Ceiling on retries as a share of traffic |
| `retry_budget_window_s` | 10.0 | |
| `retry_budget_floor` | 3 | So a quiet cluster still gets failover |
| `park_grace_s` | 10.0 | 0 disables parking entirely |
| `park_eligible_memory_s` | 60.0 | How recently a model must have served to be held |
| `park_max_waiters` / `park_max_per_model` | 256 / 64 | Overflow refuses, never queues |
| `park_max_body_bytes` | 1 MiB | An enormous prompt is refused rather than held |

Telemetry has its own settings, in `control_plane/telemetry/config.py`, and its
own env vars. It is off when its data root does not exist, so `pytest` and a
development machine record nothing without opting out.

## The seam with Agent H

`GET /api/routing` carries one added field per target:

```
"circuit": "closed" | "open" | "half_open"
```

Without it a benched target reads as `healthy: false` for a deployment that
`/api/deployments` reports READY, with nothing to explain the difference.

## Deliberately not built

- **Durable, on-disk request *replay*.** A journal of in-flight requests that
  survives a gateway restart has nowhere to deliver the answer: the HTTP
  client is long gone. It would only make sense alongside an async job API,
  which is not in scope. Recording what a *completed* request did is a
  different feature and is now built -- see `control_plane/telemetry/` and the
  durable-telemetry appendix in `00-architecture.md`. `proxy.settle()` writes
  one row per attempt at the moment the numbers exist, and `X-Request-Id` ties
  the attempts of one client request together.
- **Retrying admission's 429.** Tempting under `LOCAL_FIRST` -- a local 429
  could spill to a remote -- but it changes what `kv_cache_exhausted` means and
  touches an acceptance criterion. It stays terminal.
- **A parked-request gauge *in the SSE frame*.** `metrics.snapshot()`'s
  `queue_depth` counts in-flight upstream requests, so parked ones are still
  invisible there, and adding a field would touch the frozen 4.8 frame shape.
  Parking now emits an event on the way in and on the way out carrying the
  wait and the outcome, so the queue is no longer ungauged -- the numbers are
  in `GET /api/history/events`, just not in the live frame.

---

## Layout

Everything above is the request path under failure. The rest of the folder is
the other two thirds of the surface: the `/api` control plane the UI drives, the
serializers that decide what may leave the process, and the composition root
that puts them in the one order that works. 36 modules, 14,811 lines, 75 route
decorators.

| File | Lines | What it owns |
|---|---|---|
| `internal_api.py` | 3637 | the `/api` control surface -- 45 routes in one closure, plus `_plan_and_fit` |
| `capacity_api.py` | 1601 | read-only memory truth and "what could run here", with every hub call bounded |
| `proxy.py` | 1344 | upstream proxying, connection ownership, and per-attempt accounting |
| `openai_api.py` | 1017 | `/v1`: `_proxy` / `_proxy_multipart` in, `_serve` for everything they share |
| `serialize.py` | 689 | contract types to JSON, with provider fields as an explicit allowlist |
| `app.py` | 604 | `create_app`, the six-step startup, the CSRF middleware, and `_UIStatics` |
| `stubs.py` | 508 | the day-0 ports that serve the whole surface from fixtures |
| `router.py` | 421 | the target index, policy resolution, weight refresh, `parkable` |
| `admission.py` | 350 | the per-request KV commitment, and the three block reasons |
| `restart.py` | 290 | relaunching a deployment that crashed, three tries, backed off |
| `policies.py` | 286 | the seven selectors and `hedge_candidate`. Frozen |
| `enroll_api.py` | 279 | `/install.sh` and the one-line join command, address resolved server-side |
| `runtime_api.py` | 270 | finding a runtime already listening on a node, and adopting it |
| `targets.py` | 252 | merging local deployments and remote provider models into one target list |
| `livefit.py` | 250 | calling the fit gate twice -- static ceiling and live allocatable |
| `errors.py` | 224 | OpenAI-shaped error bodies, and the redacted exception detail |
| `ui_detail.py` | 222 | the pure computations behind the additive UI fields; `None` never becomes `0` |
| `settings_store.py` | 221 | the four operator-settable fields, persisted; file beats env beats default |
| `realtime.py` | 219 | `/v1/realtime`, a relay and deliberately nothing more |
| `metrics.py` | 214 | the 1 Hz SSE frame, one producer and many subscribers |
| `settings.py` | 202 | 52 tunables, every one with a working default |
| `setup_api.py` | 184 | first run: has anyone set this cluster up yet |
| `shell_api.py` | 175 | the browser-to-node terminal relay; the credential is never ours |
| `breaker.py` | 167 | the per-target circuit. Subtracts from health, never adds |
| `ui_api.py` | 166 | `GET`/`PATCH /api/settings`, on its own uncontended router |
| `setup_state.py` | 154 | one fact on disk, outranked by the derived signals |
| `parking.py` | 151 | the bounded waiting room |
| `strength.py` | 144 | measured, then predicted, then bandwidth -- and the weight normalization |
| `deps.py` | 112 | `GatewayDeps` / `GatewayContext`, and `strict=True` |
| `stats.py` | 110 | per-target EWMA counters: decode tok/s, TTFT, outstanding |
| `gpu_procs.py` | 92 | attributing a resident GPU process to the deployment that launched it |
| `budget.py` | 92 | the retry budget |
| `csrf.py` | 59 | refusing a cross-site write before it reaches any route |
| `main.py` | 54 | `python -m control_plane.gateway.main`, the stub surface, banner and all |
| `states.py` | 40 | which deployment states mean what, spelled without importing `deploy` |
| `__init__.py` | 11 | four names: `create_app`, `GatewayContext`, `GatewayDeps`, `GatewaySettings` |

## `internal_api.py`

The whole `/api` control surface: 45 routes in a single `create_router(ctx)`
closure, covering the cluster, topology, nodes, links, routing configs,
providers, publishers, plans, deployments, activity, the metrics stream, history
and storage. A port that does not expose an operation gets a 501 from
`_not_implemented(operation, owner)` naming the owner, rather than the route
disappearing -- the UI is built against the full shape from day 0 and a missing
route is indistinguishable from a bug in the browser.

`_plan_and_fit(ctx, payload)` is the file's centre of gravity and one of the two
names `restart.py` imports from here -- the other is `_sharding_refusal`, which
answers why a runtime cannot run a set of degrees. It resolves, plans and checks
fit without launching
anything, and every port call inside it goes through `asyncio.to_thread`:
resolution is a network round trip, the planner search and the fit calculation
are CPU, and blocking the event loop there stalls every in-flight token stream on
the box. It returns a `_PlanOutcome` carrying both verdicts, the memory picture
`_capacity_block` assembled, the placement and whose choice it was, the effective
degrees, the planner's own recommendation with its rejection list intact, and the
modality taken from the resolution that produced the shape -- looking the
modality up again later would let a cleared cache record a speech model as text,
and the gateway would then refuse the requests the deployment exists to serve.

Refusals are `_PlacementRefused`, deliberately not a `ValueError`: the generic
`except ValueError` in both plan routes flattens whatever it catches into a bare
400 `invalid_request`, which strips the code, the param and the override block
that make a refusal actionable. The two overrides are named for exactly what they
override -- `allow_over_live_memory` and `allow_mixed_hardware` -- and neither
implies the other.

Three module-level records exist because the HTTP request that started the work
ends before the work does. `_PULLS` holds strong references to in-flight provider
pulls, because asyncio keeps only a weak one and a collected task is a download
that simply never arrives. `_DOWNLOADS` holds a `_Download` per transfer with a
`total` that is `None` rather than `0` until the upstream reports one, and a
finished record lingers `_DONE_LINGER_S` (20s) so a fast local pull is seen at
all. `_LAUNCH_SEEN` timestamps a launch because `Deployment.started_at` is None
until the READY transition stamps it, which is why the wire field is called
`since` and the screen does not call it "launched".

`DELETE /api/storage/nodes/{node_id}/models/{folder}` is the largest reclaim in
the product -- one 120B repository is 182 GiB -- and the only one that can break
a running deployment, so the in-use check lives here and not on the agent, which
has no idea what a deployment is. Folders are compared as encoded names, never
decoded back to repo ids, because decoding is ambiguous the moment a name
contains a double hyphen.

## `capacity_api.py`

Memory truth and the inverse fit question, all of it read-only: nothing on these
routes changes cluster state. `GET /api/memory` is the single app-wide poll, so
the header, the node rails and the headroom view cannot disagree about a number
they all draw; a registry with no `memory_report` answers 503 saying nothing here
is a measurement rather than inventing one. `GET /api/capacity` answers "the
largest model that runs here" under both budgets at once, so the gap between the
live figure and the static ceiling is visible rather than argued about.

Every knob in this file bounds a hub call, and each is a different question.
`MAX_CATALOG_MODELS` (8) caps the curated walk; `MAX_CAPACITY_MODELS` (12) caps
a set the client named, and is deliberately not the same number -- the merged
model list asks for about ten at a time, so eight would push two rows into an
"unresolved" sentence on every call. `_RESOLVE_FANOUT` (4) sits inside urllib3's
default pool of ten so no connection is discarded, and an unauthenticated hub
answering a wide fan-out with a 429 would turn "which of these fit" into "the hub
said no" for the whole screen. `_CAPACITY_TIMEOUT_S` is one deadline for the
whole batch and it **degrades**: resolves that finished still produce rows and
the stragglers are reported, where a whole-request 504 would throw away verdicts
already computed.

`context=` and `concurrency=` are optional, and absent is a different question
from a value: absent means "choose one per model", which is what makes the
endpoint answerable on a fresh install with nothing configured. Each row reports
the numbers it was judged at, because with a derived context one figure at the
top of the table no longer describes every row in it.

## `proxy.py`

Covered above -- §1a is this file's connection ownership and §1b its `reopen`
callback. Three rules head the module and the rest follows from them: do not
buffer streams, do not rewrite backend errors, never let a provider key escape.
Worth adding here is the retry taxonomy, which is three constants and not two:
`RETRY_TRANSPORT`, `RETRY_SERVER_ERROR`, and `RETRY_POOL` kept apart so the
breaker can ignore an exhausted pool and so history can tell it from a dead node
after the fact. `_Discarded` is thrown into a losing hedge's provider upstream
rather than exiting it cleanly, because `open_upstream` records `note_success`
and the spend in the code after its yield -- exiting cleanly would bill a hedge
that lost and vouch for a transfer that never happened.

## `openai_api.py`

The `/v1` surface. `_proxy` parses JSON and `_proxy_multipart` handles the one
upload endpoint; both meet in `_serve`, which owns model lookup, the modality
guard, failover, the breaker, parking and the trace id. The trace id is minted
before the first thing that can refuse, so a 404 is recorded as readily as a 200
and every attempt of one client request shares it; it goes back as
`X-Request-Id`.

`_ENDPOINT_MODALITY` maps each path to the model family it needs, and
`_AUDIO_MODALITIES` is the set that is genuinely exclusive. TEXT and EMBEDDING
are deliberately absent from it: one vLLM server answers `/v1/chat/completions`
and `/v1/embeddings` from the same weights and this gateway has always let it, so
treating them as exclusive would refuse requests that work today. Audio is the
axis that is actually new, and it is checked in both directions.

`GET /v1/audio/voices` is not an OpenAI route and does not go through `_serve`.
It exists because `runtimes/tts.py` refuses an unknown voice name by design and
nothing outside the container could enumerate them. It keeps the model lookup and
the modality guard, answers from a **local** target only -- a voice library is a
property of a deployment's filesystem -- and returns the runtime's own envelope
unaltered, including its `skipped` list, which is the only sentence saying why a
clip was declined.

## `serialize.py`

Contract types to JSON, and the last gate before anything leaves the process.
`provider_payload` is an allowlist rather than a dataclass dump, for the one
unrecoverable mistake available here: this is a tool people screenshot.
`api_key_ref` is safe -- it is an env var name and is what tells somebody which
variable to set -- and any resolved key material renders as `REDACTED`, imported
from `providers/config.py` rather than re-typed so there is one thing to change
on the day `***` stops being the spelling.

`routing_payload` is where the UI's per-target detail is assembled, and its
sidecar arguments (`sources`, `circuits`, `zero_weight_reasons`, `counters`,
`strength_raw`, `admission_blocks`) are computed in `ui_detail.py` so that adding
a field is a call-site edit rather than surgery in a contended file. `key_state`
and `key_source` are always both present even under a port with no
`key_status()`, because a missing field and a null field read the same to a UI
and only one survives a round trip through JSON.

## `app.py`

`create_app(deps=None, *, settings=None, http_client=None, telemetry=None)` --
with no arguments every dependency is a stub, which is how the day-0 surface and
the whole test suite run. The module docstring numbers six startup steps; the
lifespan owns the first five and the sixth is uvicorn binding the socket. Those
five are six `_optional_step` calls -- registry, links, resolver,
`deployments.reconcile`, `deployments.start`, providers -- each bounded by
`startup_step_timeout_s` (5.0): a step that times out or raises is recorded in
`ctx.degraded_startup` and the gateway comes up anyway. Reconcile and start are
two calls rather than two names for one, because `reconcile()` only self-starts
the watch loop when it adopted something -- a fresh install with nothing to adopt
would otherwise never start watching at all.

Teardown is not the exact reverse. The deployment-event consumer is cancelled,
then parked requests are released: they are clients still holding a connection,
and shutting down under them hangs both sides instead of answering. Metrics,
restart, router, admission and proxy stop next, the ports themselves follow
through `_best_effort_shutdown`, and telemetry stops last, so anything the
shutdown path emits is still recorded.

`_csrf_guard` is installed unconditionally, unlike CORS, which appears only when
`DERATE_ALLOWED_ORIGINS` names an origin. That asymmetry is the point: the
default same-origin deployment installs no CORS middleware at all, and without
the guard any page open on the LAN could blind-POST to every unauthenticated
`/api` route.

## `stubs.py`

Seven day-0 ports -- `StubRegistry`, `StubLinks`, `StubResolver`, `StubFit`,
`StubPlanner`, `StubDeployments`, `StubProviders` -- serving the entire surface
from fixtures, so the UI could be built before any other component worked. Every
one returns real contract types; a stub that returns something else is worse than
no stub, because it certifies a consumer against a shape that will never arrive.
Numbers come from `tests.fixtures` when it is importable, so the stub agrees with
every other package's tests, and fall back to equivalent local values when the
gateway runs outside the repo. They are still load-bearing:
`contracts/routes.py` composes them to enumerate every route both apps answer
without standing up a real cluster.

## `router.py`

The routing table. `Router.index()` builds a `TargetIndex` and reuses it for
`index_ttl_s` (1.0s), but outstanding counts and the `admitting` flag are
refreshed on every selection, so a critical memory event takes effect on the next
request rather than at the next weight refresh. Weights themselves are recomputed
on `weight_refresh_interval_s` (60s), which is what lets a thermally throttling
node shed share on its own without anybody deciding it should.

`Router.parkable(served_name)` is the second of parking's two conditions and is
documented above; `recently_eligible` is the other half of it, and it is also
what separates "its node died" from "no such model" once a failed deployment has
left the index entirely -- answering 404 for a model that was serving a minute
ago tells the client something untrue. `Selection` carries the config, the
target, and whichever of deployment/provider/model produced it, so the dispatcher
never has to look any of them up a second time.

## `admission.py`

The request-level half of out-of-memory prevention: estimate what a request will
cost in KV cache, refuse it if the deployment cannot afford it, and hold the
commitment until the response completes. `check()` returns an
`AdmissionDecision`; `commit()` and `release()` bracket the request, and
`Attempt` in `proxy.py` is what guarantees the release happens even on a hedge
that lost. Three block reasons exist and each is spelled once:
`BLOCK_MEMORY_CRITICAL`, `BLOCK_DRAINING`, `BLOCK_RATE_LIMITED`. `is_blocked` and
`blocks` are the read side, consulted by `targets.build_index` and by
`Router.parkable`.

`kv_bytes_per_token_fallback(shape, kv_dtype)` prices the cache from
`_KV_ELEMENT_BYTES` when the fit package's own `kv_bytes_per_token` is not
available -- preferred whenever it is, because that one knows about `num_kv_heads`,
sliding windows and MLA and this table does not. `Deployment` carries no KV dtype,
so absent one the estimate assumes `default_kv_dtype` (`"fp16"`).

## `restart.py`

A deployment that crashes lands in FAILED and, before this module, stayed there.
`RestartCoordinator` subscribes to the same STATE_CHANGED fan-out `app.py`
already consumes -- a second independent subscriber, which the event bus was
built for -- and relaunches with fresh planning up to `MAX_RESTART_ATTEMPTS` (3)
on `RESTART_BACKOFF_S` `(5.0, 20.0, 80.0)`. Those are delays before attempts one,
two and three, not between failures: three tries span about 105 seconds, which
gives a flaky node or driver real time to settle instead of exhausting the budget
in under thirty.

Telling a crash from an operator's own stop needs no help from `deploy/manager.py`:
`stop()` racing a LAUNCHING deployment tags that one case with the literal
`OPERATOR_STOP_REASON = "stopped during launch"` on the same event, and every
other FAILED reason -- a fatal runtime marker, an exited container, a ready
timeout, a health probe that stopped answering -- is a genuine crash. State is
in-memory per `served_name` and is never persisted, matching the deploy manager's
own reconcile state: a crash that outlives this process starts a fresh budget
rather than resuming a stale count. The whole decision lives in the gateway layer
because it is a gateway policy, and `deploy/` may not import back up into it.

## `policies.py`

The seven selectors, plus `hedge_candidate` and `may_hedge`. Covered above and
frozen; `eligible()` is the universal filter every one of them runs first, and a
target that is not `admitting` is excluded from all seven, round robin included.
`SELECTORS` is the dispatch table and `select()` the entry point.

## `enroll_api.py`

`GET /install.sh` and the enrollment tokens behind the one-line join command.
Two properties this file protects. **The script never contains a token**: the
same bytes go to everyone and the credential is only ever an argv value in the
command the UI renders, which is what makes serving it on an unauthenticated
surface fine. **The address comes from the server, not the browser**:
`_coordinator_origin` prefers the coordinator's own probed address and falls back
to the request's Host header only when that is not loopback -- the browser's
`window.location.host` is correct in production and a lie in dev, which is why
the Settings card stopped showing it. `install_script_path()` looks at
`/opt/derate/install.sh` first (the installed name inside the image) and then the
repo root, with `DERATE_INSTALL_SH` overriding both, so a checkout serves the
script it actually has.

## `runtime_api.py`

A GPU-less machine cannot carry a rank, but it can host Ollama, and adding it as
a provider was three manual steps of which the coordinator already knew two --
the node's address, and that Ollama listens on 11434.
`GET /api/nodes/{id}/runtime` reports what is listening and `POST` adopts it
using the address the registry already probed rather than one somebody retyped.
Both positions here are refusals of something easier. **Detection never registers
on its own**: finding a runtime produces a suggestion, the same stance
`registry.offer_candidate` takes about a discovered node, and it matters more
here because a provider is a routing target and one that appeared without anybody
choosing it is traffic going somewhere nobody meant. **Only nodes in the roster
are probed** -- there is no subnet scan, so this reaches nothing the coordinator
could not already reach.

## `targets.py`

`build_index` merges local deployments and remote provider models into one target
list per `served_name`, pure given its inputs, and the client never learns which
kind answered. `ROUTABLE_STATES` is `states.SERVING`, which includes DEGRADED:
degraded is up but impaired, it is listed in `/v1/models`, and memory pressure is
expressed through `admitting` rather than by making a target invisible. A
deployment that is neither routable nor terminal goes into `index.pending`, which
is what lets `_serve` answer `model_not_ready` with the actual states instead of
a 404. `local_cost_per_mtok` prices a local target from measured power draw
against `electricity_rate_usd_per_kwh`, which defaults to 0.0 -- local is free,
and therefore always cheapest, until somebody says otherwise.

## `livefit.py`

The seam that makes asking for a live budget safe. `dual_check` calls the fit
gate twice -- once on the static ceiling, once on `allocatable_map`'s live figures
-- and `serve_decision` puts both on the wire with a `basis` of `BASIS_LIVE` or
`BASIS_STATIC`, so the backend rather than the UI decides which governs.
`_accepts_allocatable` probes the port's `check` signature, because the gateway
composes ports it does not own and the keyword is an additive deviation from the
frozen one. Two rules run through the file: **absence is never zero** -- a
registry that cannot answer, a node with no telemetry yet, or a port that
predates the parameter all degrade to the static verdict, because a cold
coordinator must not refuse every launch -- and **never fabricate the live
answer**, so `live` is None with an `unavailable_reason` rather than a number
nobody measured. `drop_zero_addressable` removes a node whose profile reports
zero addressable bytes -- a container that could not probe its GPU -- because it
would otherwise be the argmin of every budget and make every verdict WONT_FIT.
Each removal is recorded and surfaces in the refusal rather than being dropped
silently.

## `errors.py`

The gateway's own error bodies, OpenAI-shaped. Errors that came from a backend
are passed through untouched and never reach this module. Each constructor names
the remedy: `unknown_model` lists what does exist so a client can correct itself,
`model_not_ready` reports the states it is actually in, `wrong_modality` says
which endpoint family the model answers on, `upstream_pool_exhausted` says
outright that the backends are not implicated and sets `Retry-After: 5`.

`detail(exc, redactor)` is the reason a refusal is useful:
`f"{type(exc).__name__}."` discards the only part an operator can act on, and a
live plan once returned `Could not plan: MetadataUnavailable.` whose real cause --
the hub closing the connection -- existed solely in the log. `cause_chain` walks
`__cause__`/`__context__` up to `MAX_CAUSE_DEPTH` (4). Both run every string
through `_scrub` against the **shared** provider redactor: a fresh instance
scrubs only values it has been told about, and a redactor that raises drops the
message and answers with the bare class name rather than risk emitting an
unredacted one. Long detail is cut at `MAX_DETAIL_CHARS` (500) -- a diagnostic,
not a document.

## `ui_detail.py`

Pure computation for the additive fields the UI needs. Every function takes data
and returns a plain dict; nothing touches HTTP, nothing imports `serialize` or
`internal_api`, and nothing raises. It exists for a mechanical reason: several
features want to add keys to two files that several sessions edit at once, and
putting the computation here collapses each edit to a call site and a few named
keys.

The rule every function follows is that **`None` means "we do not know" and is
never written as `0`**, and the pull toward zero is strongest exactly where it
does most damage. `unpriced_requests_today = 0` says "we watched every request
and could price them all"; `metered_requests_today = 0` says "we watched, and the
provider priced none of them". Over a port that watched nothing both are false,
and the Spend screen reads those two fields to decide whether a figure is a
charge or a forecast. `completed` genuinely is a counter and does start at 0,
which is why the distinction is made field by field rather than by policy.

## `settings_store.py`

`GatewaySettings` has 52 fields. Four of them are things an operator sets and
expects to survive a restart -- `electricity_rate_usd_per_kwh`, `local_only`,
`daily_spend_cap_usd`, `auto_restart_crashed_deployments` -- and `MUTABLE_FIELDS`
is that allowlist. Persisting the whole dataclass would let a stale file pin a
code default forever, and the first person to change a default in source and
watch it not take effect would have no way to find out why.

Precedence is **file > env > default**, the opposite of the usual ordering and
deliberately so: the file is the record of a human action taken through the UI,
env is the deployment's opinion, and nothing in the container sets any of these,
so env-beating-file would leave the UI controls permanently inert. Writes are
atomic and 0600, mirroring `providers/store.py` exactly -- mkstemp in the target
directory, `fsutil.harden_fd` before any content, `os.replace` -- so a crash
between the two leaves the old file intact rather than a half-written one.
`SCHEMA_VERSION` is 1 and has been present since the first version, because a
config file without one is a migration you cannot write later.

## `realtime.py`

Covered in the opening section as the exception to failover. What is worth
repeating is the boundary: this is a **relay**, joining the client's socket to an
upstream that already speaks the OpenAI realtime protocol and pumping frames both
ways without parsing them, which is the only reason it is 219 lines. It does not
synthesise a session out of local speech-to-text, chat and text-to-speech -- that
is thirty-odd event types, server-side voice activity detection, audio buffering
and resampling and barge-in, none of which exists here -- so a local model is
refused with a message saying so rather than accepted into a session that would
never produce audio. `CLOSE_POLICY` is 1008, the closest thing the WebSocket spec
has to a 4xx.

## `metrics.py`

`MetricsHub` produces one frame per `metrics_interval_s` (1.0) and fans it out to
every subscriber. If a source is unavailable the frame still goes out with that
field null, so the UI degrades a panel rather than freezing. `snapshot()` reads
power and temperature through `serialize.power_reading`/`temp_reading` rather than
straight off the node state, because a node with no GPU reports 0.0 W and sending
the raw figure here put a measured-looking 0 W beside the null `/api/nodes` sends
for the same machine. Every node row carries `sample_ts`, so the UI can grey a
reading that has aged out instead of passing a frozen sample off as current.

## `settings.py`

52 fields, every one with a defensible default so `create_app()` with no
arguments produces a working gateway. The comments are the documentation and
several of them are incident reports: `upstream_per_origin_limit` (64) exists
because on 2026-09-08 one 30B model's abandoned streams filled all 256 pool slots
and a 0.5B that had leaked nothing stopped answering 65 ms later.
`upstream_read_timeout_s` is `None` on purpose -- a decode that takes ten minutes
is not an error. `max_audio_upload_bytes` and `max_json_body_bytes` are both
25 MiB, which is what OpenAI accepts and therefore the number a client expects;
the audio cap is a real limit rather than a formality, because the model name
lives in a form field that may be the last part in the stream and a failed target
still has to be retryable, so the bytes must stay in hand. `local_only` is a hard
block rather than a routing preference: `policies.eligible()` filters on
`admitting` before any of the seven selectors run, so it outranks all of them,
whereas `LOCAL_FIRST` is merely a policy.

## `setup_api.py`

Two routes, and the module exists for one question: *has anyone set this cluster
up yet*. It computes nothing about models, memory or fit -- the setup screen asks
`/api/capacity` for what will run here and `POST /api/enroll` for the join
command, because both questions already have an owner and a second answer would
drift from the first, at which point the first screen a person sees would promise
a token rate the launch path never agreed to.

## `shell_api.py`

`GET /api/shell/status` and a WebSocket at `/api/nodes/{node_id}/shell`. A relay,
the same shape and reasoning as `/v1/realtime`: frames are pumped between the
browser and the node agent unparsed, because a terminal session is none of the
coordinator's business. What the coordinator owns is finding the node.

The credential is **not** checked here. It is checked by the agent at the far end
against a secret only that machine holds, so the coordinator cannot grant a
session, cannot be tricked into granting one, and need not be trusted with the
key -- it forwards the `derate-shell` subprotocol and lets the node refuse. That
is a deliberate asymmetry with the process-kill route next door, which has the
coordinator fetch the cluster token and present it on the caller's behalf: a
confused deputy, where an anonymous request gets a credentialed action. Relaying
a key is carrying an envelope, not signing one.

## `breaker.py`

Covered above under "The dead target is benched". The two invariants worth
restating because both are one-line changes away from being lost: it only ever
subtracts from health, so the deployment manager stays authoritative for putting
a target back, and only transport failures trip it, so a backend answering 500 is
not benched for being reachable. `_Circuit.probing` admits exactly one half-open
probe -- without it a burst arriving the instant the cooldown expires is let
through at once, which is the stampede the breaker exists to stop.

## `ui_api.py`

`GET` and `PATCH /api/settings`, on a second `APIRouter` rather than more
handlers in `internal_api.py`. That is a working practice, not taste: the big
file is one closure that several sessions edit concurrently, FastAPI composes
routers natively, and the cost of a separate module is one `include_router` line
against the benefit that this file is never contended. `PATCH` validates through
`settings_store._coerce` and refuses a key outside `MUTABLE_FIELDS`, so an
unknown field cannot reach the file whatever a caller sends.

## `setup_state.py`

One fact -- was first-run setup walked through -- in `setup.json`, deliberately
not a fifth key in `settings_store.py`: it is a one-shot fact about a cluster's
history, not a preference, and no deployment would ever want to set it from the
environment. **The stored flag is not the whole answer and must not be.**
`is_complete(flag=, deployments=, providers=)` checks the derived signals first,
so a cluster serving a model reports "this cluster is serving 2 models" rather
than "a file existed". That ordering is what makes the failure survivable in the
right direction: a lost or unwritable file re-offers setup to an empty cluster,
which is a wizard somebody dismisses, while the opposite mistake drops a working
cluster back into onboarding, which reads as data loss. The write mirrors
`SettingsStore.save` exactly.

## `parking.py`

Covered under **Parking** above. `ParkingLot` is the mechanism; the decision of
whether a request may enter it is `Router.parkable`, not this file. Every exit is
bounded -- by `park_grace_s`, by `park_max_waiters`/`park_max_per_model`, and by
`park_max_body_bytes`, because a held request is held in memory and 256 copies of
an enormous prompt is a different order of problem. `_Queue.changed` is set
whenever a waiter leaves so the next in line wakes immediately instead of sitting
out the rest of its poll interval.

## `strength.py`

Strength in strict order of preference: measured sustained decode tok/s once
`measured_strength_min_requests` (100) have completed, then the fit gate's
`predicted_decode_tps` for this shape on this node's profile, then
`memory_bandwidth_gbps * gpu_count` as a last resort. Never a spec sheet once
measured throughput exists. Which rung answered is surfaced as
`SOURCE_MEASURED`/`SOURCE_PREDICTED`/`SOURCE_BANDWIDTH`/`SOURCE_DEFAULT` through
`/api/routing`, so the UI can say why a split looks the way it does rather than
leaving somebody to guess. `compute_weights` normalizes within one `served_name`
to sum to 1 and applies `weak_target_floor` (0.15); `strength_spread` is what
`auto_weighted_spread` (0.25) is compared against when the router picks a policy
nobody set.

## `deps.py`

`GatewayDeps` holds the seven ports and defaults every one to its day-0 stub, so
constructing a gateway with all stubs works and is how the tests run.
`strict=True` turns a missing port into a startup `RuntimeError` naming exactly
which ones are absent, and `node.py` sets it on the real composition root for one
reason: silently falling back to fixture data is a day-0 stub wearing a real
gateway's clothes, and strict mode is how a composition root asserts every port
really is wired instead of hoping it would have noticed. `GatewayContext` is the
runtime half -- stats, admission, router, proxy, metrics, breaker, parking, retry
budget, sink, events, telemetry, inventory -- with the later additions defaulted
so every existing construction keeps working.

## `stats.py`

Per-target counters, and the reason the routing table can prefer measurement over
estimate: the gateway is the only component that sees a request end to end.
`TargetStats.complete()` folds tokens, duration, decode window and TTFT into
EWMAs at `_EWMA_ALPHA` (0.2). Decode rate is tokens over the decode window when a
first-token boundary was seen and over the whole request when it was not, which is
the non-streaming case. `retry_after_s(default)` answers a rejected client from
observed request time rather than a constant, so the number it is told to wait is
one this cluster has actually produced.

## `gpu_procs.py`

`attribute(processes, deployments, handles, node_id)` decides which resident GPU
processes this control plane launched, and therefore whether a Kill button
appears. A process we can name a deployment for is **not** killable from here:
killing a backend the router is still dispatching to leaves the record claiming
READY for two health polls while the process is gone, and the operator who
clicked has no way to connect the 502s to their own click. The deployment's own
Stop is the correct verb and it drains first.

Attribution is by evidence on the command line -- the sparkrun cluster id or the
port the manager allocated -- because that is all there is: the backend's PID was
never recorded, since sparkrun launches it across an SSH hop and hands back only
a cluster id. An unmatched process is reported unattributed rather than guessed
at, which errs toward a Kill button on something we launched (guarded by a
confirm) rather than a missing one on a stray holding the whole pool. `handles=None`
means the port exposes no launch handles, as the stubs do not, and every process
is then unattributed -- degrade, never refuse.

## `budget.py`

Covered under **Retry budget** above. One detail from the code: `take()` emits a
`retry_refused` event every single time, while the log line is sampled one in
fifty -- a counter sampled at 2% cannot answer "how often did failover get
refused during that incident", which is the only question anybody asks it
afterwards.

## `csrf.py`

`is_cross_site_write(...)` is a pure predicate, and `app.py`'s middleware is its
only caller. It exists because the `/api` surface has no per-request credential,
so the only thing between "a page you opened" and "a page that rewrote your
cluster" is whether a browser sent the request on your behalf. A GET cannot do
that, but a cross-origin `fetch` inside the CORS simple-request shape can, and
`internal_api`'s handlers read the body with `request.json()`, which parses
whatever arrived regardless of the `Content-Type` a simple request is limited to.
Comparing `Origin` against the request's own scheme and netloc is the standard
defense: neither header is settable by a page's own script, so a page cannot forge
agreement between them. A **missing** `Origin` is let through -- every browser
sends one on a state-changing request, so its absence means curl, an SDK or a
health check, none of which is what CSRF is about.

## `main.py`

`python -m control_plane.gateway.main` runs the **stub** surface, and prints a
72-character banner saying so before uvicorn binds, because the real system is
`python3 -m control_plane.node` and a fixture gateway that looks real is the
failure this whole package guards against. `log_config=None` is not incidental:
without it uvicorn replaces the logging configuration after the fact, two formats
share one stream, and its access logger stops propagating to the root handler the
telemetry journal is attached to.

## `states.py`

Three frozensets -- `TERMINAL`, `SERVING`, `LIVE` -- so the gateway can spell
which deployment states mean what without importing `control_plane.deploy`. That
import is not laziness to avoid: `from control_plane.deploy import fsm` runs the
package `__init__`, which pulls the deployment manager, the sparkrun adapter and
the event bus into a request-handling module to obtain one frozenset. Four
modules had each arrived at their own spelling -- `internal_api` as a named
frozenset, `targets` as both a tuple and an inline pair, `gpu_procs` as a
complement -- and only the first had anything checking it. `LIVE` is spelled as
the complement of `TERMINAL` rather than listed, so a new state joins it by
default: a state nobody has classified yet is far likelier to be live than
finished. `tests/unit/test_single_source.py` holds all of it equal to `deploy.fsm`
through `contracts/derived.py`.

## `__init__.py`

Four names: `create_app`, `GatewayContext`, `GatewayDeps`, `GatewaySettings`.
That is the entire import surface other packages use.

## The seam with `node.py` and `contracts/routes.py`

Only two modules in `control_plane/` import this package, and they import
different halves of it.

```python
# node.py, the real composition root. Two functions, not one call:
# build_gateway_deps() assembles the ports ...
from control_plane.gateway.deps import GatewayDeps
from control_plane.gateway.settings import GatewaySettings

return GatewayDeps(registry=..., links=..., resolver=..., fit=...,
                   planner=..., deployments=..., providers=...,
                   strict=True, settings=settings)

# ... and _serve_coordinator() builds the app from them.
from control_plane.gateway.app import create_app

gateway_app = create_app(deps, settings=deps.settings,
                         telemetry=runtime.telemetry)
```

`strict=True` is the whole point of the first half: it converts a port somebody
forgot to wire from a silent stub into a `RuntimeError` naming it, raised in
`GatewayDeps.__post_init__` before the app is built at all. Passing `telemetry`
is the whole point of the second. `create_app` given none builds its own bundle
from the environment, which is right for the stub gateway and wrong on a
composed coordinator: two Journal writer threads on one `journal.db`, two
Archives on one `archive.db`, and two Collectors racing cursors over the same
local journal.

```python
# contracts/routes.py, which enumerates every route both apps answer
from control_plane.gateway import GatewayDeps, GatewaySettings, create_app
from control_plane.gateway.stubs import ...
```

That one composes the stubs deliberately, because enumerating routes must not
need a cluster.

In the other direction the gateway imports `contracts`, `registry`, `providers`,
`telemetry`, `inventory`, `planner`, `humanize`, `redaction`, `procmatch`,
`fsutil`, `logfiles`, `paths` and `version` at module scope, and `resolver` only
lazily, inside the handlers that reach the hub. It imports
`control_plane.fit.capacity`'s `context_for` at module scope in
`internal_api.py` and everything else from `fit` lazily inside the handler that
needs it. It **never** imports
`control_plane.deploy` at module scope: `states.py` exists so it does not have
to, and `_sharding_refusal` reaches for `deploy.flags` inside a `try` at call
time and degrades to None.

## Things that look like details and are not

**Every router registers ABOVE the `StaticFiles` mount.** A Starlette mount at
`"/"` catches every path not matched by an *earlier* route, so an
`include_router` below it never runs -- and the failure is silent in the worst
possible way: `/api/settings` answers `index.html` with a 200, which surfaces to
the caller as a JSON parse error three layers from the cause. It bites hardest on
two paths. `/install.sh` is root-level, so below the mount `curl | sh` on a new
machine gets piped an HTML document and fails somewhere around the first `<`, on
the machine being installed, with nothing useful on screen. `/api/setup` is the
first call a freshly installed UI makes, so getting it wrong turns the product
into a blank screen on the one boot where nobody has any context for what went
wrong. `app.py` carries a comment on each `include_router` line saying so.

**The UI's deep paths are answered by `_UIStatics.get_response`, and the fallback
is deliberately narrow.** Screens live in the path (`/cluster`,
`/models/meta-llama/Llama-3.1-8B`) and nothing exists on disk under them, so a
404 there is served `index.html` with status 200 -- the document is the correct
answer to a request for a real screen. `_NO_FALLBACK` holds it back from `api`,
`v1`, `assets`, `healthz` and `install.sh`, and a missing hashed asset must stay
a 404: load an HTML document into a `<script type="module">` and a broken deploy
renders as a blank page with nothing in the network log to explain it. The second
test is `Accept`, because a model id is allowed a dot in it and a browser asking
for `/models/meta-llama/Llama-3.1-8B` is only distinguishable from a request for
a missing file by that header.

**`index.html` is `no-store` and hashed assets are `immutable`.** Vite
content-hashes every asset and deletes the previous one on each build, and
`ui_dir` is served straight off disk. Starlette sends an ETag and Last-Modified
but no `Cache-Control`, and absent one a browser may reuse `index.html` without
revalidating -- so a tab holding the old document asks for a hash that no longer
exists, gets a 404 and renders nothing. Silent from every angle: the server logs
a 200 for `/`, the page is blank, and no `/api/*` request is ever made to hint
that the app never started.

**`states.py` exists so the gateway can spell terminal and live without
importing the launcher.** One frozenset is not worth pulling the deployment
manager, the sparkrun adapter and the event bus into a request-handling module.

**A separate `APIRouter` is a concurrency decision, not a taste one.** Six of
them -- `ui_api`, `capacity_api`, `enroll_api`, `runtime_api`, `setup_api`,
`shell_api` -- exist because `internal_api.py` is 3,637 lines in a single closure
that several sessions edit at once, and adding a handler to it means holding it.
Each new module costs one `include_router` line.

**`inventory_api` is registered below `capacity_api` on purpose.**
`capacity_api` owns four literal paths under `/api/models/`, and registering a
sibling above them is how one of those would one day start answering the wrong
handler. `inventory_api` adds only the bare `/api/models`, which collides with
nothing; the ordering is belt and braces and `tests/unit/test_inventory_api.py` pins
it.

## Failure behaviour

- **A port raises during startup.** `_optional_step` records it in
  `ctx.degraded_startup`, `GET /healthz` reports the list, and the gateway binds
  anyway. Degraded startup beats no startup.
- **A port does not implement an operation.** 501 from `_not_implemented`, naming
  the operation and its owner. The route never disappears, so the UI can be built
  against the full shape.
- **A port raises inside a handler.** Logged with `log.exception` and degraded to
  an empty list or a null field -- `_nodes()`, `_labels()`, `_links_for` and
  `capacity_api._report` all follow this shape. One unavailable source never
  takes a whole payload down.
- **The registry cannot report memory.** 503 with a sentence saying nothing here
  is a measurement, rather than a fabricated number. `livefit` separately degrades
  to the static verdict with an `unavailable_reason`.
- **The hub is slow or rate limiting.** Every capacity route is bounded
  (`_DETAIL_TIMEOUT_S` 12s, `_VARIANTS_TIMEOUT_S` 20s, `_SEARCH_TIMEOUT_S` 6s,
  `_CAPACITY_TIMEOUT_S` for a whole batch) and the batch deadline degrades:
  finished rows are returned and the stragglers are listed in `unresolved[]`.
  Failures are memoised for `_RESOLVE_FAIL_TTL_S` (120s) because `ShapeCache`
  never caches one, so a permanently gated `meta-llama/*` would otherwise buy a
  fresh hub round trip on every capacity request. Timeouts are deliberately not
  stored: a hub that was slow once is not a hub that is broken.
- **A model registry that will not open.** Caught at `create_app` time;
  `/api/models` says why rather than the coordinator failing to start.
- **A cross-origin write.** 403 `cross_origin_write_refused`, naming the origin
  and telling the operator which variable would allow it.
- **A settings write that cannot land.** `SettingsStore` writes atomically at
  0600; a crash mid-write leaves the previous file intact. An unwritable
  `setup.json` re-offers the wizard rather than losing a configured cluster.
- **A node agent refuses.** `_agent_detail` unwraps FastAPI's nested `detail`,
  falling back to the raw body, so an unexpected shape stays readable instead of
  printing "unknown error".

## Deliberately not built

- **A synthesised realtime session.** `/v1/realtime` relays to an upstream that
  already implements the protocol. Building one out of local speech-to-text, chat
  and text-to-speech means thirty-odd event types, server-side voice activity
  detection, buffering and resampling and barge-in; a local model is refused with
  that sentence rather than accepted into a session that would never produce
  audio.
- **Auto-registering a detected runtime.** `runtime_api` reports what is
  listening and waits for a click. A routing target that appeared without anybody
  choosing it is traffic going somewhere nobody meant.
- **A subnet scan.** Only nodes already in the roster are probed, so the feature
  reaches nothing the coordinator could not already reach.
- **Persisting all of `GatewaySettings`.** Four fields are operator preferences;
  the other forty-eight are decisions, and a stale file pinning a code default
  forever is unfindable from the source.
- **A fifth settings key for first-run state.** `setup_state.py` is its own file
  because the flag has no env form and no honest `GatewaySettings` default.
- **Setup computing its own fit or install line.** `/api/setup` answers one
  question. A second answer to "what will run here" would drift from
  `/api/capacity`, and the first screen anybody sees would promise a speed the
  launch path never agreed to.
