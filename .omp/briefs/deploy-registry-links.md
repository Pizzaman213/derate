# deploy, runtimes, registry, links brief

Owns: turning an approved plan into a running backend (sparkrun, recipes, flags, progress, restarts); the nodes and their health and memory readings; the measured interconnect and its tuning.
Code: control_plane/deploy/, control_plane/runtimes/, control_plane/registry/, control_plane/links/   Tests: tests/unit/test_deploy.py, test_deploy_adopt.py, test_deploy_reap.py, test_engine_flags.py, test_registry.py, test_telemetry.py, test_links.py, test_links_measure_hosts.py, test_gateway_restart.py, test_integration_kv.py, test_tts_runtime.py   Read first: control_plane/deploy/README.md, control_plane/registry/README.md, control_plane/links/README.md, control_plane/runtimes/README.md; CLAUDE.md "Rules that are not style"

An agent reads Depth 1 for every part it touches, and goes deeper only as far as the task needs.
Every `path::symbol` reference below is verified by the gate, so the brief cannot name code that
does not exist. The READMEs hold the long form; the line counts in their tables are stale.

## Depth 1 -- 30 seconds
- **deploy** launches nothing the fit gate refused, renders the exact argv before it runs it, and shows only sentences sparkrun or the runtime really printed. `DeploymentManager` is the FSM and the launch worker.
- **registry** is the node agent and the coordinator's view of nodes: discovery, join, health, 1 Hz telemetry, host memory. **links** measures the interconnect (a ladder of rungs) and tunes NCCL per pair.
- **runtimes** holds the one inference server derate ships itself (tts). It runs INSIDE a model container; nothing else imports it.
- Never: launch what the gate refused; drop a priced knob in silence (render it or refuse); invent a number for something unmeasured; let a live reading be missing turn into a made-up budget (a CPU node is the one place a missing reading refuses).
- Never touch `control_plane/contracts/` to unblock yourself. A frozen contract changes in one place, announced.

