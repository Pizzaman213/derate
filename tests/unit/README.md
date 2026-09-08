# tests/unit

The forty pytest modules. Everything else under `tests/` is a script, a fixture
package or a harness -- [`../README.md`](../README.md) is the front door and
covers those; this document covers the suite.

Run it from the repo root. Nowhere else works:

```bash
python3 -m pytest -q -m "not slow"            # the whole suite, ~2 minutes
python3 -m pytest -q tests/unit/test_fit.py   # one module
```

`pyproject.toml` sets `pythonpath = ["."]` and `testpaths = ["tests"]`, so
`control_plane` and `tests.fixtures` import only when the root is the working
directory. There is no `conftest.py` anywhere in the checkout: shared fixtures
live in `tests/fixtures/__init__.py` and shared fakes are imported from
`test_gateway.py` and `test_registry.py` by name, so every dependency between
test modules is a visible `import` rather than a fixture that appears from
nowhere.

**Every module here opens by naming the class of bug it exists to catch**, not
the code it covers. `test_inventory.py` says it out loud -- "the failures worth
pinning are not 'does SQLite work' but the ones that would leave a screen
looking full while saying something false". That habit is why 36,000 lines of
test are readable, and it is the one convention a new file must keep.

`1,838` `def test_` functions across 40 modules.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `__init__.py` | 0 | makes `tests.unit` a package, so the modules that import each other by dotted name resolve |
| `test_avatars.py` | 194 | publisher avatars: ask once, and never turn a hub failure into "no mark" |
| `test_contracts.py` | 413 | the frozen contracts and the shared fixtures have not moved |
| `test_contracts_manifest.py` | 90 | the generated manifests are regenerated live and compared |
| `test_deploy.py` | 3856 | deployment manager, sparkrun adapter, lifecycle, Docker |
| `test_enrollment.py` | 855 | enrollment tokens, the join paths they must not disturb, and `install.sh` |
| `test_fit.py` | 1170 | the out-of-memory gate, and that every refusal names a term and a fix |
| `test_gateway.py` | 6227 | the composition root, routing, admission, streaming, failover |
| `test_gateway_csrf.py` | 128 | the Origin/Host cross-site write guard |
| `test_gateway_detail.py` | 529 | `ui_detail.py` as pure computation, then its call sites on the wire |
| `test_gateway_models_api.py` | 1296 | the model browser's read surface; the quantization table digit for digit |
| `test_gateway_restart.py` | 325 | `RestartCoordinator` on one loop, with the real backoff count |
| `test_gateway_runtime.py` | 1631 | `app`, `deps`, `proxy`, `openai_api`, `metrics`, `stats`, `settings` |
| `test_gateway_ui_api.py` | 225 | `GET`/`PATCH /api/settings`: a cap nobody can measure is refused |
| `test_gpu_processes.py` | 637 | seeing and ending a process that holds GPU memory, across three layers |
| `test_history_api.py` | 531 | the five `/api/history/*` endpoints at the wire |
| `test_imageprobe.py` | 211 | asking the runtime image what it can load, hermetically |
| `test_integration_kv.py` | 70 | three packages must charge the same MLA cache width |
| `test_inventory.py` | 476 | the model registry's five-source merge and the ways it could quietly lie |
| `test_inventory_api.py` | 167 | `GET /api/models` on the wire, and what it must not shadow |
| `test_links.py` | 930 | link measurement: a subtly wrong figure, and a figure honest about guessing |
| `test_live_memory.py` | 806 | the live-memory budget, from the gate to the capacity answer |
| `test_load.py` | 109 | three SLO guards derived from what the load harness measured |
| `test_loadtest.py` | 609 | the `tests/load/loadtest.py` client's own gates |
| `test_logfiles.py` | 318 | one log folder, two files, nothing leaked |
| `test_model_corpus.py` | 349 | every captured config, every run, compared against the record |
| `test_modelcache.py` | 420 | seeing downloaded weights, and deleting them safely |
| `test_node.py` | 491 | the composition root with every port real |
| `test_node_naming.py` | 395 | a name is never an identity, an absence is never a zero |
| `test_planner.py` | 949 | the plan flips when only the measurement changes -- and greps for literals |
| `test_providers.py` | 2815 | remote providers, headed by `test_no_key_material_in_any_output` |
| `test_registry.py` | 3463 | registry, discovery, join and admission, health, telemetry, the agent |
| `test_resolver.py` | 2182 | an offline half on captured configs, a network half that skips itself |
| `test_settings_store.py` | 233 | `None` and `0.0` stay different instructions; the file beats the environment |
| `test_setup.py` | 450 | first-run setup, and what outranks the flag |
| `test_shell.py` | 338 | the remote-exec hole, and the four gates that make it shippable |
| `test_single_source.py` | 227 | one fact, one home; a derived copy that stops agreeing goes red |
| `test_storage.py` | 372 | disk capacity end to end, and not counting one device twice |
| `test_telemetry.py` | 1246 | journal, collection, archive, rollups, retention -- and never blocking |
| `test_topology_remotes.py` | 210 | provider-served models reaching the cluster screen |
| `test_tts_runtime.py` | 471 | everything about the speech runtime that does not need a GPU |

