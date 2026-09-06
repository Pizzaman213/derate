# Agent D: Fit Calculator

Read `00-architecture.md` first. Contracts there are frozen.

**You own:** `control_plane/fit/**`, `tests/test_fit.py`
**You depend on:** contracts, `ModelShape` from C, `NodeProfile` from A
**Downstream of you:** F refuses to launch on your verdict. G refuses to admit requests on your numbers.

---

## What you build

The blocking pre-launch out-of-memory gate.

Everything else in this space estimates and then lets you launch anyway. sparkrun prints a fit verdict that does not stop anything and has a known bug producing wrong utilization figures. Dynamo's profiler emits configurations that are guaranteed to OOM because it under-counts expert-parallel buffers. This component refuses, and when it refuses it names the term that blew the budget and what to change.

That refusal is the highest-value, lowest-effort gap in the whole landscape. Get it right.

---

## The budget

Per node:

```
weights + kv_cache + activations + comm_buffers + replicated + framework_overhead
    <= usable_memory
```

### Weights

```python
total_weight_bytes = shape.total_params * shape.bytes_per_param()

if pipeline_parallel > 1:
    # Pipeline splits can be uneven. Charge the busiest stage, not the average.
    layers_on_busiest = ceil(shape.num_layers / pipeline_parallel)
    stage_fraction = layers_on_busiest / shape.num_layers
    per_node = total_weight_bytes * stage_fraction / tensor_parallel
else:
    per_node = total_weight_bytes / tensor_parallel
```

### KV cache

Per token, across all layers, before sharding:

```python
per_layer_per_token = 2 * shape.num_kv_heads * shape.effective_head_dim * kv_elem_bytes
```

Use `num_kv_heads`. Not `num_attention_heads`. This is the most common error in the field and it over-estimates by the grouping ratio.

Three branches:

**Multi-head latent attention.** When `mla_latent_dim` is set, the cache is one compressed vector per layer per token, not per-head K and V: `num_layers * mla_latent_dim * elem`.

**Sliding window.** When `sliding_window` and `layers_with_full_attention` are set, full layers cache the full context and windowed layers cache `min(sliding_window, context)`. Sum the two groups separately. Treating a windowed model as fully cached over-estimates by multiples.

**Standard.** `num_layers * per_layer_per_token * context * batch`.

Sharding: tensor parallel shards KV by head, pipeline parallel shards it by layer. When both are active the divisor is their product.

### Activations

Logits buffer plus compute scratch:

```python
logits = shape.vocab_size * batch * 4
scratch = chunk * shape.hidden_size * 4 * 6      # chunk defaults to 2048
```

The logits term dominates for large vocabularies. Charge it for tokens actually emitted per step, not for the full context.

### Communication buffers

`COMM_BUFFER_BYTES` (1.5 GiB) whenever world size exceeds 1. Add `EP_EXTRA_BUFFER_BYTES` (2.0 GiB) on top when expert parallel is active, and attach a warning saying so.

Expert-parallel staging is precisely the term other planners forget. Under-counting it is what produces configurations that pass a fit check and then OOM on the first batch. Be conservative here on purpose.

### Framework overhead

`FRAMEWORK_OVERHEAD` (1.0 GiB) for CUDA context, allocator fragmentation, and runtime. Flat.

### Usable memory

`NodeProfile.usable_memory(DEFAULT_GUARDRAIL)`, which is 0.90 of addressable. On GB10 that is roughly 107.7 GiB of the 119.7 GiB addressable, not 128.

---

## Predicted throughput

Fitting and being usable are different questions. A dense 70B loads across two Sparks and decodes at a few tokens per second. Say so before the load, not after.

```python
weight_bytes_per_token = shape.effective_active_params * shape.bytes_per_param()
bytes_per_token = weight_bytes_per_token + kv_read_bytes_per_token
ceiling = (memory_bandwidth_gbps * 1e9) / bytes_per_token
predicted = ceiling * 0.55        # real runtimes land near half the ceiling
```

Use **active** parameters, not total. This is why GPT-OSS-120B with 5.1B active runs an order of magnitude faster than a dense 70B on identical hardware, and it is the single most useful thing the UI can tell someone before they wait five minutes for a load.

Below `DEGRADED_TPS_THRESHOLD` (10 tok/s), return `FITS_DEGRADED` with a reason saying the model is bandwidth bound and that adding nodes will not fix it.

---

## The diagnosis

When the verdict is `WONT_FIT`, the reason string is the product. Work out which term is responsible.

**Weights alone exceed usable.** Nothing will help except more nodes or a smaller quantization. Say how many nodes would be needed, and if quantization does not close the gap either, say that too rather than implying it might.

