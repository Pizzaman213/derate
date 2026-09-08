# contracts

The shapes every other package codes against, and three generators that dump
them so nothing has to restate them by hand. Fifteen Python files, 1,729 lines,
imported by 64 modules under `control_plane/` — every subpackage except
`inventory/`, `runtimes/` and `telemetry/`, none of which name a contract at
all. Nothing here imports back out, and that asymmetry is the whole design.

Changing something here is not a refactor. One person changes a contract,
announced, in one place. Editing a field to unblock yourself is the project's
one documented failure mode, and the reason is arithmetic: 51 files under
`control_plane/` open with `from control_plane.contracts import`, 70 across the
whole tree once the tests are counted, and a widened enum or a renamed field
lands in every one of them at once.

The other half of the job is copies. Every definition here is written exactly
once, and it was never the definitions that drifted — it was the TypeScript
union mirroring an enum, the second byte formatter in another module, the route
table someone maintained in prose. `derived.py` names each such fact and every
site that restates it; `manifest.py` and `routes.py` reflect the live objects
into JSON rather than describing them; `tests/test_single_source.py` and
`tests/test_contracts_manifest.py` go red when a copy stops agreeing.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `constants.py` | 19 | the ten frozen numbers: GB10 memory, the viability thresholds, the buffers |
| `hardware.py` | 161 | `DeviceClass`, `NodeProfile`, `NodeState`, `GpuProcess`, `LinkMeasurement` |
| `model.py` | 84 | `ModelShape` — GQA, MoE, sliding window and MLA in one frozen record |
| `quant.py` | 310 | `BYTES_PER_PARAM`: real bytes per parameter, scales included, plus 70 alias spellings |
| `plan.py` | 110 | `ParallelismKind`, `ParallelismPlan`, `Verdict`, `MemoryBreakdown`, `FitResult`, `FitRequest` |
| `deployment.py` | 56 | `Deployment` and the seven `DeploymentState` values |
| `providers.py` | 57 | `Provider`, `ProviderModel`, `ProviderKind`; `api_key_ref` is a reference, never a value |
| `routing.py` | 45 | `RoutingPolicy`, `TargetKind`, `RouteTarget`, `RoutingConfig` |
| `modality.py` | 37 | `Modality` and `ENDPOINT_FOR_MODALITY` — which endpoint family a model answers on |
| `ports.py` | 88 | the seven `Protocol`s components hold each other to |
| `derived.py` | 155 | facts derived from the shapes, their canonical home, and every site restating them |
| `manifest.py` | 220 | reflects types, enums, constants, derived facts and env into `manifest.json` |
| `routes.py` | 144 | dumps every method+path both Starlette apps answer into `routes.json` |
| `document.py` | 149 | renders the two manifests into one markdown lookup surface |
| `__init__.py` | 94 | 41 re-exported names; the import path every other package uses |
| `manifest.json` | 1732 | **generated.** 30 types, 14 constants, 8 derived facts, 63 environment variables |
| `routes.json` | 535 | **generated.** 77 gateway routes, 11 node-agent routes |

## `constants.py`

Ten numbers and nothing else. `GB10_TOTAL_MEMORY` (128 GiB) is the nameplate;
`GB10_ADDRESSABLE` (119.7 GiB) is the GPU-reachable slice, and the two are
deliberately different values rather than one with a comment. `GB10_MEM_BANDWIDTH`
is 273.0 GB/s intra-node.

`TP_VIABLE_THRESHOLD` and `EP_VIABLE_THRESHOLD` are both 40.0 GB/s — the
all-reduce rate below which tensor parallel loses to pipeline when batched, and
below which cross-node expert parallel is refused outright. They are separate
constants at the same value because they answer different questions and will
not move together. `DEGRADED_TPS_THRESHOLD` (10.0 tok/s) is what makes
`FITS_DEGRADED` a verdict rather than a warning. `DEFAULT_GUARDRAIL` (0.90),
`COMM_BUFFER_BYTES` (1.5 GiB), `EP_EXTRA_BUFFER_BYTES` (2.0 GiB) and
`FRAMEWORK_OVERHEAD` (1.0 GiB) are the terms the fit inequality charges before
it has looked at a model.

## `hardware.py`

