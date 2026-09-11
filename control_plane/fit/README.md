# fit

The blocking pre-launch out-of-memory gate. Every other tool in this space
estimates and then lets you launch anyway; this one refuses, and when it
refuses it names the term that blew the budget and the specific change that
would work. A reason string that says only "does not fit" is a bug in this
package, not a terse answer.

The whole gate is one inequality, evaluated per rank:

```
weights + kv_cache + activations + comm_buffers + replicated
    + framework_overhead  <=  usable_memory
```

`capacity.py` inverts it. Instead of "does this model fit", it answers "given
this hardware as it is right now, which models run, at what quantization, and
which is the largest of them".

## Layout

| File | Lines | What it owns |
|---|---|---|
| `calculator.py` | 1062 | `FitCalculator.check()`, the six memory terms, and every refusal string |
| `capacity.py` | 505 | the inverse question: what runs here, at which rung of the quantization ladder, at what context |
| `kv.py` | 137 | KV cache sizing — `num_kv_heads`, sliding windows, MLA, uneven pipeline stages |
| `stub.py` | 106 | the day-0 `FitPort` stub, still wired into the routes manifest |
| `catalog.py` | 70 | the four curated shapes, moved server-side out of the browser |
| `constants.py` | 67 | tuning constants; `contracts/constants.py` wins where it defines one |
| `__init__.py` | 52 | the package's export surface, and the only import path other packages use |

## `calculator.py`

`FitCalculator.check(req, nodes, *, allocatable=None)` is the gate. It returns
a `FitResult` carrying a `Verdict`, the six-term `MemoryBreakdown`, headroom, a
`limiting_term`, a predicted decode rate, and the reason string. Callers branch
on `FitResult.ok`, never on `verdict is FITS`: `FITS_DEGRADED` means the model
loads and decodes below `DEGRADED_TPS_THRESHOLD` (10.0 tok/s), which is
sometimes exactly what somebody wants.

Three refusals exist and they are ordered. A plan wanting more ranks than the
supplied GPUs is refused first. Then negative headroom, diagnosed by
`_diagnose` into `weights`, `kv_cache` or `combined`. Then a context past the
model's own `native_window` — not a memory question at all, and the reason says
so: vLLM's config validation refuses to start regardless of how much GPU is
free, and a launch here died on `max_model_len (780800) is greater than ...
max_position_embeddings (40960.0)` thirty minutes into a readiness wait,
because nothing upstream of the runtime knew to say no first. That check runs
*after* headroom, because a model whose weights alone do not fit is refused for
that reason first — a smaller context would not fix it. `FITS_DEGRADED` follows,
then `FITS`; neither is a refusal, and `limiting_term` is `"bandwidth"` and
`"none"` on them.

The exported helpers are used outside the class: `memory_breakdown`,
`weight_bytes_per_rank`, `replicated_bytes_per_rank`, `activation_bytes`,
`comm_buffer_bytes`, `min_nodes_required` and `predict_decode_tps`.

**Every number in a reason string has been verified, never extrapolated.**
`max_context_that_fits` comes from `_largest_context`, a binary search on a
`CONTEXT_ROUNDING` grain that re-checks its own answer before returning it.
"Reduce concurrency to N sequences" comes from `_largest_max_seqs`, another
search. "Requantize to q4_k_m" comes from `_suggest_quant`, which has actually
priced that scheme against the remaining budget. "Spread it over 5 nodes" comes
from `min_nodes_required`, which searched every legal shard at every node count
up to `MAX_SEARCH_NODES`.

**`weight_bytes` beats the dtype formula whenever the resolver measured one.**
`weight_bytes_per_rank` takes `FitRequest.weight_bytes`, the resolver's
safetensors/GGUF on-disk total, in preference to `total_params *
bytes_per_param()`. Formulas miss padding, tied embeddings and packing
overhead; on GPT-OSS-120B that gap is close to 3 GiB, which is the difference
between fitting and not. `None` or non-positive falls back to the formula
unchanged, and the vision share is always priced from `vision_params *
bytes_per_param()` and subtracted out, floored at zero.

### The refusal strings are the product

`_weights_reason`, `_kv_reason` and `_combined_reason` are the last 150 lines of
the file and the reason it is worth its length. Three details in them are
load-bearing:

- **`_gib_vs` widens precision until two side-by-side figures differ.** "15.6
  GiB per rank against 15.6 GiB left" is a true sentence that appears to say a
  thing does not fit inside itself. It tries one decimal, then two, then gives
  up and prints both plainly — at which point they really are equal for every
  purpose a reader has, and the exact overage clause carries the difference.
- **`_nodes_phrase` never renders `-1` as a node count.** `min_nodes_required`
  returns `-1` to mean "a different machine or a smaller quantization, not more
  Sparks". Rendering that as "more than 64 nodes" named the single remedy the
  search had just ruled out. A machine with `addressable_memory <= 0` gets its
  own branch — "a machine with GPU memory; no number of these holds it" —
  keyed on the *addressable ceiling*, never on the live budget, because a GB10
  whose pool is momentarily full is a machine that plainly does have GPU
  memory.
- **`_reported_context` is the `None` sentinel.** `_largest_context` returns 0
  for "not even one rounding step of cache fits", which is not the same claim as
  "zero tokens of context fits". 0 must never cross the port boundary, on any
  refusal, whichever term the diagnosis names — a `combined` refusal where
  nothing fits is exactly as unhelpful a "try 0 tokens" as a `weights` one.

### Live memory is an optional kwarg that degrades

`_budget` decides the budget and its basis. Without `allocatable` it is the
static ceiling: `addressable_memory * guardrail`, what the hardware could ever
spend with nothing else running. With it, the budget is what the node can hand
out at this moment, and `FitResult.budget_basis` reads `"live"`.

A node absent from the mapping falls back to its own static ceiling and says so
in `warnings` — an unpolled node must not read as having no capacity, or a cold
coordinator refuses every launch. If the *binding* node is one that could not be
read, the basis stays `"static"` however many others were read. The binding node
is the argmin over whichever budget is in force, not over the static ceiling,
because the smallest ceiling and the smallest live figure need not be the same
machine.

`_budget_word` follows the basis into the prose: under a live budget the word
is "allocatable right now", not "usable", because it is not what the hardware
could spend — it is what was left at the moment we looked.

### Speculative decoding is charged into the existing terms

`FitRequest.speculative` is trailing and defaulted, and `None` — the ordinary
case — changes nothing at all. Set, `memory_breakdown` adds the draft's weights
to `weights` and the cache for its drafted positions to `kv_cache`, rather than
inventing terms of their own. That is the point: every refusal string,
`_largest_context`, `max_context_that_fits` and the headroom arithmetic then
work unchanged, instead of each having to learn about a second kind of memory.

The draft head is divided by tensor parallel and **not** multiplied by
`stage_fraction`. A runtime places it on one pipeline stage rather than
spreading it across them, so charging it a stage's share would under-budget
precisely the rank that holds it — and a warning says so, beside the
uneven-stages one that exists for the same reason.

`speculative_decode_tps_range` returns a floor and a ceiling and never a single
number:

    ceiling = base * (k + 1) / (1 + k * draft_ratio)   every draft accepted
    floor   = base         / (1 + k * draft_ratio)     none accepted

where `draft_ratio` is the draft's parameters over the model's active ones. The
draft's own bandwidth cost is subtracted from both ends, which makes the ceiling
tighter than a bare `(k+1)x` — and makes the **floor lower than the ordinary
rate**, which is the half of the trade a recommendation is tempted to leave out:
a method that drafts with real weights and gets nothing accepted is slower than
not speculating at all. ngram's `draft_ratio` is zero, so its floor is the
ordinary rate exactly.

`speculative_best_k` is the other half, and the one that narrows the range.
Given `accept_cumulative[i]` -- the fraction of draft rounds in which position
`i` was accepted, read off the engine by `control_plane/metrics_scrape.py` --

    E[accepted](k) = sum(accept_cumulative[:k])
    TPS(k)         = base * (1 + E[accepted](k)) / speculative_overhead(k, ratio)

The series is CUMULATIVE, not conditional: a draft is only checked at position 2
if position 1 was accepted first, which is what makes that a plain sum rather
than a chain of products, and means nothing assumes the positions are
independent. They are not.

`speculative_overhead` is shared with the range above rather than written twice,
and that is load-bearing: all-ones acceptance reproduces the ceiling *exactly*
and all-zeros the floor, so a measured point always lands inside the range the
screen already showed. `tests/unit/test_spec_measure.py` pins both ends,
because that invariant is the only thing tying the measured number to the
claim it narrows.