## Depth 2 -- the rules
- **A launch is refused before a record exists.** `launch()` runs the identifier and extra-arg checks synchronously, so a bad id is a refusal and not a late FAILED. Enforced in `control_plane/deploy/manager.py::DeploymentManager.launch` and `control_plane/deploy/recipes.py::check_recipe_identifiers`; the fit reason is carried unedited in `control_plane/deploy/manager.py::LaunchRefused`.
- **A knob the gate priced must reach the engine or be refused, never dropped.** A recipe key absent from the command template is dropped by sparkrun in silence (kv dtype went fp16 for weeks while the gate charged fp8). Refuse with a `*_refusal` function: `control_plane/deploy/flags.py::kv_cache_dtype_refusal`, `control_plane/deploy/flags.py::speculative_refusal`, `control_plane/deploy/flags.py::graph_capture_refusal`, `control_plane/deploy/flags.py::nccl_env_refusal`. Pinned by `tests/unit/test_integration_kv.py::test_every_width_the_gate_prices_either_reaches_the_engine_or_refuses`.
- **The recipe, not the registry recipe, decides what runs.** `synthesize()` writes the command so `--pp 2` cannot be accepted and run as PP=1. It is pure; `materialize()` writes `<slug>-<runtime>-<sha256[:8]>.yaml`, so equal inputs give equal bytes at an equal path. See `control_plane/deploy/recipes.py::synthesize`, `control_plane/deploy/recipes.py::materialize`.
- **Three injection grammars guard the recipe and the shell.** The YAML document, `{model}` (run under `bash -c` in a host-networked container) and operator `extra_args` each have their own allowlist. `extra_args` and `custom_command` are mutually exclusive. `--speculative-config` is built by the control plane, never passed as `extra_args`. See `control_plane/deploy/recipes.py::_check_yaml_safe`, `control_plane/deploy/recipes.py::_check_command_safe`, `control_plane/deploy/recipes.py::check_extra_args_safe`, `control_plane/deploy/flags.py::render_speculative_config`.
- **`flags.py` is the only file that knows sparkrun's command line.** `KNOBS` order is render order; every knob carries a `recipe_key`; when sparkrun moves, this is the one edit. `SPARKRUN_VERIFIED_VERSION` names what the box runs, so do not bump it ahead of the box. See `control_plane/deploy/flags.py::KNOBS`, `control_plane/deploy/flags.py::RuntimeSpec`, `control_plane/deploy/flags.py::SPARKRUN_VERIFIED_VERSION`.
- **Only the recipe's `env:` block can keep state between launches, and it goes beside `hub/`, never inside.** The container's only writable mount is the HF cache; `modelcache` measures and deletes `models--*` and would bill a compile cache as weights. See `control_plane/deploy/flags.py::RUNTIME_CACHE_DIR`, `control_plane/deploy/flags.py::cache_env_for`, `control_plane/registry/modelcache.py::scan`.
- **A runtime that spends host RAM is a placement question with an accounting answer.** `placement_refusal` is the one predicate (six call sites used to spell it out). A CPU node with no live sample refuses, because its static ceiling is 0 and there is nothing to degrade to. Everywhere else a missing live reading degrades to the static ceiling and says so. Enforced in `control_plane/deploy/flags.py::placement_refusal`, `control_plane/registry/telemetry.py::allocatable_bytes`, `control_plane/registry/telemetry.py::allocatable_bytes_or_none`.
- **`--ctx-size` is llama.cpp's shared pool; vLLM's `--max-model-len` is per sequence.** derate's `context_length` is per sequence, so a runtime with `context_is_shared_pool` gets `context x concurrency`. Wrong is silent: READY, every request gets a fraction. See `control_plane/deploy/flags.py::RuntimeSpec`, `control_plane/deploy/adopt.py::parse_serve_command`.
- **`gpu_memory_utilization` is a claim on the whole device, derived per launch.** It comes from the fit breakdown, capped by what is free and by the guardrail, from the LIVE total. A constant failed every launch on a shared node while the gate said `fits`. See `control_plane/deploy/utilization.py::utilization_for`, `control_plane/deploy/manager.py::DeploymentManager._launch_utilization`.
- **Progress is only what a program printed.** An unrecognised line changes nothing; the phase never walks backwards; the only `fraction` comes from a program counting itself; `fatal` is never held back. When a marker stops matching after a version bump, fix the marker, never infer the phase. See `control_plane/deploy/progress.py::advance`, `control_plane/deploy/progress.py::_RUNTIME_FATAL`, `control_plane/deploy/progress.py::from_runtime_log`.
- **Liveness is three-valued.** `None` means sparkrun could not be asked; only an explicit `False` is death, and `stop()` waits for that. A hard False from a missing binary retired live deployments. See `control_plane/deploy/sparkrun.py::SparkrunAdapter.check_job`, `control_plane/deploy/sparkrun.py::SparkrunAdapter.is_running`, `control_plane/deploy/manager.py::DeploymentManager.reconcile`.
- **The restart budget survives the relaunch and clears only on READY.** A fresh state per FAILED event meant 1,094 launching->failed transitions in two days; giving up is announced from `_handle` too. See `control_plane/gateway/restart.py::RestartCoordinator._handle`, `control_plane/gateway/restart.py::MAX_RESTART_ATTEMPTS`, pinned by `tests/unit/test_gateway_restart.py::test_a_crash_loop_spends_the_budget_once_and_then_stops`.
- **Memory pressure sheds load and never kills.** `MEMORY_CRITICAL_FRACTION` only tells the gateway to stop admitting; the one place a process is signalled is an explicit operator stop. See `control_plane/deploy/events.py::MEMORY_CRITICAL_FRACTION`, `control_plane/deploy/manager.py::DeploymentManager._kill_through_agents`.
- **A measurement is honest about being a guess, and `None` travels the whole way.** Rungs: nccl-tests, then `nccl-torch`, then `ib_write_bw` (scaled, `estimated=True`), then TCP. An estimate must never beat a measurement; a rung that raises is skipped, not fatal. See `control_plane/links/measure.py::LadderMeasurer`, `control_plane/links/measure.py::TorchNcclMeasurer`, `control_plane/links/record.py::IB_TO_NCCL_RATIO`.
- **A collective latency is a collective.** `ib_write_lat` times a one-sided write, which once left the planner's key input ~28x optimistic. Unmeasured latency is charged `UNMEASURED_COLLECTIVE_LATENCY_US` and says so on screen. See `control_plane/links/measure.py::IbWriteBwMeasurer`, `control_plane/contracts/constants.py::UNMEASURED_COLLECTIVE_LATENCY_US`.
- **`worst_all_reduce` is `None` if any pair is unmeasured; measurement is serialized and never on a timer.** Every probe saturates the fabric, so two at once give two wrong numbers. See `control_plane/links/service.py::LinkService.worst_all_reduce`, `control_plane/links/service.py::LinkService.measure`.
- **Anything timing the fabric must run the container a real launch runs, with the bootstrap interface named.** Without `/dev/infiniband` NCCL falls to TCP with no warning (243 us vs 17 us). See `control_plane/links/collective.py::docker_argv`, `control_plane/links/collective.py::local_interface_for`, `control_plane/links/collective.py::run_collective`.
- **Tuning wins in bulk and may not regress at decode.** NCCL reads its env once per communicator, so one setting serves the process; the choice uses the last incarnation's traffic mix. Every pair a plan spans must agree or nothing applies; `DERATE_NCCL_ENV` overrides. See `control_plane/measurements.py::tuning_env`, `control_plane/measurements.py::preference_for`, `control_plane/deploy/manager.py::DeploymentManager._nccl_env_for`, `control_plane/links/service.py::LinkService.calibrate`.
- **The probe changes nothing about the transport it measures.** No `NCCL_NET_GDR_LEVEL`, no `NCCL_IB_DISABLE`; pinned by `tests/unit/test_links.py::test_the_probe_does_not_tamper_with_the_transport_it_is_measuring`.
- **Registry answers with evidence, not `platform.system()`.** GB10 reports aggregate memory N/A, so the pool comes from `/proc/meminfo` and apps from per-process accounting (hence `--pid=host`). The static ceiling is never shown as headroom. See `control_plane/registry/telemetry.py::read_host_memory`, `control_plane/registry/telemetry.py::read_compute_apps`.
- **A node that stops answering is unhealthy, never removed, and keeps its last telemetry.** A wrong token is `JoinRejected` with no detail; a worker never promotes itself. See `control_plane/registry/registry.py::Registry.record_health`, `control_plane/registry/registry.py::Registry.apply_sample`.
- **`tts` is a runtime like the others and its claim is about `runtimes/tts.py`.** An architecture is supported only after somebody ran it through that server. `FATAL_MARKER` is copied (not imported) into `progress.py`, and a test keeps them equal. See `control_plane/runtimes/tts.py::FATAL_MARKER`, `control_plane/resolver/support.py::TTS_ARCHITECTURES`.