## `test_avatars.py`

The defect: ~45 publishers on a Models grid, each resolved by the **browser**
against an unauthenticated hub endpoint, on every page load. That is over the
hub's burst limit on its own, so the grid 429ed itself and every card fell back
to two letters until a backoff doubling towards half an hour expired. The fix is
not a better backoff, it is asking once on the coordinator and keeping the
answer. So what is pinned is not "an avatar can be fetched": a publisher is asked
about once even when ninety cards want it at once, a hub non-answer never becomes
"this publisher has no mark", an owner that has not resolved yet is *absent* from
the answer rather than reported as having none, and the deadline does not cancel
the work it was waiting on. The autouse `isolated` fixture rebinds
`avatars._cache`, `_locks`, `_semaphore` and `_running` per test.

## `test_contracts.py`

The tripwire. It tests nobody's component; it tests that the things every
component agrees on have not moved -- the constants (`GB10_ADDRESSABLE`,
`DEFAULT_GUARDRAIL`, `COMM_BUFFER_BYTES`, `EP_EXTRA_BUFFER_BYTES`,
`TP_VIABLE_THRESHOLD`, `EP_VIABLE_THRESHOLD`, `FRAMEWORK_OVERHEAD`), the enums
(`Verdict`, `DeploymentState`, `DeviceClass`, `ParallelismKind`, `RoutingPolicy`,
`TargetKind`), the frozen dataclasses, and the quantization helpers
`bytes_per_param` / `normalize_dtype` / `is_known_dtype`. It also pins the shared
fixtures themselves. If a contract is quietly changed to unblock somebody, this
goes red before integration rather than after. 42 tests. Extend by adding; do not
relax an assertion to make it pass.

## `test_contracts_manifest.py`

Six tests, and none of them restates a contract. Each regenerates from live
source and compares with the checked-in artefact: `manifest.build()` against
`manifest.MANIFEST_PATH`, `routes.build()` against `routes.ROUTES_PATH`, and
`document.render()` against `document.DOCUMENT_PATH`. The only thing they can
catch is somebody changing a contract and not regenerating, and the only way to
fix a failure is to run the command the failure message names -- `_fix()`
prints it, followed by "read the diff before committing, it is the list of
contracts your change moved". A derived file allowed to go stale is worse than no
file: it answers confidently and wrongly. One test asserts the rendered document
names every route both apps answer, because a hand-kept route table here once
fell seventeen product routes behind while still introducing itself as the one
the UI codes against.

## `test_deploy.py`

153 tests over the deployment manager, the sparkrun adapter, lifecycle and
Docker, written as an acceptance list with the criterion in a banner comment
above each block. A `WONT_FIT` verdict never produces a launch attempt and the
returned error is the fit gate's reason verbatim. `render_command` produces the
right sparkrun invocation for pipeline parallel across two hosts -- **verified
against sparkrun 0.2.40 with `--dry-run`**, which is what "verified against real
sparkrun flags" means here. Recipe content is not a place an attacker-controlled
string writes YAML structure. An illegal state transition raises rather than
silently correcting. Killing a backend moves the deployment to FAILED within 15
seconds with a useful `last_error`.

The most valuable single test is `test_oom_after_passing_the_fit_gate_emits_the_full_breakdown`:
when a launch OOMs anyway, the `FIT_MISS` event carries `predicted_verdict`,
`predicted_reason`, `predicted_total` and `usable_per_node` beside the actual
failure, which is the only calibration signal the system produces. Tests needing
a real `sparkrun` or `docker` carry `needs_sparkrun` / `needs_docker` and skip;
`test_image_builds_for_both_architectures` is additionally `slow`, because a cold
`buildx` run is minutes and GB10 is arm64 while the workstation is usually amd64.

## `test_enrollment.py`

The credential that turns "install this machine, then go and click Admit" into
one command. Three things fail differently and are kept apart: the
`EnrollmentStore` itself (minting, `DEFAULT_TTL_S`/`MAX_TTL_S` expiry, use
accounting, revocation, and the 0600 file it persists to); `Registry.handle_join`,
which now accepts two kinds of token meaning different things -- the existing
cluster-token and no-token behaviour must be bit-for-bit unchanged, asserted here
as well as in `test_registry.py` because this is the change that could break it;
and the gateway routes plus the script. `INSTALL_SH` is the repo's real
`install.sh`: the route's body is asserted equal to the file on disk, `sh -n`
parses it, and it is checked executable. Expiry is driven by a hand-cranked
`Clock` -- a time question, not a sleep question.