`NodeProfile` is the hardware as probed — frozen, with `usable_memory(guardrail)`
and `describe()` on it. `NodeState` is that profile plus what is true right now,
and the split is enforced by which side a field lands on.

**`DeviceClass.CPU` and `DeviceClass.UNKNOWN` are not the same answer.** CPU
means we looked and found no GPU: a Raspberry Pi, a NAS. UNKNOWN means we could
not look — a container started without `--gpus` has no `nvidia-smi` and is
indistinguishable from bare metal by that test alone. Collapsing them is what
let a misconfigured DGX Spark sit in the roster reading like a Pi.
`registry/probe.py` separates them on evidence the kernel still exposes without
the GPU: `/proc/driver/nvidia` and the PCI vendor id. `unified_memory` is a
property covering GB10 and APPLE, and is deliberately *not* a substitute for the
`is DeviceClass.GB10` checks in `registry/telemetry.py`, which gate accounting
built on GB10 constants.

Four `NodeState` fields are additive and each records a bug. `memory_total` is a
live denominator: a machine with no GPU has `profile.total_memory` 0, so its
memory readout was a permanent em dash — and it is kept off `NodeProfile`
because that field is summed into cluster-wide totals, where host RAM no model
can reach does not belong. `sample_ts` exists because `last_seen` is refreshed
by every answered `/agent/health`, so a node whose `nvidia-smi` is gone keeps a
fresh `last_seen` while its four live readings are frozen — two questions ("is
it reachable" / "is this number current") needing two timestamps, or the UI
shows an hour-old wattage as live. `is_local` is set by `Registry.enroll_local`
and by nothing else; it is on the state rather than the profile because moving
the coordinator to another machine leaves the hardware unchanged while this
flips. `build` separates what the hardware is from what was doing the looking:
the two were conflated by absence until a Raspberry Pi running an old image
reported itself as unidentified hardware, and the roster had no way to say the
machine is fine and the software is stale.

`GpuProcess` is read on demand when an operator opens a node, never on the 5s
poll, so the durable journal does not carry a process list nobody reads.
`command` and `user` are `None` when `/proc` could not be read — a container
without `--pid=host` sees neither the processes nor their entries, and inventing
a name would be worse than admitting we could not look. `LinkMeasurement` keeps
`all_reduce_gbps` and `sendrecv_gbps` apart: the first governs tensor parallel,
the second pipeline handoff and KV transfer.

## `model.py`

One frozen dataclass carrying everything the fit gate and the planner need, and
nothing about weights on disk. `num_kv_heads` is annotated `NOT
num_attention_heads. GQA ratio matters.` in the source, because that single
substitution over-charges Llama 3.3 70B's cache by 8x.

Four families of optional fields ride on the base shape: MoE
(`num_experts`, `num_experts_per_token`, `active_params`), sliding window
(`sliding_window`, `layers_with_full_attention`), MLA (`mla_latent_dim`,
`mla_rope_dim`) and `vision_params`, which is replicated per node when sharding
and never split.

`effective_mla_rope_dim` is the one property with an argument in it. All KV-cache
math must charge `mla_latent_dim + effective_mla_rope_dim`, never the latent
alone; when the config did not carry `qk_rope_head_dim` it falls back to 64, the
width every known DeepSeek-family checkpoint uses, because charging it errs in
the OOM-safe direction. `bytes_per_param()` raises `KeyError` naming
`control_plane/contracts/quant.py` rather than defaulting, so an unpriceable
dtype surfaces here instead of downstream as a number.

## `quant.py`

**Real bytes per parameter, including block scales and zero points — not
nominal bit width.** Nominal bit width is the systematic error in every napkin
calculator: Q4_K_M costs 4.90 bits per weight once its super-block scales and
mins are counted, not 4.00, and MXFP4 costs 4.25 because every 32 elements carry
an E8M0 scale. `BYTES_PER_PARAM` has 33 entries and `ModelShape.dtype` is always
one of its keys. `QUANT_INFO` carries the same 33 with `bits_per_weight`, family,
`native_compute_capability` and whether the scheme still runs emulated below it —
`nvfp4` is Blackwell-only with no pre-Blackwell kernel; `mxfp4` runs on Hopper
and Ada by dequantizing in kernel, and pre-Hopper runtimes upcast to bf16, which
quadruples it.

The `q4_1`/`q5_0`/`q5_1` block formats were priced as `q4_0` at 4.5 bpw until
their real ggml block sizes were read out of `resolver/gguf.py`'s `GGML_TYPES` —
an under-count of up to a third, the one direction this table is not allowed to
be wrong in. The twelve IQ entries carry llama.cpp's own *measured whole-model*
bpw, not block arithmetic, because a real file keeps its embedding and output
tensors at higher precision and the measured figure is the safe one.

`normalize_dtype()` maps 70 alias spellings, case-insensitively and ignoring
separators, and returns `None` rather than a default so callers decide whether
an unknown value deserves a warning. `bytes_per_param()` raises on an unknown
dtype; `bytes_per_param_or_default()` charges `DEFAULT_DTYPE` (bf16) for the
paths that must produce a number. Neither ever guesses low.

**The `UD-` aliases identify a family and are not a size.** Unsloth Dynamic names
a per-tensor mix with no fixed bits per weight. Measured against the 27 real
files of `unsloth/Qwen3-30B-A3B-GGUF`, mapping onto the base rung lands between
15% under and 7% over, and it is worst at the bottom of the ladder:
`UD-Q4_K_XL` +5.6%, `UD-Q6_K_XL` -5.0%, `UD-Q8_K_XL` -9.9%, `UD-Q2_K_XL` -15.0%,
`UD-IQ1_S` -15.4%. Negative is the direction this table must never be wrong in,
so anything sizing a UD variant uses the real file size — the hub reported one
for all 27. Rounding up a rung is not the fix either: it would overstate
`UD-Q4_K_XL` by 23% and still under-call `UD-IQ1_M`.

## `plan.py`

`ParallelismKind` names the five shapes a plan can take — `SINGLE_NODE`,
`TENSOR`, `PIPELINE`, `EXPERT`, `HYBRID` — and `ParallelismPlan.world_size`
multiplies `tensor_parallel * pipeline_parallel * data_parallel` and
deliberately not `expert_parallel`, which is a sharding of the experts inside a
rank rather than another rank.

`ParallelismPlan` is frozen and carries its own justification: `reason` is one
sentence shown verbatim in the UI, `measured_link_gbps` is what the decision was
based on, and `rejected` lists the degrees that lost and why
(`"TP=2: link 10.2 GB/s below 40 GB/s threshold"`). A plan that cannot say what
it rejected is not a plan.

`MemoryBreakdown` is the six terms and a `total` property; `FitResult` pairs it
with a `Verdict`, `headroom`, a `limiting_term` drawn from
`"weights"|"kv_cache"|"bandwidth"|"combined"|"context"`, and `ok`, which is
`verdict is not WONT_FIT` — so `FITS_DEGRADED` reads as a yes. `budget_basis`
is trailing and defaulted to `"static"`: `Deployment.fit` is persisted, and
without it a stored verdict could not say which budget judged it.

`FitRequest.weight_bytes` is the resolver's measured on-disk total, which the
calculator must prefer over `total_params * bytes_per_param`.
`native_window` is the model's own `max_position_embeddings`. A context past it
is not a memory question — vLLM's config validation refuses to start however
much GPU is free — so the gate refuses before a launch reaches the runtime.
`None` there must never be read as "no limit"; callers pass it through unclamped.

## `deployment.py`

`Deployment` is the durable record: the shape, the plan and the fit result that
authorized it, plus `served_name`, `backend_url`, `context_length`,
`max_concurrent_seqs` and `state`. `DeploymentState` has seven values, and
which of them mean "it is over" is deliberately not answered here — see
`derived.py`.

`runtime` is a plain `str`, and its annotation named two runtimes long after the
third shipped. `deploy/flags.py::SUPPORTED_RUNTIMES` is the list that decides
(`vllm`, `sglang`, `tts`), and `tests/test_single_source.py` holds
`resolver/support.py`'s table to the same names.

Three fields are trailing and defaulted so every record written before they
existed still decodes: `modality`, `extra_args` and `custom_command`.
`extra_args` appends caller-supplied CLI tokens to the generated serve command;
`custom_command` replaces the plan-derived flags instead of appending, and the
two are mutually exclusive — `deploy/recipes.py::synthesize` refuses both at
once, and both go through `check_extra_args_safe` first.

## `providers.py`

`Provider` describes a remote upstream as a route target. **`api_key_ref` holds
a reference — an environment variable name or a secret key — never a value.**
That is stated in the module docstring and on the field, and every redaction
seam in the tree exists to keep it true.

`ProviderKind` has seven members. `OLLAMA` carries the one annotation that
reverses an earlier decision in place: another box on the LAN, which we do not
launch, supervise or kill anything on — but we do ask it to fetch a model by
name through `POST /api/providers/{id}/pull`, which the original note here said
we never would. `CUSTOM` is any OpenAI-compatible `base_url`.

`ProviderModel.modality` defaults to `TEXT` and is inferred from the upstream id
by `providers/discovery.py`. That inference is a heuristic, so it only ever moves
a model off the default when the id says so plainly.

## `routing.py`

Seven `RoutingPolicy` values, `LEAST_OUTSTANDING` the default and local-only.
`RouteTarget` is one candidate for one served name, and it keeps three fields
apart that are easy to collapse into one: `healthy` is whether the target is up,
`admitting` is whether it will take work right now (false when memory is
critical, draining, or rate limited), and `weight` is a `float`, a normalized
0..1 share only `WEIGHTED_CAPACITY` reads. Two booleans and a share, not one
health flag. `target_id` is a `deployment_id` for a local target and
`f"{provider_id}:{upstream_id}"` for a remote one, which is why `TargetKind`
has to travel beside it. `RoutingConfig.sticky_ttl_s` is
`CACHE_AFFINITY` only, and 0 disables stickiness.

## `modality.py`

**The axis that tells a TTS model from a chat model.** The gateway had one
endpoint family, so "which model" was the only question a request had to answer.
With `/v1/audio/*` there are two, and nothing in the system could distinguish
them — a provider catalog already ingests `whisper-1` and `tts-1` as ordinary
models and they surface in `/v1/models` beside the chat ones.

`Modality` is deliberately about *the endpoint family a model answers on*, not
about the model's internals: that is the only distinction the router needs to
keep a chat request off a speech deployment. Every field carrying one defaults
to `TEXT`, so every record written before this existed still decodes and a
runtime that never says otherwise behaves exactly as it did.

`ENDPOINT_FOR_MODALITY` maps each of the four values onto its path. It exists so
a client that names a model on the wrong route is told which one they wanted:
"no such model" would be a lie, and a bare refusal is unactionable.

## `ports.py`

Seven `Protocol`s — `RegistryPort`, `LinkPort`, `ResolverPort`, `FitPort`,
`PlannerPort`, `ProviderPort`, `DeploymentPort` — every one `@runtime_checkable`,
so `assert isinstance(Planner(), PlannerPort)` is a real test.
`tests/test_planner.py` makes it against both `Planner` and `StubPlanner`, and
`tests/test_deploy.py` makes the same assertion against `DeploymentPort`.
Everyone codes against the protocol, not the implementation: `gateway/deps.py`
types its seven port fields by protocol and carries nothing else but `strict`
and `settings`.

Two signatures carry their own history. `LinkPort.measure` returns
`LinkMeasurement | None`, and `None` means every measurement rung failed — an
honest absence, never a fabricated figure. `DeploymentPort.launch` takes
`modality`, `extra_args` and `custom_command` keyword-only and defaulted,
precisely so an implementation predating any of them keeps satisfying the
protocol.

`FitPort.check` declares `allocatable` keyword-only with a default; `max_context`
declares five positional parameters and no keywords at all. Deviating from that
signature is a real cost paid elsewhere: `fit/capacity.py::context_for` probes
its argument with `inspect.signature` rather than `try/except TypeError`, because
the exception form would also swallow a genuine `TypeError` raised inside the
port.

## `derived.py`

`contracts/` holds the shapes. This holds the other half — facts *derived* from
them that several components need and that therefore got written down more than
once.

The classic is the terminal-state set. `DeploymentState` is frozen and correct,
but "which of those states mean it is over" is a judgement about the enum rather
than part of it, so four modules each formed their own answer. `FACTS` is eight
`DerivedFact` rows, each naming a `canonical` `module:attr`, a `why` explaining
what keeps the fact out of `contracts/`, and two lists that fail differently:

- **`copies`** are resolvable `module:attr` names. `tests/test_single_source.py`
  imports each and asserts equality with the canonical value. These cannot drift
  silently.
- **`restated_at`** are sites that inline the fact as an expression —
  `if dep.state not in (STOPPED, FAILED)` — where there is no attribute to
  compare. No test can hold these; listing them means a person changing the
  canonical value can grep for what else has to move. Converting one into an
  import is always the better fix.

`compare` is `"value"` by default and `"members"` where the canonical form is a
tuple of names and the copy is a table keyed by them: the fact they share is
which names exist, and demanding the same container would demand the copy stop
being a table.

The eight rows as they stand: `deployment_terminal_states` and
`deployment_serving_states` (canonical in `deploy/fsm.py`, copied into
`gateway/states.py`, because the gateway cannot import the deploy package —
its `__init__` pulls the manager, the sparkrun adapter and the event bus into a
request module); `runtime_names` (`deploy/flags.py`, because a runtime without a
`RuntimeSpec` cannot be launched whatever else claims to know it);
`binary_byte_formatter` (`humanize.py`, copied as `fit/calculator.py::_gib`, and
kept out of `planner/comm.py` on purpose — that one formats transfer volumes
beside decimal GB/s and is right to stay decimal); `redacted_placeholder`
(`redaction.py`, copied into `providers/config.py` and `gateway/serialize.py`);
`default_agent_port` and `default_coordinator_port` (`registry/config.py`,
restated in five and four places including `install.sh` and `compose.yaml`); and
`node_roles`.

Adding a row is cheap and is the point.

## `manifest.py`

`python3 -m control_plane.contracts.manifest --write`. **It reflects rather than
restates.** `dataclasses.fields()` and `Enum.__members__` already know the
answer, so a generator that asks them cannot itself drift; the only thing that
can go stale is the checked-in `manifest.json`, which is exactly what
`tests/test_contracts_manifest.py` fails on.

`reflect_contracts()` walks every module in this package except
`_NOT_CONTRACTS = {"manifest", "derived", "routes", "document"}` — the modules
that *describe* contracts rather than being them. That exclusion is not
tidiness: `routes.py` exporting `ROUTES_PATH` put a developer's absolute
filesystem path into the manifest on the first run. Only names declared in the
module they were found in are emitted, so a re-export never counts twice.
`reflect_derived()` resolves each `DerivedFact.canonical` to the live object;
`reflect_env()` reads `envspec.VARIABLES`.

`_encode` is where reproducibility lives. Sets are sorted, because a frozenset's
iteration order is not a fact about the contract and a manifest that reordered
between runs would fail its own freshness test on machines that agree about
everything that matters. A callable is rendered as
`<function module.qualname>` and never `repr`'d, because a function's `repr`
carries its memory address. `_type_name` keeps the annotation *as written* —
`from __future__ import annotations` delivers them as strings — because a
contract that changes `int` to `int | None` has changed.

A guard test asserts the reflection actually found something: without it, a
moved import would leave both freshness tests comparing an empty manifest
against an empty manifest and passing.

## `routes.py`

`python3 -m control_plane.contracts.routes --write`. Starlette already knows
every path it will answer, and asking it cannot be wrong. Both surfaces are
covered — the coordinator gateway and the node agent are different apps on
different ports — and the gateway is stood up from `gateway/stubs.py` plus
`fit/stub.py::StubFit`, so enumerating routes needs no real calculator, no
registry and no disk.

**Method and path only, never the full OpenAPI schema.** The fact worth pinning
is which endpoints exist; a full schema would rewrite itself on every unrelated
signature change and train everyone to regenerate without reading the diff.

`_walk` follows `original_router` and its `include_context.prefix` rather than
reading `app.routes` flat. FastAPI stopped copying an included router's routes
onto the app and now holds a wrapper that defers to the original, so a naive
collector sees four routes on a surface that answers a hundred and cheerfully
reports the other ninety-six do not exist — the same false confidence a
hand-written table gives, arrived at more convincingly. A `Mount` is skipped,
because listing it would be listing the filesystem; a `WebSocketRoute` is
emitted with the pseudo-method `WEBSOCKET`; `HEAD` is dropped as implicit.
`_collect` collapses on `(methods, path)` together — two routers may legitimately
answer the same path with different methods, but the same pair twice is a
registration bug.

## `document.py`

`python3 -m control_plane.contracts.document --write`. `render()` calls
`manifest.build()` and `routes.build()` and lays them out as one markdown lookup
surface: enumerations, data shapes, ports, constants, derived facts, both HTTP
surfaces with their route counts, and the environment table. Every line comes
from the code, so it cannot be stale in a way the code is not.

`DOCUMENT_PATH` is `Path(__file__).resolve().parents[2]` — the repo root —
joined to a markdown filename. **Nothing is at that path in this checkout and
git has never tracked it**, so `--write` creates the file rather than updating
it, and `test_the_generated_contract_document_matches_the_manifests`, which
reads the path directly before diffing, fails on the read rather than on a
difference. `telemetry/journal.py:162` and `tests/test_telemetry.py:265` both
still send an operator there to look up `DERATE_TELEMETRY_MAX_BYTES`, and there
is nothing there to read.

## `__init__.py`

41 names in `__all__` and nothing else: eleven module constants (`BYTES_PER_PARAM`
among them), `ENDPOINT_FOR_MODALITY`, eight enums, fourteen dataclasses and the
seven ports, re-exported flat. 51 files say `from control_plane.contracts import`
directly, so this list is the surface and dropping a name from it is not a
refactor.

## `manifest.json` — generated

1,732 lines. 30 types (15 dataclasses, 8 enums, 7 protocols), 14 constants, 8
derived facts and 63 environment variables. Its first key is a `note` saying it
is generated and naming the command that rewrites it. Do not edit it; edit the
thing it reflects and regenerate.

## `routes.json` — generated

535 lines: 77 gateway routes and 11 node-agent routes, each `{methods, path}`,
sorted by path then methods. Same `note`, same rule.

## The seam with everything else

Nothing here imports out of `control_plane/contracts/` except the generators,
which import `envspec`, the gateway and the registry agent to reflect them. Every
other package imports in.

```python
from control_plane.contracts import FitRequest, ModelShape, NodeProfile, Verdict
```

- **`gateway/deps.py`** types its seven `GatewayDeps` port fields by protocol,
  and `strict=True` makes a composition root assert every one was supplied —
  so a stub can never backfill a real dependency silently.
- **`fit/`** consumes `FitRequest`/`FitResult`/`MemoryBreakdown` and the
  `constants.py` numbers; its `stub.py` is what `routes.py` composes.
- **`planner/`** produces `ParallelismPlan` and reads the two 40.0 GB/s
  viability thresholds.
- **`resolver/`** produces `ModelShape` and is the heaviest consumer of
  `quant.py` — `normalize_dtype`, `BYTES_PER_PARAM` and `GGML_TYPES` in
  `resolver/gguf.py` are the same conversation from two sides.
- **`deploy/`** owns `SUPPORTED_RUNTIMES`, which `derived.py` names canonical for
  the runtime names that `Deployment.runtime` carries as a bare string.
- **`ui/src/api/types.ts`** mirrors the enums in TypeScript, and
  `ui/src/api/contracts.check.mjs` (`// requires: python`) shells to
  `python3 -m control_plane.contracts.manifest` and diffs each
  `export type X = 'a' | 'b'` union against the enum's members, in order. A
  union that still lists five device classes typechecks perfectly against a
  Python enum that grew a sixth, and the new value arrives at runtime as
  something no branch handles. The same verifier checks
  `ui/src/tabs/models/rows.ts::TERMINAL` against
  `manifest.derived.deployment_terminal_states`.

## Things that look like details and are not

**A default on a contract field is a compatibility promise, not a convenience.**
`FitResult.budget_basis`, `Deployment.modality`/`extra_args`/`custom_command`,
`NodeState.memory_total`/`sample_ts`/`is_local`/`build` and
`ProviderModel.modality` are all trailing and defaulted for the same reason:
`Deployment` and `NodeState` are persisted, and a record written before the
field existed has to keep decoding. Inserting a required field in the middle of
one of these dataclasses breaks every stored record at once.

**`num_kv_heads` and `num_attention_heads` are both on `ModelShape` and are not
interchangeable.** The comment saying so is in the contract rather than in the
consumer because the mistake is made at the read site, not the write site.

**`_NOT_CONTRACTS` is why the manifest is stable.** A generator's own module
constants are not contracts. The first run without that exclusion committed a
developer's absolute filesystem path, which would then have differed on every
other machine and failed the freshness test for a reason that had nothing to do
with contracts.

**The manifest pins annotations as strings, not as resolved types.** `int` and
`int | None` are different spellings and the diff should say so. Resolving them
would collapse exactly the change worth catching.

**`derived.py` has two lists because they fail differently, and a `restated_at`
entry is an admission.** It says: this fact is inlined here, no test can catch
it, and converting it to an import is the fix. Four of the nine current entries
are in shell and YAML — `install.sh` and `compose.yaml`, each restating both
default ports — where there is no import to convert to at all.

**`api_key_ref` is a name, and `ProviderPort.resolve_key` is request-time only.**
The contract puts the two on opposite sides: the stored shape carries a
reference, and the only method that returns a value is annotated as being for
request time. `ProviderPort.list` is annotated `# keys redacted` on the same
grounds.

## Failure behaviour

- **An unknown dtype on a `ModelShape`.** `bytes_per_param()` raises `KeyError`
  naming this package's `quant.py`. It never defaults, because pricing an
  unrecognised dtype silently at bf16 under-counts a 32-bit model by half.
- **An unknown dtype at a boundary that must produce a number.**
  `normalize_dtype()` returns `None` and `bytes_per_param_or_default()` charges
  `DEFAULT_DTYPE` (bf16). Never smaller. Deciding what an *undeclared*
  quantization costs is the resolver's job, not the table's.
- **A contract changed and the manifests not regenerated.**
  `tests/test_contracts_manifest.py` fails with the exact command in the
  assertion message, and tells you to read the diff before committing — it is
  the list of contracts your change moved.
- **A copy of a derived fact stops matching.** `tests/test_single_source.py`
  fails, naming both sites. Fix by making them agree, and extend coverage by
  adding a row to `derived.py`, never by adding an assertion to the test.
- **A shape moved and nothing under `control_plane/` broke.** The TypeScript
  mirror is the likely casualty: `contracts.check.mjs` is the only thing that
  looks, and under `npm run check` an unmet `requires: python` puts it in the
  skipped column rather than the failed one. `--strict` is the mode that makes
  that a failure.
- **`reflect_contracts()` finding nothing at all.** Both freshness tests would
  pass, comparing empty against empty. `test_the_manifest_reflects_rather_than_restates`
  is the guard: it asserts `NodeProfile`, `ModelShape`, `ParallelismPlan`,
  `DeviceClass` and `Verdict` are present, that `DeviceClass.GB10 == "gb10"`,
  and that the environment table has more than 40 entries.

## Deliberately not built

**A hand-maintained route table.** One existed in prose, introduced with the
claim that the UI codes against exactly it, and it was missing eleven routes
that had shipped — setup, runtime management, node labelling, links reach, model
search. A route that exists and is not in the table is undiscoverable to anyone
taking the document at its word; a route in the table that no longer exists is
worse. `routes.py` replaced it with reflection, and
`test_the_generated_document_is_not_a_second_place_to_look` names three of the
previously-missing paths (`/api/setup`, `/api/models/search`,
`/api/nodes/{node_id}/runtime`) so the replacement cannot regress into the same
shape.

**A full OpenAPI dump.** Considered and rejected in `routes.py`: it would
rewrite itself on every unrelated signature change, and a generated file that
churns trains everyone to regenerate without reading the diff — which is the
failure the generator exists to prevent, reintroduced through the mechanism
meant to prevent it.

**A constant for Unsloth Dynamic quantization.** `UD-` names a per-tensor mix
with no fixed bits per weight, so there is no honest number to put in
`BYTES_PER_PARAM`. The aliases map it onto its base scheme, which identifies the
family and is explicitly not a size; sizing a UD file uses the file's real byte
count.
