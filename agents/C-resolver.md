# Agent C: Model Resolver

Read `00-architecture.md` first. Contracts there are frozen.

**You own:** `control_plane/resolver/**`, `control_plane/contracts/quant.py`, `tests/test_resolver.py`
**You depend on:** contracts
**Downstream of you:** D and E both take `ModelShape` as their primary input. Every number they produce is wrong if yours is.

---

## What you build

A HuggingFace model ID goes in. A complete, correct `ModelShape` comes out.

You also own `BYTES_PER_PARAM`, the quantization table the whole system uses.

---

## 1. Metadata sources

**HuggingFace `config.json`**, fetched via the hub API without downloading weights. This is the primary path.

**`model.safetensors.index.json`** for the true total parameter count. Sum the shard sizes and divide by bytes per element rather than trusting a parameter count in the model card, which is often rounded or absent.

**GGUF header** when the model is a GGUF file. Read the KV metadata: `block_count`, `attention.head_count`, `attention.head_count_kv`, `embedding_length`, `general.file_type`, `expert_count`, `expert_used_count`. Sum per-tensor byte sizes for an exact weight figure rather than multiplying a parameter count by a nominal bit width.

Cache resolved shapes on disk keyed by model ID and revision. Resolution should be fast enough to sit in a UI request path.

---

## 2. Field mapping

Config keys vary by architecture. Handle at minimum:

- `num_hidden_layers` or `n_layer` to `num_layers`
- `hidden_size` or `d_model` to `hidden_size`
- `num_attention_heads` or `n_head` to `num_attention_heads`
- `num_key_value_heads` to `num_kv_heads`, defaulting to `num_attention_heads` when absent, which means multi-head attention with no grouping
- `head_dim` when explicitly present, since some architectures do not satisfy `hidden_size / num_heads`

**Grouped-query attention is the field most often gotten wrong.** Using `num_attention_heads` where `num_kv_heads` belongs over-estimates KV cache by the grouping ratio, which is 4x or 8x on common models. Agent D's entire calculation depends on you getting this right.

### Mixture of experts

Set `num_experts` from `num_local_experts` or `num_experts`, and `num_experts_per_token` from `num_experts_per_tok`. Compute `active_params`: shared and attention parameters plus `num_experts_per_token / num_experts` of the expert parameters. This drives predicted decode speed, which depends on active parameters, while capacity depends on total. Both matter and they differ by an order of magnitude on models like GPT-OSS-120B.

### Sliding window attention

Set `sliding_window` and `layers_with_full_attention`. Gemma, Llama 4, and GPT-OSS interleave full-attention and windowed layers. Windowed layers cache the window, not the context, which changes KV totals by multiples. GPT-OSS-20B caches full KV on 12 of 24 layers and a 128-token window on the rest.

### Multi-head latent attention

DeepSeek-family models cache a compressed latent per layer instead of per-head K and V. Set `mla_latent_dim` from the config's KV compression dimension. This cuts KV by roughly an order of magnitude and Agent D branches on it.

### Vision towers

Set `vision_params` from the vision config. These are replicated on every node when sharding, never split, so they are a fixed per-node cost.

---

## 3. The quantization table

Real bytes per parameter, including block scales and zero points. Nominal bit width understates footprint and is the systematic error in every napkin calculator.

```python
BYTES_PER_PARAM = {
    "fp32": 4.0, "fp16": 2.0, "bf16": 2.0,
    "fp8": 1.0, "int8": 1.0,
    "q8_0": 1.0625,     # 8.5 bpw
    "q6_k": 0.82,       # 6.56 bpw
    "q5_k_m": 0.711,    # 5.69 bpw
    "q4_k_m": 0.6125,   # 4.90 bpw, not 4.0
    "q4_0": 0.5625,     # 4.5 bpw
    "q3_k_m": 0.456,    # 3.65 bpw
    "q2_k": 0.329,      # 2.63 bpw
    "mxfp4": 0.53125,   # 4.25 bpw, E2M1 plus E8M0 scale per 32 elements
    "nvfp4": 0.5625,    # 4.5 bpw, E2M1 plus FP8 scale per 16, Blackwell native
    "awq_int4": 0.5625,
    "gptq_int4": 0.5625,
    "nf4": 0.5163,
}
```

Detect quantization from `quantization_config` in the config, from the GGUF file type, or from the repo name as a last resort. When it cannot be determined, default to bf16 and add a warning rather than guessing low.

---

## 4. Runtime support check

Weights fitting is not the same question as the runtime being able to load the model. Return a support verdict alongside the shape: whether vLLM and SGLang support this architecture, and whether the quantization scheme needs a compute capability the target nodes have. MXFP4 needs Blackwell for native acceleration. A model that passes the byte check and fails the architecture check should be blocked before launch, not at load time.

---

## Interface you must satisfy

```python
class ResolverPort(Protocol):
    def resolve(self, model_id: str, dtype: str | None = None) -> ModelShape: ...
```

Plus:

```python
def resolve_gguf(self, path: str) -> ModelShape
def supported_by(self, shape: ModelShape, runtime: str) -> tuple[bool, str]
def available_quants(self, model_id: str) -> list[str]
```

---

## Day 0 stub

Return the four fixture shapes by ID and raise a clear error otherwise. D and E cannot start without this.

---

## Acceptance

- Llama 3.3 70B resolves to 80 layers, 8192 hidden, 64 heads, 8 KV heads. The KV head count is the one that must be exactly right.
- GPT-OSS-120B resolves as MoE with correct total and active parameter counts, MXFP4 quantization, and its sliding window pattern.
- A DeepSeek model populates `mla_latent_dim`.
- A model with no `num_key_value_heads` defaults `num_kv_heads` to `num_attention_heads`.
- Total parameters computed from the safetensors index are within 1 percent of the published count.
- Resolution completes in under 2 seconds cold, under 50 milliseconds cached.
- An unknown quantization defaults to bf16 with a warning, never to a smaller value.

## Traps

Do not compute total parameters as `layers * hidden^2 * constant`. Read the index. Do not assume `head_dim == hidden_size / num_heads`, several architectures break it. Do not trust the model card. When a field is genuinely absent, be conservative and say so in a warning; an optimistic default here becomes an OOM downstream.