## `test_fit.py`

57 tests on the out-of-memory gate. Every refusal has to name the term that blew
the budget and a change that would work; a test that only checks the verdict is
not testing the product. Both halves of the package are exercised: the six memory
terms through `memory_breakdown`, `weight_bytes_per_rank`, `activation_bytes` and
`predict_decode_tps`, and the KV arithmetic through `kv_bytes_per_token`,
`kv_cache_bytes`, `kv_divisor` and `stage_fraction` -- the last two being where TP
and PP sharding is kept honest rather than optimistic. `min_nodes_required` is
checked for the `-1` sentinel as well as for counts. `StubFit` is asserted to
satisfy the same port, so a consumer tested against the stub is not tested
against a shape the real calculator never produces.

## `test_gateway.py`

The largest file here at 6,227 lines and 235 tests, and the shared fake library
the rest of the gateway suite builds on. **Every test constructs the gateway with
injected fakes**: `FakeRegistry`, `FakeDeployments`, `FakeProviders`,
`make_deployment`, `make_provider`, `make_node_profile` and `build_deps` are all
defined here and imported by nine other modules. Constructing a gateway with all
stubs must work, and must be how the tests run.

Sections, in order: fakes; a real upstream on a real socket; composition; the
OpenAI surface; audio endpoints; realtime; streaming; routing; admission control;
remote providers; plan; topology and metrics; the headline acceptance -- an
unmodified OpenAI client against the gateway; failover, where a node that dies
mid-request is somebody else's to answer; and the circuit breaker. One test is
`slow`: `test_whisper_transcribes_what_the_tts_runtime_just_said` sends words out
through one audio endpoint and back through the other with nothing in between a
fixture. An image with no audio decoder had passed every gate this project has --
the architecture was in vLLM's support table, the fit gate said it fit, the health
check said serving, `/v1/models` said it answered transcription -- and the only
thing that failed was asking it to transcribe.

## `test_gateway_csrf.py`

Nine tests on `control_plane/gateway/csrf.py`. `/api` has no per-request
credential, so `is_cross_site_write` is the whole of what stands between "a page
you opened" and "a page that rewrote your cluster" for any browser that can route
to the coordinator. The pure-logic half pins the boundaries that matter: a GET is
never blocked, a *missing* `Origin` is allowed because that means a non-browser
caller -- curl, an SDK -- and those are not what the check exists to catch, and a
matching origin passes. The rest goes through `create_app`.

## `test_gateway_detail.py`

Two halves by design. The first is unit tests with no HTTP at all against
`ui_detail.py`'s `admission_blocks`, `provider_spend`, `spend_fields`,
`strength_raw` and `target_counters` -- the point of that module is that it can be
exercised without a gateway, so the edit to `serialize.py` and `internal_api.py`,
files several sessions edit concurrently, stays a call site and nothing more. The
second half pins that the call sites pass the right data through over real HTTP:
a measured link's five annotation keys, a provider's twelve spend keys, and a
routing target's counters, `strength_raw` and `admission_blocks`.
`FakeAccountingProviders` deliberately returns a key-shaped field it has no
business exposing, so the redaction path is exercised rather than assumed.

## `test_gateway_models_api.py`

78 tests on the model browser's read surface, on `capacity_api`'s router. Three
properties carry the weight. That the routes are reachable **with the UI
mounted** -- get that wrong and every other assertion in the file is testing a
page of HTML. That the quantization table on the wire is the one the fit gate
uses, digit for digit: `test_quant_table_matches_the_contract_to_the_digit`
asserts the wire's key set equals `BYTES_PER_PARAM` and every `bits_per_weight`
equals `QUANT_INFO`'s, because a second copy in the browser is a second answer
that can disagree with a refusal. And that a resolver which cannot describe a
model says so, rather than returning an empty shape that reads as "nothing to see
here". The fixture swaps in the resolver package's stub, since the gateway's own
`StubResolver` deliberately cannot answer `resolve_full`.

## `test_gateway_restart.py`

Eight tests exercising `RestartCoordinator` directly against a hand-built
`GatewayContext` and a real `EventBus` -- not through `create_app()`/`TestClient`
-- so every attempt runs on the one loop `asyncio.run()` gives the test, with no
cross-thread timing to race. `_attempt_relaunch` runs for real against the same
`StubResolver`/`StubPlanner`/`StubFit`/`FakeRegistry` fixtures `test_gateway.py`
uses, so the actual planning-and-launch path is exercised rather than a mock.
`MAX_RESTART_ATTEMPTS` is left at its real value of 3 and only
`RESTART_BACKOFF_S` is patched down, so a full exhaustion runs in under a second
without changing the number under test. `FakeDeploymentsWithBus` is a local
subclass on purpose: the shared `FakeDeployments` has no bus, which is why
`_consume_deployment_events` is a no-op across the rest of the gateway suite, and
waking it everywhere would change every other file's behaviour.