There is deliberately no assumed acceptance rate anywhere in that function.
Where a real workload lands between the two ends is a property of the workload,
derate does not measure it, and `speculative_reason` says so in the sentence it
hands the screen. An estimate there would be a throughput figure this project
cannot back, which is the one thing every number on these screens is supposed
not to be. `gateway/strength.py` keeps routing on `predicted_decode_tps`, the
base figure — routing on a ceiling would prefer a deployment for a speedup
nobody measured.

## `capacity.py`

`largest_runnable(shapes, nodes, ...)` returns one `CapacityRow` per shape and
the largest that fits — `max` over `total_params`, the honest reading of
"largest model", since a row carries no active-parameter figure to break a tie
on. Each row records the context and `max_seqs` it was actually judged at, so a
derived-per-model context does not leave one number at the top of a report
describing rows it does not describe.

`_walk` climbs down the ladder: the native dtype first, then every scheme below
it in `QUANT_SUGGESTION_ORDER`, stopping at the first that is not `WONT_FIT`. It
runs a real `check()` at each rung rather than reusing `_suggest_quant`'s
weights-only estimate — that estimate holds every other term fixed, which is
right for a suggestion attached to a refusal and wrong for a verdict, because
KV, activations and comm buffers all move with the plan.

**A measured `weight_bytes` is valid only for the dtype it was measured at.**
`_rungs` attaches it to the native rung and `None` to every rung below, and the
row carries a warning saying the weights were priced from the formula. Pricing
q4_k_m weights from measured bf16 bytes would fabricate exactly the sort of
number `weight_bytes` was introduced to remove.

`probe_plan(node_ids, tensor_parallel)` and `_single_node_plan(node_ids)` are
placements *stated*, not searched for, with `measured_link_gbps` pinned at 0.0
so a capacity answer can never be mistaken for a planner result. Capacity means "what runs on these
machines as they stand"; "what could run if we replanned the cluster around it"
needs a target and a link measurement and belongs to `POST /api/plan`.

### One context per ladder, not one per rung

`ladder_context` picks the best-quality scheme that still leaves a workable
context and returns `(context, rung_dtype)` for the *whole* ladder. Per-rung
derivation looks like the obvious implementation and destroys the ladder two
ways, both observed:

- Walking down a quantization ladder, `max_context` returns the largest context
  that fits, so every rung fits by construction — bf16 at 512 tokens "fits" —
  the native dtype always wins, and a ladder that always recommends the native
  dtype has stopped answering the question it exists for.
- Judging a list of published variants, every row gets its own context and the
  column stops being comparable: "fits at 31744" beside "fits at 32256" is two
  answers to two questions printed as one table, and the headroom figures beside
  them cannot be read against each other at all.

Two clamps pull in opposite directions and both are load-bearing.
`MIN_USEFUL_CONTEXT` (4096) is the floor a rung must clear to be chosen;
without it the native dtype wins at 512 tokens. The model's own window is the
ceiling; without it a small model on a big machine is reported at a context its
runtime would refuse to start with, and `MAX_CONTEXT_SEARCH`'s 2,097,152 tokens
is not a window anybody asked for.

A rung that holds *nothing* is skipped on the raw figure, before either clamp.
`_clamp_context` maps 0 to the model's own window — correct on the single-model
path, where the gate must be left to refuse in its own words, and wrong here,
where the loop exists precisely to fall further. Without the skip a 48 GiB bf16
rung refused for its weights alone handed a 262,144-token context to a whole
ladder of 13 GiB variants, which were then refused, one and all, on KV cache.

### `context_for` probes the signature it is calling

`context_for(fit, shape, plan, nodes, ...)` is *the* rule for "what context did
we choose": `min(the model's own window, the largest context that fits)`, on the
`CONTEXT_ROUNDING` grain, never above what the gate verified. The planning path
and the capacity probe both call it rather than each keeping a copy, because two
implementations of that question are two answers to it on the same screen.

`fit` is a `FitPort`, not necessarily a `FitCalculator`, and `weight_bytes` and
`allocatable` are additive deviations from the frozen `max_context` signature in
`contracts/ports.py` — which declares five positional parameters and neither
keyword. So the arguments are passed by `inspect.signature` probe, not by
`try/except TypeError`, which would also swallow a genuine TypeError raised
inside the port. A port with no `max_context` at all returns
`_clamp_context(0, native_window)` — the model's own window where the resolver
found one, `FALLBACK_CONTEXT` (8192) where it did not — rather than taking the
request down; `FALLBACK_CONTEXT` is the bare fallback only on the other path,
where the port raised and the exception was logged. The launch path refuses
separately and loudly when there is no fit gate, and that refusal is not this
function's to pre-empt.