## Depth 3 -- how to change it
**Add a launch knob (an engine flag).**
1. Add the flag to the runtime's `command_template` and a `Knob` in `control_plane/deploy/flags.py::KNOBS`; add a `render_*` function if the value is not a plain scalar (JSON goes through a whole-string grammar and YAML double quoting, as `render_speculative_config` does).
2. Add a `*_refusal(runtime, ...)` for every runtime whose template lacks it, and call it from `DeploymentManager.launch` and the plan path, so the verdict and the launch agree.
3. Thread it through `control_plane/deploy/recipes.py::synthesize` and `control_plane/deploy/sparkrun.py::SparkrunAdapter.render_command`; add it to `control_plane/deploy/adopt.py::parse_serve_command` if a running container must read back.
4. Tests: a render test and a refusal test in `tests/unit/test_engine_flags.py`; extend `tests/unit/test_integration_kv.py` when the gate prices it. Regenerate if an env var or contract moved: `python3 -m control_plane.contracts.manifest --write`, then `... routes --write`, then `... document --write`.

**Add a runtime (inference server).**
1. Add a `RuntimeSpec` to `control_plane/deploy/flags.py::RUNTIMES` (image env, command, custom prefix, `health_path`, `cache_env`, `memory_pool`, API-key env/arg).
2. Add its architecture list and quant profile in `control_plane/resolver/support.py`; a name belongs there only once the server has loaded that checkpoint.
3. Add launch markers to `control_plane/deploy/progress.py` only if you have read them off a real run; a fatal line goes in `_RUNTIME_FATAL`.
4. Tests: `tests/unit/test_deploy.py`, `tests/unit/test_engine_flags.py`, and `python3 -m tests.model_sweep --arch`.