## `test_gateway_runtime.py`

51 tests owning `control_plane/gateway/{app,deps,proxy,openai_api,metrics,stats,settings}.py`.
It imports nothing from `test_gateway.py` and duplicates nothing in it, so it has
no dependency on how that file evolves -- which is the point when two sessions
own the two files. The proxy half exercises `UpstreamProxy` with `RETRY_TRANSPORT`
and `RETRY_SERVER_ERROR` explicitly, the deps half covers strict-dependency
construction, and the app half covers serving the built UI as a static mount.

## `test_gateway_ui_api.py`

16 tests on `GET`/`PATCH /api/settings`. The two things worth testing hard are
the ones that separate a control from the appearance of one: **a cap nobody can
measure is refused rather than stored** -- `_AccountingProviders` exists so a
spend cap is enforceable and the refusal path is real -- and that the router is
registered above the `StaticFiles` mount so it is reachable at all. The fixture
points `DERATE_DATA_DIR` at `tmp_path` rather than injecting a store, because the
default path resolution `create_app` performs is part of what is under test.

## `test_gpu_processes.py`

29 tests, three layers, end to end. The fit gate plans against
`nvidia-smi --query-compute-apps`, and until this landed nothing could say what
was behind that number or end it. Exercised here: the node agent that can
actually signal a process (`registry/procs.py`, `registry/telemetry.py`'s
`read_gpu_processes`), the coordinator route that decides whether it may
(`gateway/gpu_procs.py`), and the deployment manager's force tier for when
`sparkrun stop` will not confirm. `procmatch.matches_deployment` and `port_in` are
the matching rules that decide a process belongs to a deployment, and they are
tested apart from the killing.

## `test_history_api.py`

19 tests on the five `/api/history/*` endpoints at the wire.
`grep "api/history" tests/` found nothing before this file existed:
`test_telemetry.py` covers the query functions and the `/agent/journal` leg, and
nothing exercised the HTTP layer between them. Two things only a wire test
catches are here -- that the disabled path returns a structured 503 rather than
an exception, and that the envelope a UI has to branch on (`resolution`, `gaps`,
`truncated`) survives serialization. `NO_FALLBACK_ROUTES` is the three routes with
nothing behind them when telemetry is off; `/api/history/nodes` is excluded
because it has the 300-sample ring to fall back on, and `_RingRegistry` is the
other branch of that truth table since the shipped `StubRegistry` has no
`history()`.

## `test_imageprobe.py`

18 hermetic tests. `docker` is a shell script written into `tmp_path` that prints
what the real one would. The point is not to verify vLLM's registry -- that
changes weekly, which is the whole reason `imageprobe` exists -- but to pin the
three rules that make asking safe, each of which a simpler version got wrong:
never pull, key the cache by **image id** rather than tag, and degrade to the
static table instead of raising. The autouse `_no_probes_leak` fixture calls
`support.clear_probes()` on both sides, because a recorded probe is module state
and would otherwise answer the rest of the suite from a fake registry.

## `test_integration_kv.py`

One test, and it is a cross-package agreement. Three places charge KV bytes for
an MLA shape -- `fit.kv`, `planner.fit_bridge`, and the gateway's
`admission.kv_bytes_per_token_fallback` -- and all three must charge
`mla_latent_dim + effective_mla_rope_dim`, never the latent alone, which
under-counts a DeepSeek-family cache by about 11 percent **in the OOM
direction**. Against the frozen `DEEPSEEK_V3` fixture (`mla_latent_dim=512`,
`mla_rope_dim=64`) all three trace back to the same `(512 + 64) * elem`. Two of
the three report a total across all layers and one reports per layer; that unit
difference is the one legitimate divergence and is asserted explicitly rather
than papered over.

## `test_inventory.py`

24 tests on the model registry, a materialized view of five sources. Every test
names the class of bug it exists to catch, and the ones worth pinning are not
"does SQLite work": a source silently dropping out of the merge, an unreachable
node reading as an empty disk, a fit verdict appearing for a question nobody
asked. `SCHEMA_VERSION` is imported from `inventory/db.py` so a migration that
forgets to bump it fails here. The fakes (`FakeShape`, `FakePlan`,
`FakeDeployment`) are minimal dataclasses, because a registry rich enough to
answer other questions would only make these failures harder to read.

## `test_inventory_api.py`

Nine tests on `GET /api/models`. Two properties carry the weight: that the route
is reachable with the UI mounted, and that it does not shadow the four literal
paths `capacity_api` already owns under `/api/models/` -- the failure that would
arrive months later when somebody reorders `create_app` for an unrelated reason.
The autouse fixture points `DERATE_DATA_DIR` at `tmp_path`, because `create_app`
opens `<data_dir>/models.db` and `data_dir()` re-reads the variable on every call,
so a test without it writes into the developer's real data directory.

