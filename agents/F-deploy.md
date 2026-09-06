# Agent F: Deployment Manager and sparkrun Adapter

Read `00-architecture.md` first. Contracts there are frozen.

**You own:** `control_plane/deploy/**`, `docker/**`, `Dockerfile`, `compose.yaml`, `tests/test_deploy.py`
**You depend on:** `ParallelismPlan` from E, `FitResult` from D, `RegistryPort` from A
**Downstream of you:** G routes to the backends you bring up.

---

## What you build

The thing that turns an approved plan into a running inference backend, tracks it, and tears it down. Plus the container story for the control plane itself.

You do not launch inference processes directly. sparkrun already owns fabric setup, SSH mesh, ConnectX-7 subnet configuration, and multi-runtime launch. Wrap it. Rebuilding that is the fastest way to spend four days and ship nothing.

---

## 1. The sparkrun adapter

Render a `ParallelismPlan` into a sparkrun invocation, run it, and parse back the result.

```
plan.tensor_parallel   -> --tp N
plan.pipeline_parallel -> --pp N
plan.node_ids          -> --hosts a,b
runtime                -> vllm | sglang
context_length         -> --max-model-len
max_concurrent_seqs    -> --max-num-seqs
```

Keep the mapping in one table, not scattered through the code. sparkrun's flags will change; when they do it should be one edit.

**Ignore sparkrun's own fit estimate entirely.** It is advisory, it does not block, and it has a known bug producing wrong utilization figures. Agent D's verdict is authoritative. Never launch on sparkrun's opinion.

Parse the launch for: the backend URL the runtime is serving on, the process or container ID, and a ready signal. Treat unparseable output as a launch failure with the raw output preserved in `last_error`.

If sparkrun is not installed, fail with a clear message telling the user to install it. Do not fall back to launching vLLM by hand.

---

## 2. Lifecycle

Enforce the state machine from the architecture doc. Illegal transitions raise; they are bugs, not conditions.

```
PLANNED -> LAUNCHING -> READY -> DEGRADED -> READY
                     \        \           \
                      -> FAILED -> FAILED  -> STOPPING -> STOPPED
READY   -> STOPPING -> STOPPED
```

`launch()` is: check the fit verdict, refuse if `WONT_FIT`, render the invocation, run it, poll for readiness, transition. A refusal returns the `FitResult` reason unchanged. Do not paraphrase Agent D's diagnosis, it is more specific than anything you would write.

Poll each backend's `/health` every 5 seconds. Move to DEGRADED when a node in the deployment goes unhealthy or when memory on any node crosses critical. Move back to READY when it clears. DEGRADED means still serving, so do not stop routing to it; Agent G decides admission.

Persist deployment records so the control plane can restart without losing track of what is running. On startup, reconcile: for every persisted deployment, check whether its backend still answers. Adopt the ones that do, mark the rest STOPPED.

`stop()` tears down through sparkrun and waits for confirmation. A stop that does not confirm within 30 seconds escalates and reports what was left behind.

---

## 3. Runtime OOM watch

Two halves. This half watches nodes and deployments; Agent G watches individual requests.

Subscribe to Agent A's telemetry. Per node in a deployment:

- Above 90 percent memory used: warn, emit an event, mark DEGRADED.
- Above 95 percent: critical. Emit an event Agent G uses to stop admitting new requests to this deployment. Do not kill the process; shedding load is recoverable, killing is not.
- If a backend dies, capture the exit and any OOM signature in `last_error` and transition to FAILED.

When a launch fails with an out-of-memory error despite passing the fit check, log the full breakdown alongside the actual failure. That gap is the calibration data that makes Agent D's estimates better, and it is the most valuable telemetry the system produces.

---

## 4. Docker

**One image, `sparkplane/node`, running on every machine.** Role is resolved at runtime by Agent A, not baked at build time. There is no separate worker image and no separate UI service.

Inference backends are not containerized by us; sparkrun manages those on the host.

```bash
docker run --network host -v sparkplane:/data sparkplane/node
```

The same command on every node. The first becomes coordinator and serves the UI on 8080. The rest discover it and join. That sequence is the demo, so it has to work on a clean machine with nothing preinstalled but Docker and sparkrun.

Requirements on the image:

- **Host networking, enforced.** mDNS does not cross a bridge and the agent must see the real interfaces to report ConnectX-7 topology. Detect bridge networking at startup and exit with a message naming `--network host`. Silently failing to discover is the worst outcome here.
- Multi-arch build. GB10 is arm64 and the workstation is likely amd64. Build both or the heterogeneous case does not work at all.
- UI served from the same origin as the API. No CORS, no second service.
- Volumes: `/data` for persisted state (cluster token, node registry, link measurements, deployment records, resolved-shape cache) and a read-only mount of the host sparkrun config.
- Environment: `SPARKPLANE_ROLE` (default `auto`), `SPARKPLANE_TOKEN`, `SPARKPLANE_JOIN`, `SPARKPLANE_PORT` (default 8080), `SPARKPLANE_AGENT_PORT`. Every one has a working default so first run needs no configuration.
- Health check on `/agent/health`, which exists in both roles. Do not health check `/api/cluster`; it is coordinator-only and would mark every worker unhealthy.
- Restart policy `unless-stopped`. On restart the container re-resolves its role, so a restarted coordinator reclaims the role if no other has taken it.

Ship a `compose.yaml` too, for the single-node case and for people who prefer it, but `docker run` must work standalone. A compose file that is required is a configuration step, and the pitch is that there are none.

---

## Interface you must satisfy

```python
class DeploymentPort(Protocol):
    def launch(self, shape, plan, fit, runtime, ctx, max_seqs) -> Deployment: ...
    def stop(self, deployment_id: str) -> None: ...
    def list(self) -> list[Deployment]: ...
    def get(self, deployment_id: str) -> Deployment | None: ...
```

Plus:

```python
def reconcile(self) -> list[Deployment]
def render_command(self, plan, shape, runtime, ctx, max_seqs) -> list[str]
async def events(self) -> AsyncIterator[dict]      # state changes, OOM warnings
```

`render_command` must be pure and separately testable without running anything. Agent H may display it so the user can see exactly what will run.

---

## Day 0 stub

An in-memory manager that fakes the lifecycle with timers: LAUNCHING for 3 seconds, then READY, with a fake backend URL. Agent G needs this immediately.

---

## Acceptance

- A `WONT_FIT` verdict never produces a launch attempt, and the returned error is Agent D's reason verbatim.
- `render_command` produces the correct sparkrun invocation for pipeline parallel across two hosts, verified against real sparkrun flags.
- A deployment reaching READY exposes a backend URL that answers `/health`.
- Killing a backend moves the deployment to FAILED within 15 seconds with a useful `last_error`.
- Restarting the control plane re-adopts a still-running deployment rather than orphaning or duplicating it.
- Crossing 95 percent memory emits a critical event that Agent G receives.
- An illegal state transition raises rather than silently correcting.
- `docker run --network host sparkplane/node` on a clean machine reaches a working UI with no configuration.
- The same command on a second machine joins the first automatically, no flags, no IPs.
- The image runs on both arm64 and amd64.
- Started with bridge networking, the container exits with a message naming `--network host`.
- A restarted coordinator reclaims the role and re-adopts running deployments.

## Traps

Host networking is required for mDNS, do not use a bridge, and fail loudly rather than silently when it is wrong. Do not build separate coordinator and worker images. Do not skip the arm64 build, the Sparks need it. Do not trust sparkrun's fit output. Do not kill on memory pressure, shed. Do not lose deployment state on restart. Do not paraphrase Agent D's refusal reasons.