**Change the launch lifecycle (a state, a restart rule, a stop path).**
1. Edit `control_plane/deploy/fsm.py::LEGAL`; illegal transitions raise, so add the edge or do not move that way. `reconcile()` is the only place a state is set without an FSM check.
2. Keep `stop()` three-valued; escalate through `_kill_through_agents`; never kill on memory pressure.
3. If the retry rule changes, edit `control_plane/gateway/restart.py::RestartCoordinator` and keep the budget alive across relaunches. Tests: `tests/unit/test_deploy.py`, `tests/unit/test_gateway_restart.py`.

**Add a link rung or a measured tunable.**
1. A rung is a class with `method`, `available()` and `measure()`; insert it in `control_plane/links/measure.py::default_measurer` in evidence order (a measurement above an estimate).
2. Check `available()` against a real box, not only `measure()`: a rung that measures but reports unavailable never runs (`tests/unit/test_links.py::test_available_is_true_when_the_image_is_here`).
3. A new NCCL candidate goes in the `CALIBRATION_ENVS` tuple on `control_plane/links/service.py::LinkService`, and the name must be in `control_plane/deploy/flags.py::NCCL_TUNABLES`. Add it only with a measured row behind it.
4. Parsers return `None`, never a guess: `control_plane/links/parsers.py::parse_nccl_perf`. Tests in `tests/unit/test_links.py`.

**Add a node field the planner or gate reads.**
1. Add it to the frozen profile contract (announced), then read it in `control_plane/registry/probe.py::probe_local`.
2. Carry it through `control_plane/registry/serde.py` and `control_plane/registry/profiles.py::profile_supersedes`, so a failed re-probe cannot overwrite a good profile.
3. If it is a live reading, add it to `control_plane/registry/telemetry.py::TelemetrySample` and make `allocatable_bytes` degrade, not refuse.
4. Tests: `tests/unit/test_registry.py`, `tests/unit/test_telemetry.py`; regenerate the three contract documents.

**Change a progress marker.**
Edit `control_plane/deploy/progress.py::_RUNTIME_MARKERS` or `_SPARKRUN_MARKERS` from a literal line you saw; add the line to a `from_launcher` or `from_runtime_log` test. Do not guess SGLang markers.

## Pitfalls
- **A knob the gate priced was dropped by the template.** `--kv-cache-dtype` was absent, so fp8 was budgeted and fp16 ran. Nothing errored. Find it by diffing what `render_command` prints against what the verdict card says.
- **A refusal that spells the same fact in six places.** `addressable_memory <= 0` stood for "cannot serve" until a CPU runtime split it. Add a predicate, not another inline test.
- **A hub GGUF id has no `hf://` scheme once resolved.** Two functions tested for the scheme and refused or mistranslated every legitimate repo. Test both spellings: `control_plane/deploy/flags.py::is_hub_gguf_blob`, `control_plane/deploy/flags.py::llamacpp_model_spec`.
- **`llama-server` is not on PATH in the upstream CPU image, and the entrypoint is cleared.** Both llama.cpp templates prepend `/app` to `PATH` for that reason.
- **sparkrun's `gpus: "all"` default is fatal with no NVIDIA runtime.** The recipe's `executor_config:` must override it, and `""` must be quoted or YAML reads null and falls through to the default.
- **sparkrun upgrades broke the handle.** The cluster id gained a second hex segment and `CLUSTER_ID_RE` lost a live workload. When a regex stops matching, read sparkrun's source. See `control_plane/deploy/sparkrun.py::CLUSTER_ID_RE`.
- **Cross-node expert parallel needs sparkrun >= 0.3.7.** vLLM is fine; the launcher never starts rank 1. It is a version gate: `control_plane/deploy/flags.py::data_parallel_refusal`.
- **A dead engine does not kill its container.** A solo launch keeps the container sleeping, so check-job says running after the engine exited. The runtime's own fatal line is the evidence; `sparkrun logs` follows and must be subscribed to, never polled.
- **A stale log tail hid the root cause.** The tail was 60 lines and the real ValueError sat 40 above. `LOG_TAIL_LINES` is 300; keep it larger than the longest "see above".
- **Serial backend probing grew without bound.** 16 s at six deployments against a 15 s death bound. Probes run on daemon threads with a hard join deadline; a `ThreadPoolExecutor` was rejected because shutdown waits.
- **A rung that measured fine reported itself unavailable and never ran.** Call `available()` on a real box.
- **The NCCL bootstrap picked an unroutable /30 and hung until timeout,** which reads like a fabric fault. Name the interface.
- **The staleness window is in two places.** `STALE_AFTER_S` in `record.py` and `STALE_LINK_AGE_S` in `internal_api.py` are separate copies; moving one moves nothing on screen.
- **Auto-calibration made UI copy false.** `measure()` now calibrates a never-calibrated pair, so "about a minute" became several. Any change to what `measure()` does must recheck that sentence.
- **Port checks are local by design.** `port_is_free` cannot see a port held on the node being placed on; the identity probe is what protects a deployment.