## `test_links.py`

66 tests, and they care less about coverage than about two failure modes: a
figure that is subtly wrong, and a figure that is honest about being a guess. The
number this component produces is the input the whole product turns on. Sections
run parsers (`parse_ib_lat`, `parse_ib_write_bw`, `parse_iperf3`,
`parse_nccl_perf`) against captured tool output including a real GB10 pair with
GPUDirect RDMA off whose bandwidth-bound sizes sit near 10.2 GB/s; then
`detect_gdr`, `inspect_ports`, the `NcclMeasurer`, the fallback ladder down
through `IbWriteBwMeasurer` and `TcpMeasurer`, `LinkStore` persistence,
`LinkService`, and `StubLinkService`. `IB_TO_NCCL_RATIO` is imported by name
rather than restated.

## `test_live_memory.py`

34 tests on the live-memory budget, from the gate through the refusal to the
capacity answer. The regression was reproduced on real hardware: **a GB10 holding
92 GiB of foreign process was told a 66 GiB model fit**, because the gate budgeted
against the 107.7 GiB static ceiling rather than the roughly 15 GiB the machine
could actually hand out. Both ends are covered -- `FitCalculator.check(...,
allocatable=...)` and `largest_runnable` directly, and `gateway/livefit.py`
through `create_app` and a `TestClient`, so the two verdicts the backend puts on
the wire are asserted where a UI would read them.

## `test_load.py`

Three SLO guards, and the whole module is `pytest.mark.slow`. Deliberately small:
the full ladder in [`load/`](../load/README.md) is an exploration tool that takes
minutes, while these are the properties that must not silently regress. The
constants are measured headroom rather than aspiration -- the harness derated the
streaming path at 128 concurrent streams, so `STREAMS = 32` sits well inside the
working range and a failure means something genuinely regressed rather than the
box being busy. Run it with `pytest tests/unit/test_load.py -m slow`.

## `test_loadtest.py`

37 tests on `tests/load/loadtest.py`. That script is a client of the published
API and imports nothing from `control_plane`, so nothing else in the suite would
touch it. What is pinned is the handful of things that go wrong silently: a paced
run that sends one request and stops, a prompt a prefix cache can answer,
a rung that blames the target for the harness's own stall, and
`classify_providers` -- the classification that decides whether a run costs
money. `synth_wav`, `build_upload` and `MAX_AUDIO_UPLOAD_BYTES` cover the audio
path; `HAMMER_PROMPT_FALLBACK`, `synth_prompt`, `prompt_tokens_for` and
`max_tokens_for` cover the prompt side.

## `test_logfiles.py`

24 tests, no network and no GPU, guarding the three properties
`control_plane/logfiles.py` claims: a startup that cannot write files still
starts, a key never reaches a file, and the folder is bounded. `REAL_KEY` is a
real-shaped OpenRouter key checked for absence through the `Redactor`. The
handlers go on the **real** root logger, because that is the only place they can
see `gateway.proxy` -- a record logged through a named logger reaches a handler by
propagation, and a stand-in root would receive nothing. The autouse `_clean_root`
fixture calls `logfiles.uninstall()` and restores the level whatever the test did,
which is what keeps that out of the rest of the suite.

## `test_model_corpus.py`

23 tests, and the inverse of the resolver's other files: where those each pin one
behaviour with a config chosen to show it, this resolves *everything* in
`resolver_data/` and compares the whole result against `EXPECTED.json`. No config
changes outcome quietly in either direction -- a model that stops resolving fails,
and a model that starts resolving fails too, because that is a change somebody
should look at before it ships rather than after. It exists because this build was
wrong in both directions in one week: `Gemma4ForConditionalGeneration` was refused
by a stale architecture list while the pinned image could load it, and
`Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` was refused by a stale list of config *key*
names. Both were found by a person pasting an error into a chat window, which is
not a regression net.

The autouse `_static_tables` fixture clears any recorded image probe, so the
corpus describes this checkout rather than whichever vLLM image is pulled on the
box. `TestAgainstTheImage` is `slow` and needs the pinned image; when the image is
absent those tests report that they could not look, never that they looked and
agreed. Adding a config fails this test until `--regenerate` is run, which is
deliberate: an un-recorded fixture is a file that runs and asserts nothing.

## `test_modelcache.py`

21 tests. Model weights are the largest thing on a serving box by orders of
magnitude -- **894 GiB across 51 repositories on the machine this was written
against, one of them 182 GiB** -- and nothing in the product could see them, let
alone reclaim them. The delete is the dangerous half, so most of the file is about
what must not happen: deleting outside the cache, deleting a model something is
serving, or deleting on an uncredentialled request. `make_repo` builds a
repository in the hub cache's real layout, blobs holding the bytes, because a
flat fixture would not exercise the path arithmetic that makes the delete safe.
This file also carries the original `internal_api._TERMINAL_STATES == fsm.TERMINAL`
assertion that `test_single_source.py` generalised.

