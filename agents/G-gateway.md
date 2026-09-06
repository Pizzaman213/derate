# Agent G: Gateway, Routing, and Admission Control

Read `00-architecture.md` first. Contracts there are frozen.

**You own:** `control_plane/gateway/**`, `tests/test_gateway.py`
**You depend on:** every other port, Agent I's included. You are the composition root.
**Downstream of you:** H codes against your HTTP surface and nothing else.

---

## What you build

One endpoint. Every model in the cluster behind it, whatever node it runs on and whatever runtime serves it.

A client points at one base URL, calls `/v1/models`, and sees everything. It sends a request naming any of them and gets tokens. It never learns which node answered.

You also own the internal API the UI runs on, and the request-level half of out-of-memory prevention.

---

## 1. OpenAI-compatible surface

```
GET  /v1/models
POST /v1/chat/completions
POST /v1/completions
POST /v1/embeddings
```

`/v1/models` aggregates every READY and DEGRADED deployment from Agent F plus every model from every enabled provider via Agent I. The `id` is `Deployment.served_name`. Include the standard OpenAI fields so existing clients work unmodified.

Requests route on the `model` field to that deployment's `backend_url`. Proxy the body through unchanged and stream the response back without buffering. Server-sent events must pass through with no added latency; buffering a stream to inspect it defeats the purpose.

Preserve the backend's status codes and error bodies. A client debugging a vLLM error should see the vLLM error, not a rewritten one.

An unknown model name returns 404 with the list of names that do exist. A model whose deployment is not READY returns 503 with its current state, so a client can retry sensibly.

---

## 2. Routing policy

When several deployments serve the same `served_name`, the policy decides which answers. Contract is in architecture section 4.4. Implement all five.

**LEAST_OUTSTANDING**, the default. Fewest in-flight requests wins. Correct because it accounts for a replica being mid-prefill on a long prompt, which round robin cannot see.

**ROUND_ROBIN.** Plain rotation. Right when replicas are identical and requests uniform, and worth offering because it is what people expect and because it makes an unfair split visible.

**WEIGHTED_CAPACITY.** The answer to unequal machines. Each target gets traffic in proportion to a strength score, which is how a 3090 desktop stops being handed a Spark's share and becoming the cluster's tail latency.

Strength score, in preference order:

1. Measured sustained decode tok/s for that deployment, once 100 requests have completed. Prefer measurement over estimate, always.
2. Agent D's `predicted_decode_tps` for this shape on that node's profile.
3. `memory_bandwidth_gbps * gpu_count`, last resort.

Normalize so weights sum to 1. Recompute every 60 seconds so a thermally throttling node sheds share on its own. Any target below 15 percent of the strongest drops to zero weight and is held as failover only, because a small share of traffic on a very slow node still ruins the tail.

**CACHE_AFFINITY.** Hash the prompt prefix to a target so repeat prefixes land where they are already cached. Fall back to least-outstanding when the chosen target is not admitting. Worth real throughput on agent workloads with a shared system prompt.

**FAILOVER.** Everything to the primary, switch only when it stops admitting. For a deliberately unequal pair where the second node is a spare rather than a peer.

**LOCAL_FIRST.** The reason remote providers exist. Prefer local targets by least-outstanding, spill to remote only when every local target is saturated, unhealthy, or not admitting, and return to local the moment capacity frees. The cluster is the default and the paid API is the overflow valve.

**COST_AWARE.** Cheapest admitting target by `cost_per_mtok`, ties broken on least-outstanding. Local targets price from measured power draw against a configurable electricity rate, defaulting to zero. Targets with unknown cost are skipped rather than assumed free.

Remote targets come from Agent I's `route_targets()`, carrying `kind=REMOTE`. Merge them with local deployment targets into one `RoutingConfig` per `served_name`. A model served both locally and remotely is one entry in `/v1/models` with several targets behind it; the client never learns which answered.

Exclude remote targets from CACHE_AFFINITY, since we cannot reason about a remote runtime's cache. Exclude them from the 15 percent strength floor too, which is a local-hardware rule.

Default to LEAST_OUTSTANDING, automatically to WEIGHTED_CAPACITY when local target strengths differ by more than 25 percent, and automatically to LOCAL_FIRST when both local and remote targets exist. Expose the current policy and the live weights through `GET /api/routing` so the UI can show why traffic is split the way it is. `PUT /api/routing/{served_name}` changes it live, no restart.