## `kv.py`

The one calculation everything else here leans on, and the one the field most
often gets wrong. Three rules, in order of how much damage getting them wrong
does:

1. **`num_kv_heads`, never `num_attention_heads`.** Llama 3.3 70B has 64 query
   heads over 8 KV heads; the mistake over-charges by exactly 8x.
2. **A sliding-window layer caches the window, not the context.**
   GPT-OSS-120B windows half its layers at 128 tokens. `cached_layer_tokens`
   sums per layer: full layers cache the context, windowed layers cache
   `min(window, context)`.
3. **MLA caches one compressed latent per layer per token**, not per-head K and
   V, so DeepSeek-V3 is an order of magnitude cheaper than its head count
   suggests. `per_layer_per_token_bytes` charges the latent *plus* the decoupled
   RoPE component: dropping that half under-counts DeepSeek-family caches by
   about 11 percent, in the OOM direction.

`kv_divisor` is where TP and PP sharding is kept honest rather than optimistic.
TP cannot shard past `num_kv_heads` — a runtime asked for TP=8 on a model with 4
KV heads replicates the heads instead of splitting them — and MLA has nothing to
split by head at all, so every TP rank holds the whole cache. `stage_fraction`
charges the *busiest* pipeline stage: 80 layers over 3 stages is 27/27/26, and
the node that OOMs is the one holding 27.

`kv_bytes_per_token` is the full-attention *rate* and `kv_cache_bytes` the real
total: multiplying the rate by the context is right for a dense model and
over-charges a windowed one by multiples, which is why every term in
`calculator.py` goes through the total.

`kv_elem_bytes` reads `"auto"` as the model's own dtype only when that dtype is
something a cache can be stored in; a model whose weights are mxfp4 still caches
in bf16 or fp8. An unrecognised dtype falls back to `KV_FALLBACK_DTYPE` (bf16),
over-estimating on purpose — the alternative is a gate that lets an OOM through.

## `stub.py`

`StubFit` is the day-0 `FitPort`: FITS below `STUB_LIMIT` (100 GiB), WONT_FIT
above, with arithmetic simple enough to be obviously wrong so nobody ships it by
accident. It is still reachable — `contracts/routes.py` composes it to enumerate
every route both apps answer without standing up a real calculator, and
`node.py`'s composition root sets `strict=True` on `GatewayDeps` precisely so a
stub can never backfill a real deployment silently.

It returns the same contract types the real calculator does, including two
branches it did not have to implement: it honours `allocatable` and reports
`budget_basis`, and it reports `max_context_that_fits=None` on a refusal rather
than 0. A stub that skips a branch leaves every consumer tested against it with
zero coverage of that branch.

## `catalog.py`

`CURATED_MODELS` is four frozen `CuratedModel` shapes — gpt-oss-120b,
Qwen3-30B-A3B, Llama-3.3-70B-Instruct, DeepSeek-V3 — each with a label, a detail
string and default context and concurrency, plus `catalog_payload()` for the
wire. It is not a registry of what is installed: it is a shortlist of shapes
worth asking about, and every one is resolved for real before any verdict is
taken.

It lived in `ui/src/api/catalog.ts`, which was fine while the picker was its
only consumer. The capacity answer needs the same list, and that answer has to
come from the fit gate rather than the browser, so the list moved here and the
UI fetches it from `GET /api/catalog`, which is `catalog_payload()` and nothing
else. `gateway/capacity_api.py` and `inventory/api.py` both import it.

## `constants.py`

Nine names, four of them read through `getattr(_k, NAME, default)` against
`contracts/constants.py`, which defines none of the four today — so all four
run on the local default: `ACTIVATION_CHUNK_TOKENS` (2048),
`DECODE_EFFICIENCY` (0.55), `DECODE_EFFICIENCY_BEST` (0.75) and
`CONTEXT_ROUNDING` (512, because page sizes are powers of two and a suggestion
of 18944 is easier to act on than 18991). Contracts win the moment they define
one, without an edit here.

