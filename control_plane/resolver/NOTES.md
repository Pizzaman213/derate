# Resolver notes for D, E, F and G

Six things the type signature does not tell you.

**1. Use `resolve_full()` when a number matters; `resolve()` when only the shape does.**

```python
from control_plane.resolver import ModelResolver

resolver = ModelResolver()
shape = resolver.resolve("openai/gpt-oss-120b")        # the ResolverPort method
res   = resolver.resolve_full("openai/gpt-oss-120b")   # shape + warnings + support + real bytes
```

`Resolution.warnings` is where every assumption is recorded. If you show a fit or a
plan to a human, show these too: they say which numbers were read and which were
inferred.

**2. `weight_bytes` beats `total_params * bytes_per_param()` on mixed-precision repos.**

GPT-OSS keeps attention and embeddings in bf16 while the experts are MXFP4, so the
dtype figure under-states it by about 5 percent — 3 GiB on a 120B model, which is
the difference between fitting and not. `Resolution.effective_weight_bytes()`
returns the larger of the two and is what the weights term should charge. It is
`None` only when no source could measure it.

**3. Active parameters exclude the input embedding, include the output head.**

Decode reads the whole output projection every token and one row of the embedding
table, so the embedding is not streamed and is not counted. Published "active"
figures are inconsistent about this: GPT-OSS's 5.1B agrees with us, Qwen's 3.3B
counts the embedding. Expect us to sit within a few percent of either.

`effective_active_params` is `total_params` for dense models.

**4. `mla_latent_dim` is `kv_lora_rank` alone, per the frozen fixture.**

DeepSeek-V3 reports 512. The runtime also caches a 64-wide decoupled RoPE
component per layer per token, so the true cached width is 576. If your KV
arithmetic uses `mla_latent_dim` directly, it under-counts by an eighth. The
resolution carries a warning saying so on every MLA model.

**5. `layers_with_full_attention` is `None`, not `num_layers`, when there is no window.**

`None` means every layer caches the full context. `0` means every layer is
windowed. Anything between is a real interleave: GPT-OSS-120B is 18 of 36,
Gemma 3 27B is 10 of 62.

**6. Fitting and loading are different questions.**

```python
ok, reason = resolver.supported_by(shape, "vllm")
ok, reason = resolver.supported_on(shape, "vllm", nodes)   # also checks compute capability
```

`supported_by` reads the architecture out of the resolution cache, so call it on a
shape this resolver produced. A fixture shape reports "unverified" rather than a
guess. NVFP4 needs Blackwell outright; MXFP4 runs emulated below it, and on
pre-Hopper the runtime widens the weights to bf16, which quadruples the footprint
the fit check was told to expect.

## Where the numbers come from

| Field | Source | Fallback |
|---|---|---|
| layers, hidden, heads, KV heads, head_dim | `config.json`, explicit keys only | none, it is an error |
| `total_params` | hub's safetensors tally, arbitrated against shard bytes | analytic split, warned |
| `active_params` | measured total minus the experts the router skips | none |
| `dtype` | `quantization_config`, then `hf_quant_config.json`, then `torch_dtype`, then the repo name | bf16, warned |
| `weight_bytes` | root-level shard sizes, or summed GGUF tensor sizes | `None` |
| `vision_params` | analytic from `vision_config` | 0 |

Resolution is cached on disk under `$SPARKPLANE_CACHE_DIR`, `/data/cache/resolver`
in the container. Cold is around 100 ms, cached is microseconds. `ShapeCache` is
safe to share between components; pass one in if you want a single warm cache.