A target that is not `admitting`, because memory went critical or it is draining, is excluded from every policy including round robin. If no target is admitting, return 503 rather than queueing indefinitely.

---

## 3. Internal API

Exactly the surface in the architecture doc. No additions without updating that file, since Agent H is coding against it in parallel.

`POST /api/plan` is the one to get right first. It resolves the model, plans, and checks fit, and it launches nothing. This is the dry run that powers the UI's plan panel and the thing you can demo before any inference works.

`GET /api/metrics/stream` is server-sent events, one message per second, in the shape given in the architecture doc. Aggregate from Agent A's telemetry generator and Agent F's per-deployment counters. If a source is unavailable, emit the event with that field null rather than dropping the event. The UI should degrade a panel, not freeze.

---

## 4. Admission control

The request-level half of out-of-memory prevention. Agent F watches nodes; you watch requests.

Before proxying, estimate what this request will cost:

```python
requested_tokens = prompt_tokens + max_tokens
kv_cost = D.kv_bytes_per_token(shape, kv_dtype) * requested_tokens / kv_divisor
```

Track outstanding KV commitments per deployment. Reject with 429 when admitting would exceed the deployment's KV budget, and say when to retry. Reject with 400 when a single request exceeds the deployment's context length, naming the configured limit.

When Agent F reports critical memory on a deployment, stop admitting new requests to it and let outstanding ones finish. Resume when it clears. Never kill in-flight work over memory pressure.

This is what makes the system safe under load rather than merely correct at launch. A cluster that accepts everything and OOMs an hour in has not solved the problem.

---

## 5. Composition

You wire the ports together and own startup order:

1. Registry starts, discovery begins.
2. Link store loads from disk. No measurement on startup, it is disruptive.
3. Resolver cache loads.
4. Deployment manager reconciles against what is actually running.
5. HTTP server binds.

Every dependency is injected. Constructing a gateway with all stubs must work and must be how your tests run, so you are never blocked on another agent.

Startup must not block on a slow or unreachable node. Degraded startup beats no startup.

---

## HTTP surface

Exactly as specified in `00-architecture.md` section 3.6. That is the contract with Agent H.

---

## Day 0 stub

Serve the whole surface from fixtures: two nodes plus one 3090, two measured links, a fake ready deployment with two unequal replicas so the UI can show weighted routing, a metrics stream emitting plausible varying numbers. Agent H builds the entire UI against this and only switches to live data at integration. This stub is the highest-leverage thing you produce on day one, because it unblocks the agent with the most visible output.

---

## Acceptance

- An unmodified OpenAI client, given only the base URL, lists models and completes a chat.
- Streaming passes through with no measurable added latency against calling the backend directly.
- An unknown model returns 404 listing available names.
- A deployment in LAUNCHING returns 503 with its state, not a hang.
- Two replicas of one `served_name` receive balanced load by outstanding requests.
- Under WEIGHTED_CAPACITY with a Spark and a 3090 serving the same model, the 3090 receives proportionally less traffic, and the split matches the strength ratio within 10 percent.
- A target below 15 percent of the strongest receives zero traffic until every other target stops admitting.
- Changing policy through `PUT /api/routing/{name}` takes effect on the next request with no restart.
- A non-admitting target is excluded from every policy, round robin included.
- With no target admitting, requests get 503 rather than hanging.
- Under LOCAL_FIRST, traffic stays local until every local target stops admitting, spills to remote, and returns to local when capacity frees.
- A model served both locally and remotely appears once in `/v1/models` with both targets behind it.
- No provider API key appears in any gateway response or log line.
- A request exceeding the deployment's context returns 400 naming the limit.
- Admitting past the KV budget returns 429 with retry guidance.
- A critical memory event stops new admissions within one second and lets in-flight requests finish.
- `POST /api/plan` returns a plan and fit for a real HuggingFace ID without launching anything.
- The metrics stream sustains 1 Hz to multiple concurrent subscribers.
- The gateway constructs and serves with every dependency stubbed.

## Traps

Do not buffer streams. Do not rewrite backend errors. Do not make round robin the default, it cannot see a replica mid-prefill. Do not route to a non-admitting target under any policy. Do not compute strength from a spec sheet once measured throughput exists. Do not block startup on a slow node. Do not add endpoints without updating the architecture doc, Agent H is building against it right now.
