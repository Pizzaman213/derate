# planner

Picks tensor, pipeline, expert and data parallel degrees from measured hardware
facts rather than from a user's guess, and hands back a sentence naming the
bandwidth and the concurrency the decision turned on. Nothing else in the
ecosystem chooses: vLLM, SGLang, TensorRT-LLM and Ray take the degrees from the
user and only check legality at startup, and sparkrun maps `--tp N` straight to
N hosts without ever picking N.

**Nothing here hardcodes a bandwidth.** If a driver update turns GPUDirect RDMA
on and the measured all-reduce doubles, the answer flips on its own —
`tests/unit/test_planner.py::test_no_bandwidth_is_hardcoded_in_the_planner` greps
every `.py` in this folder for a literal shaped like one, because a planner that
hardcodes the number it exists to measure has quietly stopped being the product.

The `reason` string is the deliverable. `ui/src/sidebar/PlanSection.tsx` and
`ui/src/inspectors/DeploymentInspector.tsx` render it through `Verbatim` and the
`rejected` list through `VerbatimList`, exactly as received — never truncated,
re-cased or summarised.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `planner.py` | 986 | `Planner.plan()`, the family ranking, and every reason and rejection sentence |
| `legality.py` | 264 | which degrees can load at all, and the refusal wording for the ones that cannot |
| `fit_bridge.py` | 237 | the two capacity numbers the planner needs from the fit gate, plus a conservative fallback |
| `comm.py` | 196 | bytes-on-the-wire arithmetic behind every decision |
| `topology.py` | 174 | grouping nodes into pools it is sane to serve one model across |
| `NOTES.md` | 86 | where the original specification disagreed with itself, and which reading won |
| `__init__.py` | 78 | the export surface other packages import through |
| `constants.py` | 63 | planner-local tunables, each carrying the measurement it came from |
| `stub.py` | 57 | the day-0 fixed PP=2 plan |

## `planner.py`

`Planner.plan(shape, nodes, link, target, concurrency, *, context_length=None,
kv_dtype=DEFAULT_KV_DTYPE, allow_mixed_hardware=False)` is the entry point. The
five positional arguments are the frozen `PlannerPort`; the three keyword-only
extras are beyond it. What it returns is the head of `alternatives()` by
construction, so the recommendation and the ranked override list can never
disagree. `plan_for()` renders an operator's chosen degrees
through the same `_render` path — same reason text, same rejected list — because
a gateway-written `reason` would be rendered verbatim and persisted on the
deployment forever. `explain()` is the multi-line dump; `valid_tp_degrees()` is
re-exposed on the class so `gateway/capacity_api.py` can ask a port rather than
a module.

`_family_rank` is the decision and everything else is arithmetic. Single node
ranks 0 unconditionally: a model that fits on one machine is never split,
because the spare node is worth more as a second replica behind the gateway's
load balancer than as a second rank paying interconnect cost on every token.
Expert ranks 1. Below that the order inverts on `_Facts.tp_preferred` — tensor
first when the measured all-reduce is at or above `TP_VIABLE_THRESHOLD` (40.0
GB/s) or the workload is single-stream and latency-targeted, pipeline first
otherwise. With no measurement at all the answer is no, and pipeline wins:
pipeline degrades gracefully at unknown bandwidth and tensor parallel does not.

### `min_nodes < 1` is "impossible", not "one node"

`_Facts.capacity_impossible` exists because the real fit calculator returns
`-1` for "no node count in my search range holds this", which is a different
fact from "needs more nodes than this cluster has" — that one returns a
positive, merely large, `min_nodes`. Reading `-1` as an ordinary count and
clamping it to a floor produces a floor of 1, which makes a single-node plan
look legal and lets its reason claim the model fits when nothing was found to
fit it anywhere. `capacity_floor` therefore replaces the sentinel with the whole
group, forcing the widest split available, and `_justification` says plainly
that the shape is "offered as the closest available shape, not a working plan,
and the fit check will refuse it".

### The single-node winner is upgraded to intra-node degrees