## Functions
- `control_plane/deploy/manager.py::DeploymentManager.launch` -- the only way to start a deployment; refuses, renders, runs, returns at LAUNCHING.
- `control_plane/deploy/manager.py::DeploymentManager.stop` -- stop with confirmation and escalation; the only signalling path.
- `control_plane/deploy/manager.py::DeploymentManager.reconcile` -- re-derive states from evidence after a restart; never marks dead on "could not ask".
- `control_plane/deploy/manager.py::DeploymentManager._allocate_port` -- port from the counter, then bound over a 200-port window.
- `control_plane/deploy/manager.py::DeploymentManager._emit_fit_miss` -- the post-gate OOM telemetry; keep calling it from new failure paths that look like OOM.
- `control_plane/deploy/manager.py::port_is_free` -- binds a port locally; reuse, do not re-implement.
- `control_plane/deploy/fsm.py::LEGAL` -- the transition table; illegal edges raise.
- `control_plane/deploy/recipes.py::synthesize` -- plan to recipe, pure; start here for any "what reaches the runtime" question.
- `control_plane/deploy/recipes.py::check_recipe_identifiers` -- identifier gate run before any record exists.
- `control_plane/deploy/sparkrun.py::SparkrunAdapter.render_command` -- the exact argv, no side effects; what the UI shows before commit.
- `control_plane/deploy/sparkrun.py::SparkrunAdapter.stream_logs` -- the log subscription (a thread, not a poll).
- `control_plane/deploy/sparkrun.py::is_oom` -- classifies output as a memory failure via `OOM_SIGNATURES`.
- `control_plane/deploy/health.py::probe` -- is the backend answering and is it ours (identity, not just 200).
- `control_plane/deploy/progress.py::explain_failure` -- turns raw launcher output into a sentence naming the cause.
- `control_plane/deploy/flags.py::runtime_spec` -- look up a `RuntimeSpec`; use it rather than branching on runtime names.
- `control_plane/deploy/flags.py::parse_nccl_env` -- parse `DERATE_NCCL_ENV` into a dict; pair with `nccl_env_refusal`.
- `control_plane/deploy/flags.py::sparkrun_binary` -- resolves `DERATE_SPARKRUN_BIN`; use it for any sparkrun call.
- `control_plane/deploy/flags.py::launcher_does_data_parallel` -- version-gate fact behind `data_parallel_refusal`.
- `control_plane/deploy/reap.py::survey` -- classify containers on a node as ours, orphaned or stray.
- `control_plane/registry/registry.py::Registry.allocatable_or_none` -- live budget or `None`; use this for a gate, not `available_memory`.
- `control_plane/registry/registry.py::Registry.memory_report` -- separates pool figure from GPU figure for an operator staring at a refusal.
- `control_plane/registry/telemetry.py::read_host_memory` -- MemAvailable-based host reading, three-valued on failure.
- `control_plane/registry/probe.py::probe_local` -- the evidence-based node probe; returns `unknown_profile` rather than guessing.
- `control_plane/registry/startup.py::_open_probe_sink` -- starts the TCP sink on every node; a taken port is a warning, not a failed boot.
- `control_plane/links/service.py::LinkService.measure` -- one pair, serialized; returns `None` when every rung failed.
- `control_plane/links/service.py::LinkService.calibrate` -- per-pair NCCL tuning rows; never raises, records failures.
- `control_plane/links/record.py::pair_key` -- the sorted, undirected key for a link.
- `control_plane/links/stub.py::StubLinkService` -- the day-0 `LinkPort`; has an `unmeasured` mode to exercise the missing case.
- `control_plane/measurements.py::tuning_env` -- the NCCL winner for a pair under the two-size rule.
- `control_plane/runtimes/tts.py::parse_speech_request` -- pure request parsing, so refusals are testable without a model.
