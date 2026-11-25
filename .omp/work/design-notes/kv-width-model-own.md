# Design note: the KV width picker answers for THIS checkpoint

## Ask
User, on the Serve panel's "Memory — kv" list (model's own, bfloat16 x1.00, float16 x1.00, fp8 x0.50,
... dashes): "make [it] truly accurate with the model types". Acceptance: every row says what the
engine will actually store for this checkpoint on this runtime, the default row names its width and
multiplier, and a dashed row says why.

## What was wrong (evidence, pinned image vLLM 0.28.1rc1.dev486)
1. `fit/kv.py::kv_elem_bytes("auto", shape)` read `shape.dtype`, the WEIGHT scheme: an fp8/int8-weight
   checkpoint's own cache priced at 1 byte (vLLM stores 2) -- TurboQuant's boundary layers and every
   `auto` caller under-charged; an fp32 checkpoint priced at 4 (vLLM downcasts to bf16).
2. A checkpoint whose quantization config declares a KV scheme (ModelOpt `kv_cache_scheme` /
   `kv_cache_quant_algo`, compressed-tensors `kv_cache_scheme`; `nvidia/Llama-3.1-8B-Instruct-FP8`,
   `nvidia/Qwen3-8B-FP8`, the `qwen3-next-80b-nvfp4` fixture) is stored at fp8_e4m3/nvfp4/fp8 when vLLM
   is told nothing (`resolve_kv_cache_dtype_string`, `attention.py:276-286`). derate priced 16-bit and
   the default row named nothing.
3. `bfloat16` and `float16` both rendered no flag, so on a bf16 model "float16 x1.00" launched bf16.
   Rendering it would be worse: measured on the GB10 here, `reshape_and_cache_flash(bf16 key, float16
   cache, "float16")` copies the BITS (`kc.view(bfloat16) == k`).
4. One dash covered five reasons.

## Where it went (built)
- resolver: `quant_detect.py::checkpoint_kv_dtype`, `compute_dtype`, `loader_facts`; two
  `Resolution` fields; `cache.py` SCHEMA_VERSION 15 + both serializers + `_as_hit`. `resolver.py`
  changed net zero (`**loader`).
- fit: `kv_elem_bytes` prices own at 16 bits and lost its `shape` parameter (2 callers);
  `MODEL_OWN_KV_DTYPES` is the one home of "own" spellings, `fp16`/`bf16`/`half` included.
- deploy: `flags._KV_DTYPE_TO_VLLM` renders `float16`/`bfloat16` by name; the duplicate own tuple in
  `kv_cache_dtype_refusal` is gone (render already answers None for it).
- gateway: NEW `kv_widths.py` -- `own_width` (per-engine `_UNTOLD` table), `settle` (replaces the
  `kv_dtype_refusal` call and the settings default in `_plan_and_fit`), `passed` (both launch sites),
  `label` (the silicon refusal's name for a declared width), `refuse_kv_arg` (extra_args), `choices`
  (moved from `internal_api._kv_dtype_ratios`, plus own row, flag per width, reason per refusal).
  internal_api.py shrank ~40 lines.
- UI: `kvWidths.ts` (pure rows) + `kvWidths.check.mjs` + `KvWidthField.tsx`, out of AdvancedTab
  (now ~620 lines); `NO_KV_FLAG` deleted, flags come from the server; the kept answer in
  `useModelSession` is keyed on model AND runtime (net zero there).

## Decisions (after design-critic)
- A declared width is PRICED by name and NOT PASSED (critic #1, #10, #11): vLLM resolves the same
  width itself, so a custom command is not refused, a restart launches what a fresh launch does, and
  a record does not read as the operator's pick. The silicon check judges what the engine STORES.
- `float16`/`bfloat16` render by name (critic #3). `settle` hands the gate "auto" when the name is
  the compute dtype and no scheme is declared, so the flag is only ever passed to override a declared
  scheme; the other 16-bit name, or either when the compute dtype is unstated, is refused before
  anything renders. Stored records never held a 16-bit name (only rendered widths are stored).
- `fp16`/`bf16`/`half` stay "own" (critic #5; `test_gateway.py:6347`, `test_deploy.py:3909`).
- Per-engine rule is `kv_widths._UNTOLD`, not an `EngineSpec` field (critic #6): that dataclass is
  pinned field-for-field to its copy in over-budget `deploy/flags.py`; a test holds the table to
  `engines.SUPPORTED`. tts is bfloat16 (`runtimes/tts.py:688`), llamacpp f16.
- A refusal of the declared width says so and names the override (critic #4, `_declared_refusal`,
  `label`).
- `--kv-cache-dtype` in `extra_args` is refused (critic #2): with a declared default it would put a
  16-bit cache into an fp8 budget. `custom_command` stays the unpriced escape hatch it already is.
- Nested quant configs (`thinker_config.text_config`, Qwen3-Omni) are not read: unproven which config
  vLLM builds its quant object from, and "" over-charges, the safe direction.

## Out of scope, said in the report
Sparse-MLA auto -> fp8_ds_mla (no sparse detection; over-charges). Capacity/list rows and admission
still price 16-bit (over-charge a declared checkpoint, so two screens can differ). Autoadopt does not
parse `--kv-cache-dtype`. Gemma's float16 exclusion, pre-sm_80 fallback and vLLM's `is_pooling_model`
breadth change the label only, never the bytes.

## Verification
`tests/unit/test_kv_widths.py` (24, incl. two TestClient tests), `node
ui/src/tabs/models/kvWidths.check.mjs` (15), `servePanel.check.mjs` against a scratch coordinator,
screenshots of `nvidia/Llama-3.1-8B-Instruct-FP8` and `Qwen/Qwen3-8B-FP8`, the full suite, `cd .omp
&& npm run validate`.