`_score` re-scores the head of the ranking when it is `SINGLE_NODE` and the
exemplar has more than one GPU. "Tensor parallel inside a node" is the case that
rule was written for and it holds there. The upgrade routes through
`valid_tp_degrees` / `valid_ep_degrees` rather than assuming `gpu_count` divides
the model: a 6-GPU node running a 64-head/8-KV-head dense model cannot do TP=6
and would fail at load, so the largest degree that actually divides is used and
`_justification` names the GPUs left idle and why — "only 4 of 6 GPUs on this
node divide the head counts evenly".

### Three sentences that would otherwise be false

- **A tensor-parallel plan below the threshold does not argue for itself.**
  Reachable only through `plan_for`, when an operator asked for TP at a
  bandwidth the planner would not have picked it at. The usual clause would
  report a measured 10.2 GB/s as "at or above the 40 GB/s threshold", so that
  branch states the measurement against the shape instead and says the shape was
  asked for rather than chosen. The recommendation travels back beside it with
  its own reason intact.
- **When the model fits on one node, no rejection line mentions bandwidth.**
  `_rejection_line` short-circuits: crossing the link is pure cost there, and a
  threshold argument would be nonsense. It counts the exchanges the split would
  add and points at the second replica instead.
- **A tensor candidate that lost while the link was fast enough is not told it
  failed the threshold.** Nor is the winner claimed to move fewer bytes — an
  expert all-to-all usually moves more and wins anyway, because it overlaps with
  compute where a chain of serialised all-reduces cannot.

`_structural_rejections` lists what was ruled out *before* ranking — every
illegal TP degree, the single-node refusal, the expert-parallel gate — because
an option nobody can see was rejected looks like an option nobody considered. A
single-node plan has that line filtered out of its own rejected list; a plan
must not list its own chosen shape as a rejection.

## `legality.py`

Legality is not a matter of taste, so it is decided before preference applies.
`valid_tp_degrees` requires a degree to divide **both** `num_attention_heads`
and `num_kv_heads`; under GQA the KV count is the binding one, and a model with
64 query heads over 8 KV heads caps at TP=8, not TP=64. `valid_ep_degrees`
requires `num_experts % ep == 0`, because an uneven split leaves one rank
holding an extra expert and every all-to-all waits on the slowest rank.
`valid_pp_degrees` has no head constraint and tolerates uneven layer splits — 80
layers over 3 stages is 27/27/26 and runs fine — bounded only by a stage owning
at least one layer.

`enumerate_candidates` walks hybrid TP x PP combinations rather than assuming
symmetry: published sweeps found TP2/PP8 beating TP4/PP4 for one model and
TP4/PP4 beating both alternatives for another, so only the arithmetic can say.
`Candidate`, `check_degrees`, `DegreeRefusal` and `IllegalDegrees` are the
override path. `DegreeRefusal.axis` is the wire field (`tensor_parallel`, …) so
a 400 can point at the exact key the operator sent while `message` still speaks
the planner's own vocabulary.

**`check_degrees` deliberately does not check that the world size fits the
supplied ranks.** `fit.calculator.check` already refuses that with a better
sentence — it knows how many GPUs were offered and says how many more are needed
— and two authorities on one fact is worse than one. Legality here means "this
shape cannot load", never "this shape does not fit". `tp_rejection`,
`pp_rejection`, `ep_rejection` and `dp_rejection` are those sentences, one per
axis, and `check_degrees` runs all four in axis order. They were lifted verbatim
out of `Planner._structural_rejections`, which now calls `tp_rejection` instead
of keeping its own copy, so a degree an operator names by hand is refused in
exactly the words the planner uses when it rules the same degree out on its
own.

## `fit_bridge.py`

The planner needs exactly two numbers from the fit gate:
`min_nodes_required(shape, profile, context, max_seqs, kv_dtype)` and
`kv_bytes_per_token(shape, kv_dtype)`. `default_fit_helpers()` imports
`control_plane.fit` lazily and by name, checks that both attributes exist, and
answers them itself otherwise. Because swapping in the fallback changes the
capacity arithmetic every plan's reason and warnings rest on, it is never
silent: the fallback path logs a warning naming why, rather than burying it
behind a passed exception. `default_fit_helpers()` runs from `Planner.__init__`,
so that warning fires once for each planner constructed, not once at module
import.