**The two efficiencies are the two ends of one range, and neither is an
average.** Seven checkpoints measured on a GB10 through `tests/decode_sweep.py`
imply efficiencies from 0.560 (LFM2.5-350M) to 0.738 (Qwen3-1.7B) — a 28%
spread, so no single constant describes them and the best one available is
still 16% wrong for somebody. 0.55 is at or below all of them, so the figure
the degraded threshold judges and the router seeds from never over-promises;
0.75 is the highest rounded up, so the empty-cache end is a real upper bound
rather than a guess that the hardware sometimes beats. At 0.70 it would not be:
Qwen3-1.7B decodes above it. No MoE is in that corpus — nothing large enough
fit on the box while it was taken.
`MAX_CONTEXT_SEARCH` (2^21), `MAX_SEARCH_NODES` (64), `KV_ELEM_BYTES`,
`KV_FALLBACK_DTYPE` and `QUANT_SUGGESTION_ORDER` are local outright.

`KV_ELEM_BYTES` is deliberately not `BYTES_PER_PARAM`: sub-byte weight
quantization schemes do not apply to the cache, and runtimes cache in fp8 at the
smallest. `QUANT_SUGGESTION_ORDER` is the twelve schemes this package is willing
to suggest, best quality first, and it is also the default ladder
`largest_runnable` walks.

## `__init__.py`

The import path. `FitCalculator` and `StubFit`, the seven calculator helpers,
and the seven `kv` functions, all re-exported and all in `__all__`. The planner's
`default_fit_helpers()` checks `hasattr(_fit, "min_nodes_required")` and
`hasattr(_fit, "kv_bytes_per_token")` on this module by name, so removing either
from `__all__` is not a refactor — it silently swaps the planner onto its own
fallback arithmetic.

## The seam with the gateway and the planner

`gateway/internal_api.py` is the only module in `control_plane/` that imports
this package at module scope, and it takes `context_for` alone. Every other
consumer imports lazily inside the function that needs it, so a worker process
never pulls the package in.

- **`node.py`** builds the one real `FitCalculator()` in `build_gateway_deps`
  and hands it to `GatewayDeps.fit`. It deliberately does *not* pass it to the
  `Planner`: the planner's `fit` argument is its own `FitHelpers` protocol
  (`min_nodes_required` / `kv_bytes_per_token`), which `FitCalculator` does not
  implement, and `Planner()` with no argument resolves to this package's
  module-level helpers instead.
- **`gateway/livefit.py`** is the live-memory seam. It calls the gate twice —
  once on the static ceiling, once on `allocatable` — and puts both verdicts on
  the wire so the backend, not the UI, decides which governs. It probes
  `check` for the keyword first, and a port that predates it degrades to the
  static verdict with an `unavailable_reason` rather than a fabricated number.
- **`gateway/capacity_api.py`** is the biggest consumer: `largest_runnable`,
  `ladder_context`, `probe_plan`, `_single_node_plan`, `CURATED_MODELS` and
  `QUANT_SUGGESTION_ORDER`.
- **`planner/fit_bridge.py`** imports `control_plane.fit` by name, checks for
  the two helpers, and falls back to its own conservative arithmetic if either
  is missing — logging a warning at import time when it does, because the
  fallback changes the capacity numbers a plan's reason rests on and must never
  be silent.
- **`inventory/api.py`** takes `CURATED_MODELS` only.
- **`contracts/routes.py`** takes `StubFit` to enumerate routes.

```python
from control_plane.fit import FitCalculator

result = FitCalculator().check(req, nodes, allocatable=budgets)
if not result.ok:                 # .ok is FITS and FITS_DEGRADED; this is WONT_FIT
    refuse(result.reason)         # rendered verbatim, never summarised
```

`contracts/derived.py` records one crossing in the other direction:
`control_plane.fit.calculator:_gib` is a declared copy of
`control_plane.humanize:binary_bytes`, and `tests/unit/test_single_source.py` fails
if the two stop matching. The formatter left this package because the gateway's
download strings need the same ladder; it stayed out of `planner/comm.py` on
purpose, because that one formats transfer volumes beside decimal GB/s
bandwidths and is right to stay decimal.

## Things that look like details and are not

**`gpu_memory_utilization` is not set here, but it is derived from what this
package returns.** `deploy/utilization.py` takes `needed_bytes` — in its own
words, "the fit gate's own per-node total" — and turns it into the share of the
device the runtime is told to claim. A wrong term here does not merely
mis-report; it sizes the launch.