## `test_node.py`

13 tests on the composition root. **Every port a coordinator builds here is
real** -- the same `Registry`, `LinkService`, `ModelResolver`, `FitCalculator`,
`Planner`, `DeploymentManager` and `ProviderService` that `node.py` wires in
production. mDNS is never exercised: `DERATE_ROLE` is always set explicitly and
`resolve_role` returns immediately for `coordinator` without a browse, so these
build a real `Registry` directly rather than going through the full
`start_node` -> advertiser -> telemetry pipeline, which buys speed without giving
up a real component the gateway depends on. No port is ever a bound socket:
`create_app` goes through `TestClient`'s in-process ASGI transport and the
signal/serve-loop plumbing runs against fake `uvicorn.Server` stand-ins.

## `test_node_naming.py`

26 tests over two features that arrived together because they answer the same
operator question -- "which machine am I looking at, and is it actually talking to
the others?" -- from opposite ends. The rule both share: **a name is never an
identity, and an absence is never a zero.** A rename must not move `node_id`,
because deployments, links and routing on disk are keyed by it; and a reachability
leg that did not answer must not carry a millisecond figure, because `0 ms` beside
"unreachable" reads as a fast link -- the same error `links/measure.py` refuses to
make with bandwidth. `normalize_label`/`MAX_LABEL_LEN` cover the naming half;
`dial`, `summarize`, `unknown_leg`, `validate_target` and `UnusableTarget` cover
the reachability half.

## `test_planner.py`

56 tests, and two of them are the product thesis in executable form.
`test_answer_flips_when_only_the_measurement_changes` takes the frozen 10.2 GB/s
fixture, changes two fields, and asserts the plan inverts.
`test_no_bandwidth_is_hardcoded_in_the_planner` **greps every `*.py` in
`control_plane/planner/`** for a literal shaped like a measured bandwidth --
`r"(?<![\w.])([1-9]\d\.\d{1,2}|9\.0)(?![\w])"`, one or two digits, a decimal
point, one or two more -- skipping comments and docstrings. The pattern is shaped
that way so it does not fire on the package's legitimate small constants: byte
widths (`2.0`, `4.0`), ratios (`0.99`), fractions of one. If either test fails,
the product thesis has quietly stopped being true. `test_thresholds_come_from_contracts`
is the companion: the planner must not keep its own copy of `TP_VIABLE_THRESHOLD`
or `EP_VIABLE_THRESHOLD`.

## `test_providers.py`

133 tests over remote providers, in nine numbered sections: registry and
discovery; secrets; forwarding; health, rate limits and budget; spend and budget;
route targets; port conformance and the day-0 stub; lifecycle; and the allowlist
of what a provider is willing to serve.

**The headline is `test_no_key_material_in_any_output`.** A leaked key in a
screenshot is unrecoverable, and this is a tool people screenshot. The test
configures a provider with a resolvable real key, asserts `resolve_key` really
returns it so any leak would be a real one, then serializes every response shape
the package can emit -- `ProviderPort.list`, `public_list`, `public_dict`,
`public_models`, `catalogue`, `servable`, route targets, health, the persisted
record on disk, an upstream error that echoes the key back, and the log stream at
DEBUG -- and asserts the key is in none of them. The 401 body quoting the
credential back is not hypothetical: providers really do that, and it is the path
that would put a live key into a client-visible error body. Async tests run
through a local `run()` helper rather than a pytest plugin, so the suite needs
nothing beyond pytest and httpx.

## `test_registry.py`

206 tests in thirteen numbered sections: the hardware probe; networking;
cluster identity and the token; join, candidates and admission; manual add;
health; telemetry and the ring buffer; the node agent; role resolution; the
two-containers-no-configuration demo; serialisation; the day-0 stub; and two node
agents over real HTTP. No network, no mDNS and no GPU are required; the two tests
that can use real hardware skip themselves when it is absent.

The probe section is where the platform rule is enforced: GB10 uses the contract
constants rather than `nvidia-smi` (which reports aggregate memory as `[N/A]` on
that hardware -- see `GB10_ROWS`), GB10 addressable is not the nameplate, an
unknown card returns zero bandwidth rather than a guess, a machine with no GPU is
CPU rather than unknown, and NVIDIA hardware is found even with no `nvidia-smi`
present. `FakeClient`, `make_registry` and `run` are exported and imported by
`test_enrollment.py`.

## `test_resolver.py`