Worked example, `deepseek-v3` on two Sparks. Every figure here comes from `contracts/constants.py` and the shape, not from a round number someone liked:

```
weights           671_026_419_200 B x 1.0 (fp8)          = 624.94 GiB
usable per node   int(119.7 GiB) x 0.90 guardrail        = 107.73 GiB
usable, 2 nodes                                          = 215.46 GiB
overflow                                                 = 409.48 GiB

per-node fixed    1.5 comm + 2.0 EP + 1.0 framework      =   4.50 GiB
weights budget    107.73 - 4.50                          = 103.23 GiB
min nodes, fp8    ceil(624.94 / 103.23)                  = 7
min nodes, q4_k_m 624.94 x 0.6125 = 382.78, ceil(/103.23) = 4
```

Round for display only at the end, never during. Rounding the operands first gives an overflow of 409.4, and someone checking your subtraction by hand against the displayed figures is exactly the person this string is written for.

Which produces:

> DeepSeek-V3 needs 624.9 GiB of weights at fp8. Two Sparks give 215.5 GiB usable. Over by 409.5 GiB on weights alone, before any KV cache. Seven nodes would hold it at fp8, four at Q4_K_M. No quantization in the table reaches two.

Note what the arithmetic forced: the obvious closing line, "try a smaller quantization," is false here, and only computing it revealed that. Never suggest a remedy you have not evaluated.

This is the string someone screenshots. It is generated, never hand-written, and the test asserts the numbers against the constants so a guardrail change cannot silently make it a lie.

**KV is the swing term.** Solve for the context length that fits and say it:

```python
fixed = weights + activations + comm_buffers + replicated + framework_overhead
kv_budget = usable - fixed
per_token = kv_bytes_per_token(shape, kv_dtype) / kv_divisor * max_seqs
max_context = floor(kv_budget / per_token) rounded down to 512
```

Then: "Over budget by 12.4 GiB. KV cache is the problem: 34.1 GiB at 32768 tokens by 16 sequences. Drop context to 18944 tokens, reduce concurrency, or quantize the KV cache to fp8."

**Combined.** No single term dominates. List all of them with their sizes.

A reason that says only "does not fit" has failed. Every refusal names the term, the overflow, and the specific change that would work.

---

## Interface you must satisfy

```python
class FitPort(Protocol):
    def check(self, req: FitRequest, nodes: list[NodeProfile]) -> FitResult: ...
    def max_context(self, shape, plan, nodes, max_seqs, kv_dtype) -> int: ...
```

Plus:

```python
def kv_bytes_per_token(shape: ModelShape, kv_dtype: str) -> float
def min_nodes_required(shape, node_profile, context, max_seqs) -> int
def predict_decode_tps(shape, bandwidth_gbps, kv_read_bytes) -> float
```

`kv_bytes_per_token` and `min_nodes_required` are called by Agent E. Export them cleanly.

---

## Day 0 stub

Return `FITS` with a plausible breakdown for anything smaller than 100 GiB and `WONT_FIT` above. F and G need something callable immediately.

---

## Acceptance

- Llama 3.3 70B at bf16 on one Spark: `WONT_FIT`, limiting term `weights`, minimum 2 nodes.
- DeepSeek-V3 at fp8 on two Sparks: `WONT_FIT`, limiting term `weights`, minimum 7 nodes. Assert the overflow figure is derived from `GB10_ADDRESSABLE * DEFAULT_GUARDRAIL` rather than a literal, by changing the guardrail in the test and asserting the string changes with it.
- Same model at fp8 on one Spark at 4096 context: fits, predicted decode under 10 tok/s, verdict `FITS_DEGRADED`.
- GPT-OSS-120B MXFP4 on one Spark: fits, predicted decode well above threshold, because active parameters are 5.1B not 120B.
- A grouped-query model computes KV using `num_kv_heads`. A test must assert that using `num_attention_heads` would produce a figure larger by exactly the grouping ratio.
- A sliding-window model produces a KV figure below the naive full-cache calculation.
- An MLA model produces KV roughly an order of magnitude below the equivalent non-MLA calculation.
- Every `WONT_FIT` includes a non-empty `limiting_term` and, when KV is responsible, a `max_context_that_fits` that actually fits when fed back in. Assert the round trip.
- Predictions land within 20 percent of measured peak memory on at least three real models.

## Traps

Do not use `num_attention_heads` for KV. Do not treat pipeline stages as evenly sized. Do not skip expert-parallel buffers. Do not let `FITS_DEGRADED` be treated as failure by callers, it loads fine and sometimes that is what someone wants. Do not report a context suggestion you have not verified fits.