The fallback is conservative by construction — it may ask for one node more than
necessary, never one fewer. Instead of searching TP and PP shards separately the
way the real calculator does, every candidate node count `n` is charged as the
busiest pipeline stage: `ceil(num_layers / n) / num_layers` of the shardable
weights and KV, never a flat `1/n`. An even divide is optimistic whenever the
layer count does not divide evenly, and the node holding 27 of 80 layers is the
one that OOMs.

`kv_bytes_per_token` uses `num_kv_heads`, not `num_attention_heads` — 8x apart
on Llama 3.3 70B and the most common over-estimate in this field. MLA caches the
compressed latent *plus* the decoupled RoPE component; dropping that half
under-counts a DeepSeek-family cache by roughly 11 percent, in the OOM
direction. For a sliding-window model it returns the marginal cost of one more
token — the full-attention layers only — while `kv_total_bytes` charges the
windowed layers' fixed cost, because it is the one that knows the context
length. `min_nodes_required` charges `EP_EXTRA_BUFFER_BYTES` on top of
`COMM_BUFFER_BYTES` for any sharded MoE: expert staging is the term other
planners forget, and forgetting it produces a config that passes a fit check and
then OOMs on the first batch.

## `comm.py`

The arithmetic behind the decisions, in its own file so the numbers in the
reason strings are computed rather than asserted and a reviewer can check them
without reading the decision logic. The headline comparison, a dense 70B at
batch 1 across two nodes:

```
tensor parallel     2.6 MB per output token, over 160 cross-node exchanges
pipeline parallel  16 KB  per output token, over 1 cross-node exchange
```

A factor of roughly 160 in both volume and exchange count, and the reason the
"tensor parallel across a slow link" default is wrong here.
`test_planner.py` asserts both figures against this module.

`tensor_bytes_per_step` uses the ring all-reduce figure `2 * S * (n-1) / n` per
rank, which is what NCCL implements. `tensor_exchanges_per_step` is the
latency-bound half: at 40 microseconds a hop, an 80-layer model pays 160 round
trips per token however small the payload. `pipeline_bubble_fraction` is the
GPipe `(p-1)/(m+p-1)`, with in-flight requests standing in for microbatches.
`estimated_step_seconds` orders candidates and does nothing else. Its result
reaches `_Scored.sort_key` and stops there -- no rejection line ever prints it,
whatever the function's own docstring still says; the concrete numbers in those
sentences come from `human_bytes`, `tensor_exchanges_per_step` and
`pipeline_bubble_fraction`. It reproduces the *ordering* of the GPT-OSS-120B
measurements, not their magnitudes, and `control_plane/fit` owns
`predict_decode_tps` and the efficiency factor behind any figure shown to a
user.

**A node whose memory bandwidth probed as 0 GB/s ranks last, it does not
raise.** `compute_seconds_per_step` returns infinity — the honest ranking answer
for a machine that cannot be shown to decode any faster than never — instead of
a `ZeroDivisionError` inside the scorer. Whether such a machine may be placed on
at all is a placement question, refused earlier and with a better sentence.

**`human_bytes` is decimal and stays here.** `contracts/derived.py` records the
split: `control_plane.humanize:binary_bytes` is canonical for GiB and this one
is kept out of it on purpose, because this module formats transfer volumes
beside decimal GB/s bandwidths and is right to stay decimal.

## `topology.py`

A 3090 desktop and a Spark have different memory, different bandwidth and a
slower link between them. Pooled, the slow node becomes the tail latency for
every request that touches it and its pipeline stage is the one everything waits
for — so `homogeneous_groups` partitions on `_shape_key` (device class, GPU
name, GPU count, addressable memory, bandwidth rounded to one decimal because
probes wobble) and the odd machines are offered as a separate deployment target.
Ordering is by aggregate usable memory first: the fastest group is useless if
the weights do not fit in it.

`pooled_group` is the explicit override, used whenever the caller named the
nodes. It preserves the order given, because the first node is the pipeline head
sparkrun SSHes to first and sorting would silently move it. `NodeGroup.exemplar`
is the **weakest** member, not the first — for a homogeneous group every member
ties and it is `nodes[0]` as it always was, but a pooled group has to charge its
capacity floor, its step-time estimate and its prose against the node that will
actually bind.