162 tests in two halves. The offline half runs against the captured
`config.json` payloads in `resolver_data/` and never touches the network; the
network half is **skipped automatically when the hub is unreachable**, and
`DERATE_TEST_NETWORK=0` skips it deliberately. `needs_hub` is the marker. The
nineteen offline classes are the map of what the resolver has to get right --
`TestFieldMapping`, `TestCompositeConfigs`, `TestMixtureOfExperts`,
`TestAttentionVariants`, `TestParameterAccounting`, `TestQuantDetection`,
`TestWeightIndexArbitration`, `TestLocalDirectory`, `TestSpeechModels`,
`TestQuantLadderCoverage`, `TestArmRepackAndLSuffixes`, `TestHeaderProbeBudget`
and the rest. `TestGGUF` writes its GGUF files by hand so the file needs no
encoder.

## `test_settings_store.py`

16 tests on the persisted mutable-settings layer. Two properties a reasonable
implementation gets wrong: that `None` and `0.0` stay **different instructions**
for the spend cap -- no cap versus a cap of zero -- and that the file beats the
environment rather than the other way round. `MUTABLE_FIELDS` is asserted to be
exactly four names against a `GatewaySettings` with 42 fields, because persisting
all of them would let a stale file pin a code default forever. `SCHEMA_VERSION`,
`SettingsError` and `resolve` round out the surface.

## `test_setup.py`

28 tests on first-run setup: the flag, what outranks it, and the endpoint over
both. The environment is pinned rather than inherited -- `DERATE_DATA_DIR` decides
where the flag file lands, and a test that let it default would write into
whatever `paths.data_dir()` resolves on the developer's box, passing or failing
according to which machine ran it. `SetupStore`, `Completion` and `is_complete`
are the surface. The fakes are deliberately minimal: this module answers one
question, and a registry rich enough to answer others would only make its
failures harder to read.

## `test_shell.py`

17 tests, and every one is about a gate rather than a feature. The feature is four
lines of `pty.fork`; what earns its place in the product is that the shell is
absent unless asked for, refuses without a secret the network cannot obtain,
refuses a handshake from a page we did not serve, and cannot leave a root process
behind. `registry/procs.py` opens by saying "three rules keep this a narrow verb
rather than a remote-exec hole" -- this **is** that hole, opened deliberately, and
these are the rules that replace those.

## `test_single_source.py`

Six tests, and the pattern is: one fact, one home, and a red test when a copy
stops agreeing. `test_contracts.py` pins the shapes; this pins everything derived
from them, which is where drift actually happened, because a shape has one obvious
home and a fact about a shape has none. `control_plane/contracts/derived.py` names
every such fact and every site that restates it, and this file does the comparing
via `manifest.resolve`. **Extend it by adding a row to `derived.py`, not by adding
an assertion here.** `test_the_terminal_state_assertion_that_started_this_still_holds`
keeps the seed -- `internal_api._TERMINAL_STATES == fsm.TERMINAL` -- so deleting
`test_modelcache.py`'s copy does not silently remove the only check on the oldest
known duplicate.

The second half does the same for environment variables, which have no import
graph at all: a name is a string in four languages and nothing fails when two of
them disagree. `_ENV_NAME` walks every `.py`, `.sh`, `.ts`, `.tsx`, `.mjs`,
`.yaml`, `Dockerfile` and `install.sh` in the tree for `DERATE_*` and requires
`envspec.is_declared`. Markdown is excluded on purpose: a document naming a
variable that no longer exists is a documentation bug, and failing this test on it
would put the two problems in one place.
`test_no_two_readers_of_a_variable_disagree_about_its_default` exists because
`docker/placeholder_app.py` defaulted `DERATE_AGENT_PORT` to whatever
`DERATE_PORT` was while the Dockerfile and `registry/config.py` both said 8081, so
an operator who named only the coordinator port got an agent on top of it and no
error either way.

## `test_storage.py`

20 tests, three layers: the probe that reads the filesystem
(`registry/storage.py`), the agent route that serves it, and the coordinator
fan-out that has to survive a node it cannot reach. Nothing in the product knew
what a filesystem held, so a launch could fail on a full disk with no warning
anywhere. **The regression that matters most is the first one**: our paths usually
share a device, and adding their usage up reports the same bytes several times.

## `test_telemetry.py`

53 tests over journal, collection, archive, rollups and retention, in eight
sections. No network and no GPU. The one thing every test is really guarding is
that **a telemetry failure stays a telemetry failure**: nothing in this subsystem
may block a caller, break a request, or leak a key. The surface is broad and
imported by name -- `Journal`, `Archive`, `Telemetry`, `Hist`/`merged`, the query
functions `q_nodes`, `q_requests`, `pick_step` and `resolve_window`, the retention
compactor with `STEP_1M`/`STEP_1H`, the log handler's `install`/`uninstall`, and
`GatewayEvents`/`journal_events` with `SOURCE_DEPLOY`. A `FIT_MISS` from
`deploy.events` is exercised through the journal, so the calibration event
`test_deploy.py` emits is followed to where it lands.

## `test_topology_remotes.py`