**`_candidate_shards` searches pipeline degrees as well as tensor degrees.**
Tensor parallel cannot shard the cache past `num_kv_heads`, so on a model with 8
KV heads a pure TP split stops helping at TP=8 no matter how many nodes are
added; pipeline parallel keeps shedding cache by layer past that point.
Searching only TP reports "impossible" for configurations a pipeline split holds
comfortably. The same function refuses any TP degree that does not divide both
head counts, mirroring `planner/legality.py::valid_tp_degrees`, because such a
degree fails at model load and is therefore never a legal shard here either.

**`predict_decode_tps` reads *active* parameters, not total.** GPT-OSS-120B
moves 2.7 GB per token off 5.1B active params; a dense 70B moves 140 GB. That is
an order of magnitude of decode speed on identical hardware and the most useful
thing this package can say before somebody waits five minutes for a load. It is
also what makes `FITS_DEGRADED` a distinct verdict rather than a warning:
"adding nodes will not fix this" is in that reason string because it is true.

**`comm_buffer_bytes` charges expert parallel separately.** `EP_EXTRA_BUFFER_BYTES`
(2 GiB) on top of `COMM_BUFFER_BYTES` (1.5 GiB). Expert parallel is the term
other planners forget, and forgetting it produces a configuration that passes a
fit check and then OOMs on the first batch.

**The logits buffer is charged per emitted token, not per context token.**
One per sequence in flight. It still dominates on a large vocabulary:
GPT-OSS's 201k vocab is 12.9 MB per emitted token in fp32.

**The refusal string is rendered exactly as sent.** `ui/src/components/Verbatim.tsx`
uses `white-space: pre-wrap` and never truncates, reflows, sentence-cases or
paraphrases. Shortening a reason here does not make the UI tidier; it deletes the
sentence that says what to change.

## Failure behaviour

- **No nodes at all.** `_participating` raises `ValueError("fit check needs at
  least one NodeProfile")`. This is the one place the package raises rather than
  refusing — there is no budget to refuse against.
- **The plan names nodes with no profile supplied.** Budget against the rest and
  warn. If *no* profile matched, budget against every node supplied and warn.
- **No live reading for a node.** Fall back to that node's static ceiling and
  warn. Never refuse for want of a live number.
- **Unknown KV dtype.** Charge `KV_FALLBACK_DTYPE` (bf16) and warn, naming the
  byte count. Over-estimating is the safe direction.
- **Nodes that are not identical.** Budget against the smallest and name it.
  More than one GPU on a node warns that the budget is per rank and assumes one
  rank per GPU with the full addressable pool each.
- **PP exceeds the layer count, or does not divide it.** Warn, and budget
  against the busiest stage.
- **A `check()` that throws inside `capacity._walk`.** Logged with
  `log.exception` and turned into an `error` row reading "The fit gate could not
  be run for this model." One model's failure never takes the capacity table
  down.
- **`max_context` throws inside `ladder_context` or `context_for`.** Logged, and
  the rung is skipped or the context degrades to `FALLBACK_CONTEXT`. Never 0,
  which would be a refusal dressed as a choice.
- **No rung clears `MIN_USEFUL_CONTEXT`.** The best-quality rung that held any
  context at all is returned anyway — a cramped answer beats refusing to
  answer. Only when no rung holds even one page does `ladder_context` return
  `(CONTEXT_ROUNDING, None)`, which lets the following walk produce the gate's
  own refusal, naming the term and the overflow, rather than a sentence
  invented in that function.

`tests/unit/test_fit.py` (57 tests) and `tests/unit/test_live_memory.py` (34 tests) gate
all of it.

## Deliberately not built

**A planner.** `min_nodes_required` answers a memory question only — the fewest
nodes of this kind that hold the model under the most memory-efficient shard
available at each size. Whether that shard is a *good* plan at the measured link
bandwidth is `control_plane/planner/`'s call, and the synthetic
`ParallelismPlan`s this package builds for its searches carry
`measured_link_gbps=0.0` so they cannot be mistaken for one.

**An override.** There is no flag that turns a `WONT_FIT` into a launch. The
degraded verdict is the only "yes, anyway" the contract has, and it exists for
the case where the model genuinely loads.

**A second copy of the context rule.** `context_for` exists because the planning
path and the capacity probe had begun to grow one each, and two implementations
of "what context did we choose" is two answers to the question every verdict on
screen is taken at.