`exclusion_note` and `pooling_note` are the two voices of one fact and both
render `POOLING_HAZARD`, differing only in `verb`: "would make" for a pool being
refused, "makes" for one that now exists. A single template is what stops the
refusal and the warning from drifting into two descriptions of the same hazard.
`pooling_note` returns empty for a homogeneous selection — warning about pooling
interchangeable machines teaches people to ignore the warning.

## `NOTES.md`

The record of where the original planner specification contradicted itself and
which reading won. Each of the three conflicts is resolved toward keeping the
acceptance criteria testable, and each resolution is pinned by a named test:

1. **The MoE branch versus "flips to tensor parallel".** The decision function
   sent any MoE model to DP attention plus EP whenever the link cleared
   `EP_VIABLE_THRESHOLD` with GDR, while an acceptance case required
   GPT-OSS-120B to flip to *tensor* parallel at 50 GB/s on two nodes. Resolved
   by `MIN_NODES_FOR_CROSS_NODE_EP`, so the MoE branch falls through on a narrow
   cluster and fires as written on a wide one.
2. **The latency override is not restricted to dense models.** Its evidence — 40
   tok/s under TP against 29 under PP at single stream — was measured on
   GPT-OSS-120B, which is MoE, so restricting the rule to dense models would
   contradict its own source. It is evaluated before the family split.
3. **One acceptance case is tested two ways.** GPT-OSS-120B at concurrency 1
   fits on a single Spark at every context it supports (57.8 GiB of MXFP4
   weights and under 5 GiB of KV at the full 131072 tokens, against 107.7 GiB
   usable), and the "never split a model that fits on one node" rule is
   absolute. So the single-stream flip is pinned with a stubbed `min_nodes` of
   2, and again end to end on Llama 3.3 70B, whose 131 GiB of bf16 weights need
   both nodes at any concurrency.

A fourth section is not a fourth conflict. It records that criterion 1 -- two
Sparks at 10.2 GB/s and concurrency 16 choosing pipeline -- only holds at a
context where the model genuinely needs two nodes, so its test uses
GPT-OSS-120B's native 131072 rather than the 32768 default, at which the model
fits on one Spark and the planner correctly says so. It closes on
`PlannerPort.plan()` being frozen without a context parameter, which is why
`context_length` arrives as a keyword-only extra defaulting to
`DEFAULT_PLAN_CONTEXT`.

## `__init__.py`

The import path, twenty-eight names wide. `Planner` and `StubPlanner`; seven of
`comm`'s eleven functions -- the three byte counters, the pipeline and tensor
exchange counters, `pipeline_bubble_fraction` and `estimated_step_seconds`,
while `human_bytes`, `link_seconds`, `compute_seconds_per_step` and
`expert_exchanges_per_step` stay internal; `Candidate`, `enumerate_candidates`,
`check_degrees`, `DegreeRefusal`, `IllegalDegrees` and the three degree
validators from `legality`; `NodeGroup`, `POOLING_HAZARD` and the four topology
helpers; and five of `constants.py`'s eight values, which is why
`tests/unit/test_planner.py` imports `MIN_NODES_FOR_CROSS_NODE_EP` from the package
rather than from the module. All of it is in `__all__`, and
`gateway/internal_api.py` imports seven of those names at module scope, so
removing one is not a refactor.

## `constants.py`

Anything the whole system shares lives in `contracts/constants.py` and is
imported, never re-declared — `tests/unit/test_planner.py::test_thresholds_come_from_contracts`
asserts that neither `TP_VIABLE_THRESHOLD` nor `EP_VIABLE_THRESHOLD` appears in
this file's source. What is here is how the planner weighs one strategy against
another, and every value carries the measurement it came from:

- `ACTIVATION_DTYPE_BYTES` (2) — MXFP4 is a storage format for weights; the
  hidden states TP all-reduces and PP hands off are 2 bytes per element whatever
  the weights are.
- `ALLREDUCES_PER_LAYER` (2) — after the attention output projection and after
  the MLP down projection. This is the number that makes TP expensive across a
  slow link: 160 cross-node exchanges per output token on an 80-layer model.