13 tests. `index.remotes` had been populated since the router was written -- one
entry per model of every enabled provider, created when the provider is added and
needing no traffic at all -- and nothing ever read it. So `/api/topology` knew only
about local deployments, and a model routed to a provider reached the cluster
screen as a substring inside the provider rail's sublabel and as nothing else.
The fact that makes it worth reporting is `node_id`: **a provider can be a machine
on the roster.** The Pi enrols as a GPU-less node and is also registered as an
Ollama provider, and a model pulled onto it runs on a box already drawn on the
screen. It is in its own file rather than more of `test_gateway.py`, which several
sessions edit at once.

## `test_tts_runtime.py`

33 tests covering everything about the speech runtime that does not need a GPU.
`control_plane/runtimes/tts.py` runs inside the model container where torch,
transformers, soundfile and scipy live; none are in `requirements.txt`, so the
module keeps them behind function-local imports and this file holds on a machine
that has none of them. That is the point: **the refusals this server writes are
the part a person reads, and they must be testable without hardware.** `wav_bytes`
builds a real minimal mono 16-bit WAV by hand, because the runtime reads clip
durations with libsndfile and the fixture has to be something libsndfile will
open. What is not covered, and cannot be: that the checkpoint loads and speaks --
that was verified by running the server against `Audio8/Audio8-TTS-Preview-0.6b`
on a GB10 and playing the result.

## The seam between test modules

There is no `conftest.py`. Two files are libraries as well as suites, and
importing from them is the convention:

```python
from tests.fixtures import SPARK_01, GPT_OSS_120B, LINK_SPARK_10G, fits, wont_fit
from tests.test_gateway import FakeRegistry, build_deps, make_deployment
from tests.test_registry import FakeClient, make_registry, run
```

`test_gateway.py`'s fakes are imported by `test_gateway_detail.py`,
`test_gateway_models_api.py`, `test_gateway_restart.py`, `test_gpu_processes.py`,
`test_inventory_api.py`, `test_live_memory.py`, `test_loadtest.py`,
`test_topology_remotes.py` and `test_enrollment.py`. `test_gateway_runtime.py`
deliberately imports **nothing** from it, so the two files can be owned by two
sessions without one's refactor breaking the other.

`tests.model_sweep` is imported as a library by `test_model_corpus.py`, which is
what makes the script and the gate the same code rather than two implementations
of the same check.

## Things that look like details and are not

**Deselected is not skipped.** `-m "not slow"` deselects the image-dependent
tests rather than starting a 24 GB container inside the suite, and the run says it
did not check them. Four modules carry the marker: `test_load.py` entirely,
`test_model_corpus.py::TestAgainstTheImage`,
`test_deploy.py::test_image_builds_for_both_architectures` and
`test_gateway.py::test_whisper_transcribes_what_the_tts_runtime_just_said`. A
skip that reports green is the failure these are shaped to avoid --
`TestAgainstTheImage` says so in its own docstring, naming `ui/check.mjs` one
directory over as the same principle.

**The environment is pinned, never inherited.** Any module that touches persisted
state sets `DERATE_DATA_DIR` to `tmp_path` -- `test_setup.py`,
`test_gateway_ui_api.py` and `test_inventory_api.py` all do it in a fixture -- and
each explains why: `paths.data_dir()` re-reads the variable on every call, so a
test that let it default passes or fails according to which machine ran it.

**Module state is reset by autouse fixtures, in pairs.** `test_imageprobe.py`
clears `support`'s recorded probes on both sides of the yield, because a probe
recorded in one test answers the rest of the suite from a fake registry.
`test_avatars.py` rebinds four module globals. `test_logfiles.py` uninstalls its
handlers off the *real* root logger in a `finally`. Each of these guards a
cross-test leak that would show up as an unrelated failure somewhere else.

**Async is run, not plugged in.** Several modules define a three-line
`run(coro)` that calls `asyncio.run`, and say why: it avoids a hard dependency on
`pytest-asyncio`. `pyproject.toml`'s dev extra is `["pytest>=9", "ruff"]` and
nothing else, so the suite runs on a checkout with no plugin resolution at all.

**Streaming is tested against a socket.** `test_gateway.py` stands up a real
uvicorn server (`RunningServer`, `RunningBackend`, `_free_port`) rather than
mocking the upstream, because the failures that matter in the proxy -- a
disconnect mid-stream, a cancelled scope, a half-closed connection -- do not exist
in a mock.

**A grep is a legitimate assertion when the property is "no literal exists".**
`test_planner.py` reads `control_plane/planner/*.py` and fails on a
bandwidth-shaped number; `test_single_source.py` reads the whole tree for
`DERATE_*`. Neither property is expressible as a call.

**The three generator commands are the fix, not the failure.** When
`test_contracts_manifest.py` goes red, run what the message names:

```bash
python3 -m control_plane.contracts.manifest --write
python3 -m control_plane.contracts.routes --write
python3 -m control_plane.contracts.document --write
```

