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