- `LATENCY_CONCURRENCY_CEILING` (2) — measured on GPT-OSS-120B across two
  Sparks, roughly 40 tok/s under TP against pipeline's 29 at single stream.
- `PIPELINE_INFLIGHT_PER_STAGE` (4), `MIN_NODES_FOR_CROSS_NODE_EP` (4),
  `DEFAULT_PLAN_CONTEXT` (32768), `MAX_NODES_CONSIDERED` (64).

`DEFAULT_KV_DTYPE` is `"auto"`, which defers to the fit calculator's own
per-model resolution rather than the planner pinning a byte width. Pinning a
literal here used to be cosmetic: `kv_dtype` was accepted and stored but never
forwarded to the capacity question, so the default — and anything a caller
passed — had no effect on `min_nodes_required` at all. `_facts` now forwards it,
and `"auto"` preserves the historical no-op for callers that do not care.

## `stub.py`

A fixed PP=2 plan with a fixed reason that reads nothing, for use before the
real planner is wired. It returns a real `ParallelismPlan` with a non-empty
reason and a non-empty `rejected` list, because a stub that returns invalid
contract types is worse than no stub, and it accepts `context_length` and
`kv_dtype` unused so that wiring it would not 502 `/api/plan` on the first call.
`STUB_REASON` says out loud that the decision "was not derived from any
measurement and must not be shown to a user as if it were".

Its docstring says it is deleted at integration and that any import of it
afterwards is the bug. Today its only importers are this package's `__init__.py`
and `tests/unit/test_planner.py`: the gateway, `contracts/routes.py` and the load
harness all compose `gateway/stubs.py::StubPlanner`, which is a different class.

## The seam with the gateway and the node

`node.py::build_gateway_deps` builds the one real `Planner()` and hands it to
`GatewayDeps.planner`. It deliberately calls `Planner()` with no argument: the
`fit` parameter is this package's own `FitHelpers` protocol
(`min_nodes_required` / `kv_bytes_per_token`), and `FitCalculator` implements
neither -- both are module-level functions in `control_plane/fit/calculator.py`,
not methods on the class. Passing one is accepted silently, skips
`default_fit_helpers()` and its `hasattr` gate altogether, and raises
`AttributeError` at the first capacity question rather than falling back. The
no-arg default resolves through `fit_bridge` to `control_plane.fit`'s
module-level helpers.

`gateway/internal_api.py` is the only module that asks for a plan, and it calls
through `asyncio.to_thread`:

```python
ranked = _rank_plans(deps.planner, shape, nodes, link, target, concurrency,
                     context_length=..., kv_dtype=..., allow_mixed_hardware=pool)
recommended = ranked[0]
plan = recommended if requested_degrees is None else plan_for(..., **requested_degrees)
```

- **`_rank_plans` probes for `alternatives` and degrades to `plan`.** A planner
  exposing only the frozen port still works; it just cannot say what it ranked
  second.
- **`plan_for` is probed too, and its absence is a 501, not a fabrication.** A
  gateway-written reason would be rendered verbatim and persisted on the
  deployment forever, so a planner that cannot author one for the operator's
  shape says so.
- **`IllegalDegrees` becomes a 400** carrying `param` built from
  `DegreeRefusal.axis`, one `rejected` line per refusal in the planner's own
  words, and `_legal_degrees` — `valid_tp_degrees` / `valid_pp_degrees` /
  `valid_ep_degrees` — so the refusal is actionable.
- **`allow_mixed_hardware` is set from whether the operator named the nodes.**
  Pooling unlike hardware is a permission, not a fit failure, so it rides as a
  serve gate whose `reason` is `pooling_note(homogeneous_groups(plan_nodes))`
  and the plan is computed across the set as given — a dry run starts nothing,
  so it owes an honest answer about the set it was handed.

`gateway/capacity_api.py::_plan_for` takes `valid_tp_degrees` off the port
rather than using `len(profiles)`, because three machines take a model at TP=1
however many boxes somebody ticked, and reporting a fit at a degree the runtime
would refuse to start at is exactly the class of answer this project does not
give.

## Things that look like details and are not

**`measured_link_gbps=0.0` is a sentinel, not a speed.** `_Facts.link_gbps`
returns it when nothing has been measured, and `_link_label` renders it as "not
measured". A UI that prints it as a bandwidth is reporting a link nobody probed.

**A single-node plan names one host even when its world size exceeds one.**
`_nodes_for` truncates to `node_ids[:1]` for `SINGLE_NODE`, because those extra
ranks are GPUs in the same box. This is why `plan_for` matches on the kind
alongside the degree tuple: on a 4-node cluster of 4-GPU boxes, a single-node
TP=4 and a cross-node TP=4 share a tuple and are not the same plan.

**"Use the fewest nodes" is conditional on a whole second replica fitting.**
`_Scored.sort_key` flips the world-size direction on
`_Facts.second_replica_possible` (`min_nodes * 2 <= group.size`). Spare capacity
is worth more as an independent replica than as extra ranks paying interconnect
cost per token — but only when there is enough of it for a whole second copy.
When there is not, the spare nodes are merely idle and spreading wider is the
better answer.

**Expert parallel is gated three ways and all three are hard.** Bandwidth at or
above `EP_VIABLE_THRESHOLD`, GPUDirect RDMA enabled, and at least
`MIN_NODES_FOR_CROSS_NODE_EP` nodes. DeepEP assumes InfiniBand or RoCE with
GPUDirect; measured internode dispatch on a properly equipped H800 cluster is
roughly 43 GB/s and the Spark link is four times slower than that
already-degraded case. `_ep_rejection` names which gate failed, and an
unmeasured link gets its own fourth sentence rather than being reported as a
bandwidth of zero.

**Every rejection line is a sentence, and `_plural` exists so they read like
one.** "1 exchange", "160 exchanges". `_node_list` caps at three names and then
says "and N more" rather than printing a wall of node ids.

## Failure behaviour

- **No nodes.** `plan` and `plan_for` raise
  `ValueError("cannot plan a deployment with no nodes")`. There is nothing to
  rank.
- **No link measurement.** `tp_preferred` is false, pipeline wins, and the
  reason says so and asks for the link to be measured. Never a fabricated
  bandwidth.
- **`control_plane.fit` missing or incomplete.** `default_fit_helpers()` falls
  back to `fit_bridge`'s own arithmetic and logs a warning naming why, once for
  each `Planner()` constructed.
- **Capacity impossible at any node count.** The widest legal split is offered,
  the reason and a `Warning:` both say no plan on this cluster will pass the fit
  check, and the remedy named is context, concurrency or quantization.
- **Capacity needs more nodes than the group has.** The best available shape is
  returned with a `Warning:` naming the shortfall; at or above
  `MAX_NODES_CONSIDERED` the phrasing becomes "more than 64" rather than a
  precise-looking number the search never reached.
- **Concurrency too low for the pipeline depth.** `_bubble_warning` fires below
  `PIPELINE_INFLIGHT_PER_STAGE * pp`, prints the bubble percentage, and says
  tensor parallel may serve better.
- **A node with unprobeable memory bandwidth.** Ranked last via an infinite step
  time, never a `ZeroDivisionError`.
- **Illegal degrees from an operator.** `IllegalDegrees` is raised, not
  returned: a shape that fails head divisibility does not load, so there is
  nothing for the fit gate to have an opinion about.
- **No legal candidate at all.** `_score` falls back to a single
  `Candidate(1,1,1,1)` so the caller gets a plan with an honest reason rather
  than an empty list.

## Deliberately not built

**A throughput prediction.** `estimated_step_seconds` orders candidates and
nothing else — its own docstring says nothing here should be shown to a user as
a tokens-per-second figure. `control_plane/fit::predict_decode_tps` owns that
number and its efficiency factor.

**A second opinion on whether the model fits.** `check_degrees` skips the
world-size-versus-ranks check and `plan_for` does not raise for a shape that
merely will not fit. Both are the fit gate's sentence to say, and saying them
here would put two authorities on one fact.

**A lock.** `alternatives` returns every legal plan ranked, each carrying its
own reason and its own rejected list, and `plan_for` will plan any legal shape
an operator names. The planner is a recommendation with its reasoning attached;
the reasoning is what makes disagreeing with it possible on the evidence.
