# resolver

A HuggingFace model id in, a complete `ModelShape` out. Every field is either
read from real metadata or accompanied by a warning saying it was not, because
the fit gate's memory arithmetic and the planner's parallelism choice are both
wrong if the KV head count, the active parameter count or the quantization is
wrong here.

Two questions are answered, and the cheaper one first. *What shape is this
model* is `resolve()` / `resolve_full()`. *Can the runtime actually load it* is
`support.py`, and a model that clears the byte check and fails that one should
be refused before launch rather than at load time, five minutes in.

```python
from control_plane.resolver import ModelResolver

resolver = ModelResolver()
shape = resolver.resolve("openai/gpt-oss-120b")       # the ResolverPort method
full  = resolver.resolve_full("openai/gpt-oss-120b")  # + warnings, support, real bytes
```

## Layout

| File | Lines | What it owns |
|---|---|---|
| `resolver.py` | 1212 | `ModelResolver`: `resolve`, `resolve_full`, `resolve_gguf_full`, `quant_variants`, `search_models` |
| `support.py` | 639 | the architecture and quantization tables, per runtime, and the refusal wording |
| `config_map.py` | 634 | `config.json` keys to shape fields, explicitly rather than cleverly |
| `gguf.py` | 330 | the GGUF header and tensor directory, summed per tensor |
| `hf.py` | 305 | hub access: metadata only, never weights |
| `avatars.py` | 291 | publisher marks, resolved coordinator-side and cached forever |
| `quant_detect.py` | 282 | which quantization scheme a repo ships, in order of trust |
| `params.py` | 277 | parameter accounting by bucket, reconciled against the weight index |
| `speculators.py` | 227 | which speculative-decoding methods a checkpoint declares, and what each costs |
| `cache.py` | 250 | the disk cache: sha-pinned entries never expire, floating refs get a TTL |
| `imageprobe.py` | 248 | asks the runtime image what it can load, instead of keeping a list |
| `types.py` | 223 | `Resolution`, `RuntimeSupport`, `QuantVariant` — the provenance `ModelShape` has no room for |
| `gguf_names.py` | 167 | what a `.gguf` filename tells you, and that a projector is not a model |
| `stub.py` | 78 | the day-0 `ResolverPort`; an unknown id is an error, not a guess |
| `__init__.py` | 64 | the export surface, and the import path other packages use |
| `NOTES.md` | 79 | six things the type signature does not tell a downstream caller |

## `resolver.py`

`ModelResolver.resolve_full(model_id, dtype=None, revision="main", *, refresh=False)`
is the whole answer: a `Resolution` carrying the shape, the commit sha it was
read at, where the parameter count and the dtype came from, the runtime support
verdict, the measured on-disk weight bytes and every assumption made along the
way. `resolve()` is the same call with everything but `ModelShape` thrown away,
and it is the only method `contracts/ports.py::ResolverPort` declares.

The same entry point takes four kinds of subject: a hub id, a local directory
with a `config.json`, a local `.gguf` file, and `hf://owner/repo/file.gguf`.
Only the first and the last are cached — a path on disk can be replaced or
deleted under a long-lived resolver, and re-reading a header is already as cheap
as a cache hit.

`_fetch_metadata` submits the model record and `config.json` to a two-worker
pool and waits on both, because they are two round trips that do not depend on
each other and resolution sits in a UI request path.

`_measure_params` is where a number is decided rather than computed.
`_TALLY_DISAGREEMENT_LIMIT` is 0.15: above that the hub's own safetensors tally
is not believed, because it has been seen to count storage elements rather than
logical weights on exotic 4-bit packings, and being 40 percent wrong about a
70B model is not a rounding error. When the tally and the analytic estimate
disagree, shard bytes divided by bytes-per-parameter arbitrate — a measurement
neither figure can argue with — and the warning names the GiB that decided it.
With nothing to arbitrate, the larger wins: over-stating the footprint costs a
refusal, under-stating it costs an OOM.

### `quant_variants` is the shortlist a person picks from

A quantization is a different repository, not a flag: neither serve command
template carries `--quantization` and the only model identifier they
interpolate is the repo id. So `quant_variants(model_id)` returns
`QuantVariant` rows that each name a launchable `repo_id`, and
`available_quants` is now derived from it so the two can never disagree about
what exists.

Three budgets keep one click off the hub's rate limit. `_MAX_GGUF_REPOS` (6)
covers the publishers who maintain distinct ladders without turning one click
into thirty model-info requests. `_MAX_HEADER_PROBES` (3) is scoped to the
whole enumeration rather than per repository — six repos at three probes each
is eighteen multi-second ranged reads, measured at 2.4-3.3s per file against
the live hub, which is the 20s timeout again with extra steps. When the budget
is spent the remaining files are priced at the default and **say so**: the note
must never claim a header could not be read when it was never opened.

`_quant_stem` decides what is a variant of what, and it demands an exact match
after suffix stripping rather than a substring. Searching `Qwen3-30B-A3B`
returns `-Thinking-2507` and `-Instruct-2507`, whose names contain the stem and
which are different models; offering them as quantizations presents a 21 GB
download as a smaller build of the thing you asked for.

A repository that ships nothing but `.gguf` files is marked `launchable=False`
even though its leftover `config.json` reads `torch_dtype: bfloat16`. That
confident bf16 is not a set of weights anything here loads, and it is the single
most misleading answer this resolver can give about a quantization catalogue.

## `support.py`

`evaluate_runtime(runtime, architectures, dtype)` answers per runtime and
`build_verdict` answers for all three at once. `RUNTIMES` holds `vllm`,
`sglang` and `tts`; `contracts/derived.py` records it as a copy of
`deploy/flags.py::SUPPORTED_RUNTIMES`, so `tests/unit/test_single_source.py` fails if
the two stop naming the same set.

`VLLM_ARCHITECTURES` is generated from the image this project launches —
`ghcr.io/spark-arena/dgx-vllm-eugr-nightly`, vLLM 0.28.1rc1.dev462, read on
2026-09-07 — not curated by hand, and the module carries the command that
regenerates it. An absent name is not a hedge: the verdict is UNSUPPORTED,
`RuntimeSupport.ok` is False, and `internal_api` turns that into a 400
`runtime_unsupported`. Thirteen names left the set on that sync, including
`MllamaForConditionalGeneration` — Llama 3.2 Vision — which the pinned image
cannot load. `SGLANG_ARCHITECTURES` is still hand-kept, and the docstring says
why: that image is not on this box, so there is no registry to read and no
honest way to generate it.

`VLLM_KNOWN_BROKEN` is the third state, for an architecture the registry lists
and the runtime cannot actually run. `DiffusionGemmaForBlockDiffusion` is its
one entry: weights load, `torch.compile` succeeds, and it dies in CUDA graph
capture handing FlashInfer's prefill `plan()` a tensor-shaped causal mask —
`TypeError: Mismatched type on argument #14 ... Expected 'bool' but got
'ffi.Tensor'`, on 4 consecutive launches of `google/diffusiongemma-26B-A4B-it`,
2026-09-07.

`TTS_ARCHITECTURES` is a claim about `control_plane/runtimes/tts.py`, not about
the world. That server calls exactly `processor(...)`, `generate() -> codes`
and `decode_audio(codes)`, so a name belongs here once somebody has run that
checkpoint through that server — never because the model card says TTS. It
holds `ArkttsModel`, verified on a GB10 against
`Audio8/Audio8-TTS-Preview-0.6b`.

`AUDIO_ARCHITECTURES` decides the route rather than the loading. Loadable and
answerable on `/v1/chat/completions` are separate claims, and keeping them
separate is what stops a transcription-only checkpoint being offered as a chat
model. Only the architectures vLLM marks `supports_transcription_only` are
listed; GlmAsr, GraniteSpeech, Qwen3ASR, Qwen3OmniMoe, MoonshotKimia and
Gemma3n transcribe *and* chat, and stay on the chat route because taking a chat
model off it is the louder failure.

`_elsewhere` is why a refusal here is worth reading. "Not in vllm's list" is
true and leaves the reader nowhere to go; this module is the only place that
knows the tts runtime loads that same checkpoint on `/v1/audio/speech`, so it
says so in the same sentence.

## `config_map.py`

`map_config(config)` returns a `Mapped`, and every field it could not read
plainly leaves a warning behind. The rules are explicit rather than clever
because a silently wrong `num_kv_heads` over-charges or under-charges the KV
cache by the grouped-query ratio, which is 4x or 8x on the models people
actually run.

The key lists are the file. `_KV_HEAD_KEYS` carries `n_local_heads`, from
gpt-fast's `ModelArgs` by way of the DualAR speech checkpoints: the name reads
like a tensor-parallel shard count and is not one, and without it a
14-head/2-KV-head model was charged as multi-head and its cache came out seven
times too large. `_EXPERT_TOPK_KEYS` carries `top_k_experts`, Gemma 4's
spelling; absent, a config stating `top_k_experts: 8` fell through to the
assumed 2, a four-fold understatement of active parameters on a model whose own
config said the number plainly.

`_discover_stacks` is the fallback for a wrapper nobody has taught this module
about, and it is reached only where `map_config` used to raise — so it cannot
change a model that already resolves, only turn a refusal into a shape.
`Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` keeps its stack under `talker_config`
and was refused outright before it existed. The ranking is what makes guessing
safe: Qwen3-Omni serves from `thinker_config.text_config`, 48 layers of 2048
with 128 experts, while the only candidate one level down is `code2wav_config`,
an 8-layer vocoder. Largest wins, `_MAX_STACK_DEPTH` is 3, and the warning names
the stack chosen and every one rejected — because KV cache per token is charged
against the stack named there.

`_map_sliding_window` reads every convention published for the same fact —
`layer_types`, `sliding_window_pattern`, gemma2's implicit half, Qwen2's
`max_window_layers`, Llama 4's `no_rope_layers` — and prefers an explicit
`layer_types` list over all of them, since it is the only unambiguous source. `_map_mla` reads
DeepSeek's own numbers rather than deriving them: its query heads are 192 wide
against 128-wide value heads and neither equals `hidden_size /
num_attention_heads`. A config with `is_encoder_decoder: true` falls back to the
decoder's own layer and head counts and says so — Whisper large-v3 publishes
`max_target_positions: 448` and nothing under any of the usual window names, so
without that key the planner would offer a context vLLM then refuses to start
with.

## `gguf.py`

Reads the KV metadata block and the tensor directory, never the weights. Byte
sizes come from summing each tensor's own block-quantized size, so a mixed-quant
file — which every llama.cpp release produces, since attention and embedding
tensors are left wider than the MLP — is measured rather than approximated from
a nominal bit width. `read_gguf_file(path)` reads a local file; `RangeReader`
walks a remote one forward in 4 MiB HTTP ranges.

`GGML_TYPES` maps each ggml type to elements and bytes per block, and
`GGUF_FILE_TYPES` maps llama.cpp's `LLAMA_FTYPE` onto a key in
`BYTES_PER_PARAM`. Both tables record fixes rather than opinions. File types 3,
8 and 9 all used to answer `q4_0`, charging 4.5 bits per weight for formats
costing 5.0, 5.5 and 6.0 — a third under on the worst of them, and under is the
direction that becomes an out-of-memory kill minutes into a load. 19 and 20
answered `q2_k` and are IQ2_XXS and IQ2_XS; 21 is Q2_K_S; everything from 22 to
31, the rest of the importance-matrix family, was absent outright, so
`dominant_file_type()` returned `None` for those files and they were charged at
the bf16 default -- four to eight times their real footprint, which is what
made every Unsloth GGUF repo look like it would not fit. 36 and 37 stay out deliberately, because `BYTES_PER_PARAM` has no
ternary key and adding one is a change to a frozen contract.

Token vocabularies run to hundreds of thousands of strings, so an array longer
than 4096 entries is walked past and kept as an `_ArrayStub` that knows only its
own length. That is all the header read needed from it.

## `hf.py`

`HubClient` is one warm `requests.Session` and nothing else: plain GETs against
the hub API and `resolve` URLs, no `transformers`, no shard downloads, on the
two-second cold budget the docstring commits to. `DERATE_HF_TIMEOUT` (8.0)
bounds every call and `hf_token()` reads `DERATE_HF_TOKEN`, then `HF_TOKEN`,
then `HUGGING_FACE_HUB_TOKEN`.

`ModelInfo.shard_bytes()` counts only root-level files matching
`model*.safetensors`. Repos carry duplicate copies of the same weights —
GPT-OSS ships an `original/` tree, Mistral ships `consolidated.safetensors`
beside the sharded files — and counting either doubles the weight footprint.

`count_safetensors_params` unpacks packed formats back into logical weights:
MXFP4 puts two 4-bit values in a byte, GPTQ and AWQ put eight in an int32.
Scales, zero points and `g_idx` are storage, not parameters, and are counted in
the bytes but not in the total.

Two error paths are deliberate. A 401 or 403 says the repo may not exist *or*
may be gated, because the hub answers 401 for both and sending someone hunting
for a token when they have a typo wastes their afternoon. And `search` raises on
429 and 5xx rather than returning `[]` — "no such model" and "the hub would not
answer" are different facts, and returning the empty list for both makes a rate
limit look like an empty catalogue.

## `avatars.py`

A publisher's own mark, resolved once by the coordinator and kept on disk. The
Models grid draws ~90 cards from ~45 distinct publishers, and every browser used
to resolve every one of them itself, on every page load, against the hub's
unauthenticated and hard-limited overview endpoint. Forty-five lookups is over
that limit on its own, so the grid reliably 429ed itself: the client backed off
60s and doubled to half an hour, and for that whole window every card fell back
to two letters. What made it look intermittent is that the limit is a burst
window — the same page is fine ten minutes later and letters again after a
reload.

Resolving coordinator-side collapses forty-five lookups per browser, tab and
reload into forty-five lookups total, and a restart re-reads them from
`data_dir()/avatars`. `_MAX_CONCURRENT` is 6, because moving forty-five
simultaneous requests from the browser to the server would have changed nothing.

`resolve_many` returns **three** answers, not two: `True` (cached, the image
route will serve it), `False` (this publisher has no mark), and absent (not
determined yet, ask again). The third state is what lets a cold cache work
without the client running a timer. The miss is read by *kind* rather than by
freshness for the same reason — a rate-limited lookup and a publisher with no
avatar are both fresh misses, and reporting the second as `False` tells the
client to stop asking, which is the entire failure this path was built to end.

`asyncio.wait` with a `DEFAULT_DEADLINE_S` of 6.0, never
`wait_for(gather(...))`: that one cancels what has not finished, throwing away
a lookup already in flight so the next batch pays for it again. Stragglers keep
running behind the answer, held in `_running` because a bare task is
collectable and a collected task is a fetch that silently never happened.

## `quant_detect.py`

`detect(config, model_id, *, override=None, hf_quant_config=None)` returns
`(dtype key, QuantSource, warnings)` in order of trust: caller override, then
`quantization_config`, then the `hf_quant_config.json` sidecar, then
`torch_dtype`, then the repo name. When none of them say, the answer is bf16 and
a warning — never a smaller guess, because a low guess turns into an
out-of-memory kill minutes into a load rather than a refusal before it.

`_NAME_PATTERNS` is the last resort, and each late addition to it repairs an
over-charge rather than an under-charge: the default it rescues a file from is
bf16, the largest footprint in the table. The `_XL` sizes sit above the bare
`[sml]` rules because that class cannot match "XL", and without them an Unsloth
Dynamic file fell through to bf16 and was charged four times what it costs. `Q2_K_S`, `Q2_K_L`, `Q6_K_L` and `Q8_0_L` had
no pattern at all, because `_` is a word character and `\b` never fires between
"k" and "_"; the bf16 default over-charges Q6_K by 2.5x. llama.cpp's ARM
repackings `Q4_0_4_4`, `_4_8` and `_8_8` are the same Q4_0 blocks interleaved
for i8mm — same 4.5 bits per weight, same bytes — and they fell to the default
at 2.0 bytes per parameter against a real 0.5625, a 3.6x over-charge, while each
one also cost a multi-second ranged read that could not answer either.

`_from_compressed_tensors` reads the scheme out of `config_groups` rather than
off a method name, and `group_size == 16` is what separates nvfp4 from mxfp4 at
4 bits. A method this table cannot size warns and returns `None`, which lands
on bf16 — stated in the warning as over-stating the footprint, so nobody reads
it as a measurement.

## `params.py`

Two jobs. `analytic_breakdown(m, vision_cfg)` splits parameters into buckets —
embedding, lm_head, attention, dense MLP, routed experts, shared experts,
router, norms, MTP, vision — because capacity depends on the total while decode
speed depends on what is read per token, and on GPT-OSS-120B those differ by an
order of magnitude. `reconcile(m, breakdown, measured_total)` then combines that
split with the real count off the weight index. The analytic figure is never the
answer for `total_params`; it is the ratio that divides a measured total.

A weight index counts the multi-token-prediction module and no runtime loads it
unless speculative decoding is on, so it is subtracted and the warning names the
billions removed. `ParamBreakdown.as_dict` publishes both `total` and
`total_with_mtp`, because a reader comparing our figure against the repo's own
"N B parameters" needs the second to see why they differ.

Active parameters exclude the input embedding and include the output head:
decode reads the whole output projection every token and one row of the
embedding table, so the embedding does not stream. The routed-expert share is
capped at 0.995 of the total rather than something rounder, because Kimi K2 is
98.9 percent routed experts and a lower cap would refuse to split it. A drift
above 5 percent between the measured and analytic totals warns that the active
split is approximate.

## `speculators.py`

`detect(mapped, breakdown, ...)` answers which speculative-decoding methods a
checkpoint can be served with, and what each one costs. Two rules.

**Read what the config declares, never the architecture name.** There is more
than one mechanism and a checkpoint can carry two at once:
`tests/resolver_data/deepseek-v4-flash.config.json` has
`num_nextn_predict_layers: 1` *and* `dspark_block_size: 5`. A table keyed on
`DeepseekV4ForCausalLM` would have to pick one; the config says both, in fields
the runtime reads too. `ngram` is offered for every model including the ones
that declare nothing — it drafts by matching output against the prompt, loads
no weights, and needs no support from the checkpoint. That is what makes a
dense model like Qwen3-8B answerable on its own screen rather than blank.

**Never offer what cannot be budgeted.** DSpark comes back with
`draft_params=None` and `launchable=False`: `dspark_target_layer_ids` names
layers the model already has, so it is not a decoder layer's worth of new
weights the way the MTP module is — but "not that" is not a figure, and this
build has not read the rank-256 markov head's parameterisation out of a
checkpoint. It is detected, named, and refused. The whole product is a gate
that refuses launches which will run out of memory, and a method whose weight
cost is unknown is a launch nothing has checked.

The MTP cost is derived as the exact inverse of `resolver.py`'s own
subtraction — `weight_bytes_after * mtp / total_params`, not
`mtp * bytes_per_param` — so turning speculative decoding on adds back
precisely what excluding the module took away. On a mixed-precision repo those
two differ by gigabytes, and the difference lands in the fit gate.

`head_option(head, base_shape, ...)` is the other half, and it is the one that
reaches models the target's config cannot describe. A draft head published in
its own repository -- `AngelSlim/Qwen3-4B_eagle3`, `RadixArk/Qwen3.8-27B-DSpark`
-- is a repo like any other, so the same mapper sizes it and the same weight
index measures it. Four things must hold, each a sentence rather than an
exception: the class it declares must be recognised (prefix-matched, because
the pinned image registers 61 speculator classes and grows every release), the
image must register it, `hidden_size` and `vocab_size` must match the target
(a head reads the residual stream directly, so a mismatch is a load failure and
not a quality question), and its shards must be *measured* -- the analytic split
for a head is a floor, landing 16 percent low on the one head whose shards can
be counted, and under-charging is the wrong direction for a memory gate.

This is also why `Qwen/Qwen3-Next-80B-A3B-Instruct` reports no MTP: its own
config declares no `num_nextn_predict_layers` while the image loads
`Qwen3NextMTP` perfectly well from a separate repository. A config-only check
is right about the config and silent about the ecosystem, and `head_option` is
where the ecosystem is answered.

What the *image* can load is a separate question and lives in `imageprobe.py`:
that module now keeps the `_SPECULATIVE_DECODING_MODELS` set it used to compute
and discard. A method the config declares and the image cannot load is a launch
that clears every gate here and dies at load.

## `cache.py`

Two tiers: a 128-entry in-process LRU in front of a directory of JSON files,
keyed by model id, revision and dtype override. An entry pinned to an exact
40-hex commit sha — the only revision spelling git guarantees is immutable —
never expires. A floating ref expires on `DERATE_RESOLVER_TTL` (24 hours). A
non-positive TTL does not mean "cache forever"; it means the cache is disabled
and every read counts as stale, because 0-as-infinite is the trap a caller
reaches for when they mean "no TTL configured".

`SCHEMA_VERSION` is 4, and the last bump moved no field. A cached resolution
stores facts about the model *and* this build's opinions about it — the support
verdict per runtime and the warnings derived from it — so a build that adds a
runtime would read yesterday's answer and report that nothing can load a model
it now serves. The entries would have aged out within the TTL anyway; making it
immediate matters because the stale reading is `launchable: false` on exactly
the models the new runtime exists for.

`default_cache_dir()` is `data_dir() / "cache" / "resolver"` unless
`DERATE_CACHE_DIR` names somewhere else. It used to answer the question itself
— check `/data`, fall back to `~/.cache/derate` — and so disagreed with
`control_plane/paths.py`: an operator who set `DERATE_DATA_DIR` to move the
estate found the resolver cache left behind in `~/.cache`, reachable only
through a differently-named variable. Writes go through a temp file and
`os.replace`, so a reader never sees a half-written entry, and an unwritable
cache is a pass, not a resolution failure.

## `imageprobe.py`

The support table is a hand-copied claim about somebody else's software and it
goes stale in the one direction nobody notices: it keeps refusing models that
started working. `Gemma4ForConditionalGeneration` was refused with "not in
vllm's supported architecture list" while the pinned image had been able to load
it all along, and the same morning the same list was refusing DeepSeek-V4,
Qwen3-Next, Qwen3.5/3.6/3.8, LFM2.5 and llava-onevision. vLLM knows the answer
exactly — its model registry is a dict in the image — so `probe(runtime, image)`
runs `python3 -c` inside the image and reads it back behind a `derate-imageprobe:`
line prefix, findable in the middle of the INFO lines vLLM writes about Triton
and CUDA on import.

Three rules, each one a simpler version of this getting it wrong:

- **Never pull.** `docker image inspect` runs first and a miss is `None`. The
  image is 24 GB; pulling it inside a resolve would turn a page load into a
  half-hour download on a machine that may not even be the one serving models.
- **Cache by image id, not by tag.** The pinned tag is
  `dgx-vllm-eugr-nightly:latest` and it moves. Keyed by the tag, the entry would
  still be answering for last week's image — exactly the staleness this exists
  to end. Keyed by the id, a moved tag is a miss and re-probes.
- **Degrade, never raise.** No docker, no daemon, no image, a timeout, a
  non-zero exit, an unparseable answer: all `None`, and the caller keeps its
  static table. Same contract as the fit gate's live-memory kwarg.

The marker is lower-case and hyphenated on purpose. Spelled in the shouting
style this project uses for environment variables it was picked up by
`test_single_source`'s scan of the tree and reported as an undeclared variable.

## `types.py`

The provenance that `ModelShape` has no room for. `Resolution` is shape plus
revision, `ParamSource`, `QuantSource`, `SupportVerdict`, warnings, measured
`weight_bytes`, architectures, the analytic breakdown and the cache/timing
flags. `ParamSource` and `QuantSource` are both ordered best-first, so a reader
can see at a glance whether a number was counted or guessed.

`effective_weight_bytes()` returns the larger of the measured figure and
`total_params * bytes_per_param()`, and that is what a weights term should
charge. `Resolution.modality` is derived rather than stored, so nothing has to
be re-cached when a new architecture joins the audio table and a resolution
written by an older build still reports the current answer.

`QuantVariant` keeps `label` beside `dtype` on purpose: `dtype` is the canonical
key this codebase prices with, `label` is what the publisher called it, and
rendering "q4_k_m" for a file named "UD-Q4_K_XL" would paraphrase a string
somebody else chose. `file_bytes` is a measurement or it is `None`, never a
table estimate. `QuantRequirement.check(compute_capability)` is the silicon
question, and it returns a sentence either way — NVFP4 needs Blackwell outright,
MXFP4 runs emulated below it.

## `gguf_names.py`

A GGUF repository is a directory of files and only some of them are weights.
Vision projectors, importance matrices, speculative-decoding draft heads and
big-endian rebuilds sit in the same listing under the same extension, and a
variant list that takes the extension at its word offers a 600 MB projector as a
model you could run.

`is_weight_file` excludes four things, each a real file that reached the variant
list: `mmproj-*`, a leading `imatrix` token, `*-mtp`, and a trailing `*-be`.
Also AppleDouble sidecars — a macOS publisher uploading from Finder ships one
`._name.gguf` per real file, which arrives as an exact duplicate listing at a
few KiB each. `imatrix` is matched only as the *leading* token, because
`Model-IQ4_XS-imatrix.gguf` is a real quantization advertising how it was made
and a substring test would throw away the entire IQ ladder of every repository
that labels it.

The bias is deliberate and one-directional: a file this module cannot classify
is a weight file. A false exclusion hides a quantization that exists; a false
inclusion shows one extra row with a measured size beside it, which a person can
see and dismiss.

`shard_family` and `variant_stem` group `-00001-of-00002` families into one
download while keeping `IQ4_XS-3.53bpw` and `IQ4_XS-4.19bpw` apart — the same
scheme at two sizes, and collapsing them would hide one build behind the other's
size. `quant_token` preserves the publisher's own case, and the module states
plainly that `quant_detect.from_name` answers the other question: its answer for
`UD-Q4_K_XL` is `q4_k_m`.

## `stub.py`

`StubResolver` implements `ResolverPort` against the day-0 fixtures, so the fit
and planner packages could be built against real contract types before this
package existed. It never invents a shape: an unknown id raises `ModelNotFound`
naming the four ids it knows, rather than returning a plausible guess that
silently poisons a memory calculation. `resolve_gguf` refuses outright, and
every `Resolution` it returns carries the warning "shape came from the day-0
fixture stub, not from the hub". `_FIXTURE_ARCHITECTURES` exists because
`ModelShape` carries no architecture field, so without it the stub's support
verdict would be unverified for everything.

`gateway/deps.py` still falls back to its own `stubs.StubResolver` when no
resolver is passed, which is why `node.py` builds `GatewayDeps` with
`strict=True`.

## `__init__.py`

The import path. `ModelResolver`, `StubResolver`, `HubClient`, `ShapeCache`,
`GGUFHeader`/`read_gguf_file`, `ParamBreakdown`/`analytic_breakdown`,
`check_nodes`/`quant_requirement`, every type in `types.py`, and
`BYTES_PER_PARAM`/`bytes_per_param`/`normalize_dtype` re-exported from
`contracts/quant.py` so a caller pricing a resolution does not need a second
import.

## `NOTES.md`

Six things a downstream caller cannot read off the type signature, and the table
of where each field comes from and what it falls back to. The one to read first
is that `Resolution.warnings` is where every assumption is recorded: show them
alongside any fit or plan shown to a human, because they say which numbers were
read and which were inferred. The rest cover `weight_bytes` beating the dtype
formula on mixed-precision repos, what "active parameters" includes and why
published figures disagree with ours, that `mla_latent_dim` is `kv_lora_rank`
alone and `effective_mla_rope_dim` carries the rest, and that
`layers_with_full_attention` is `None` for "no window" and `0` for "every layer
windowed" — GPT-OSS-120B is 18 of 36, Gemma 3 27B is 10 of 62.

## The seam with the gateway and the launch path

`contracts/ports.py::ResolverPort` declares exactly one method,
`resolve(model_id, dtype=None) -> ModelShape`. Everything else this package
offers is an extra that consumers probe for with `getattr` rather than assume —
`gateway/serialize.py` and `gateway/internal_api.py` both say so in their own
docstrings — so a stub or a third-party port never has to grow a method to stay
compatible.

- **`node.py::build_gateway_deps`** is the composition root and the only place
  that knows both which images this build launches and which component needs to
  ask them what they can load. It builds `ModelResolver(runtime_images={...})`
  from `deploy/flags.py::RUNTIMES` filtered by `imageprobe.SCRIPTS`. Every
  import of this package there is function-local, so a worker process never
  pulls it in.
- **`gateway/app.py`'s lifespan** calls the first of `load_cache`, `load` or
  `start` the port actually has, bounded by `startup_step_timeout_s` (5.0s).
  `ModelResolver.start()` therefore returns immediately and probes on a daemon
  thread named `derate-imageprobe`: a container start takes fifteen to thirty
  seconds, and a step that timed out would be recorded as degraded *and* throw
  the answer away.
- **`gateway/internal_api.py`** prefers `resolve_full` when the port has it,
  falls back to `resolve`, and imports `support.modality_for` to decide which
  endpoint family a deployment answers on. It also owns the two avatar routes:
  `GET /api/publishers/avatars` resolves a batch, `GET
  /api/publishers/{owner}/avatar` serves only what is already cached.
- **`gateway/capacity_api.py`** takes `search_models`, `quant_variants`,
  `resolve_full`, `support.RUNTIMES`, `hf.hf_token` and the resolver types.
- **`tests/model_sweep.py`** imports `map_config`, `support` and `imageprobe`
  directly. See below.

## The model tester

`tests/model_sweep.py` (723 lines) is how a change to this package is proved not
to have moved a number, and `tests/unit/test_model_corpus.py` gates the suite on it.

```bash
python3 -m tests.model_sweep              # the checked-in corpus, hermetic, ~1s
python3 -m tests.model_sweep --live       # every model the coordinator knows
python3 -m tests.model_sweep --arch       # VLLM_ARCHITECTURES against the image
python3 -m tests.model_sweep --regenerate # rewrite EXPECTED.json
```

**Drift fails in both directions, and they are reported as different events.**
The corpus mode resolves all 26 `tests/resolver_data/*.config.json` against
`EXPECTED.json` beside them. A regression is a bug to fix; an improvement — a
model that used to be refused and now resolves — is a diff to review and then
`--regenerate`. Reading them as one list buries the difference, and one session
produced both within an hour. A shape that *moved* is a regression whichever way
it went: either the old number was wrong and the fit gate has been lying, or the
new one is, and both want a person rather than a regenerate. A config in the
corpus with no entry in `EXPECTED.json` is a regression too — a fixture that
runs and asserts nothing is worse than either kind of drift.

Refusal is not itself a failure. Two fixtures are *supposed* to be unreadable —
a CTranslate2 export (`Systran/faster-whisper-base`) and a static embedding model
(`minishlab/potion-base-8M`), neither of which has a transformer in it — and a
sweep that flagged them would cry wolf on every run.

The corpus then runs a second time with the image's registry recorded, because
**a shape must not depend on which runtime image is installed.** The mapper
never asks a runtime anything, and `compare_probed` is where that is proved
rather than assumed. Its second output is information rather than a failure: the
models the pinned image can serve that the static table alone would refuse.

**`--arch` catches the two opposite failures a hand-kept table has.**
`architecture_divergence` diffs `VLLM_ARCHITECTURES` against what the pinned
image's registry actually holds. A name the image loads and the table refuses is
a 400 on a servable model — `Gemma4ForConditionalGeneration`, for weeks. A name
the table claims and the image cannot load clears every gate and dies at load —
`MllamaForConditionalGeneration`. When the image cannot be read here the answer
is `None` and the run reports **unchecked**, never agreement: silence and a
clean bill of health must not look the same.

`--live` sweeps the coordinator's catalogue, and only a shape this build cannot
read is flagged. Provider-offered models get their own column rather than a
failure — they are served over somebody's API and have no HuggingFace repo, so
"no local shape" is correct for them. Gated repos, 404s and network wobbles are
counted apart too, because each is a fact about the hub rather than about this
build.

## Things that look like details and are not

**The warnings are the product, exactly as the fit gate's refusals are.**
`Resolution.warnings` is not diagnostics — it is the record of which numbers
were read and which were inferred. They reach the screen as
`result.resolver_warnings` and `ui/src/tabs/models/Verdict.tsx` draws them
through `VerbatimList`, unmodified. The arbitration warning — "the hub counts
NB parameters where the config describes NB; N GiB of shards at <dtype> back the
config, which was used" — is the whole argument for a number somebody is about
to launch against, and summarising it deletes the argument.

**`weight_bytes` and `total_params * bytes_per_param()` are different questions
and the larger wins.** GPT-OSS keeps attention and embeddings in bf16 while the
experts are MXFP4, so the dtype figure understates it by about 5 percent — 3 GiB
on a 120B model, which is the difference between fitting and not.

**An architecture is not carried on `ModelShape`, so `supported_by` reaches back
into the cache.** `_architectures_for` looks the shape's own model id up in the
resolution cache under both `None` and its dtype. A shape that was never
resolved here — a fixture — reports no architecture and the support check says
unverified rather than inventing a verdict. Same limitation on `modality_of`,
where the fallback is "text", which means the model stays on the routes it
always had.

**The probe result is recorded in a module-level dict and preferred over the
static tables for every question `support.py` answers.** `record_probe` is
duck-typed rather than typed against `imageprobe.ImageProbe`, because `support`
is imported by the fit and launch paths on every node and the probe is a
coordinator-side convenience not every node has a docker socket to run. Empty is
the normal state, and it costs nothing but the older answer.

**A refusal names the image and its version when a probe has run.** "Not in our
list" and "not in the registry of the image we launch" are different strengths
of claim. `ImageProbe.provenance` renders
`ghcr.io/... (vLLM 0.28.1rc1.dev462)`, and `RuntimeSupport.version` is carried
on every verdict — not only the ones whose reason mentions it — so a screen can
show which build okayed a model that was never refused.

**A GGUF shape is for planning, not for a launch.** `_resolution_from_gguf`
appends that warning to every resolution it builds, and every `QuantVariant`
naming a single `.gguf` file is `launchable=False`: both serve command templates
take a repository path, and no runtime here claims to load llama.cpp's format.

## Failure behaviour

- **The hub is unreachable, slow or rate limiting.** `MetadataUnavailable`, with
  the URL and the status. `_safe_model_info` swallows it for the model record
  alone and carries on from `config.json` with a warning saying parameter counts
  now come from the config rather than the weight index.
- **The repo is gated, private or misspelled.** One message naming both
  possibilities and telling the reader to check the id first and set `HF_TOKEN`
  second.
- **No `config.json`.** `ModelNotFound`, suggesting the `.gguf` file be resolved
  directly — which is the right advice for a GGUF-only repository.
- **No transformer stack anywhere in the config.** `map_config` raises `KeyError`
  naming the missing fields *and* how many sub-configs were searched, which
  `_build` turns into `UnsupportedArchitecture`. Naming what was looked in is
  what separates "we do not understand this wrapper" from "there is no attention
  in this checkpoint at all".
- **A field the config never states.** A documented assumption and a warning:
  32000 vocab, `4 x hidden` intermediate, `num_kv_heads == num_attention_heads`
  (never 1), tied word embeddings, 2 experts per token, a 4096 window.
- **No docker, or the runtime image is not on this machine.** The probe returns
  `None`, the static tables answer, and `log.info` says which runtime kept its
  static table. Nothing fails a startup or a resolve.
- **An unwritable or corrupt cache.** A miss. `put` swallows `OSError`, `get`
  swallows a bad JSON or a schema mismatch, and either way the model resolves
  again.
- **The hub will not serve an avatar.** Every failure is a miss, a miss draws a
  monogram, and nothing on this path is awaited by a request that matters. A
  non-200 is remembered as a transient backoff, never as "this publisher has no
  mark".
- **Offline (`DERATE_OFFLINE=1`).** Anything not already cached raises
  `MetadataUnavailable` naming offline mode; `search_models` raises rather than
  returning an empty list; `quant_variants` returns what the cache holds. A
  local directory and a local `.gguf` still resolve, because neither needs the
  network.

`tests/unit/test_resolver.py` (162 tests), `tests/unit/test_model_corpus.py` (23),
`tests/unit/test_imageprobe.py` (18) and `tests/unit/test_avatars.py` (8) gate all of it.

## Deliberately not built

**A shape from `transformers`.** Importing it would be the obvious
implementation and it is not on the table: resolution sits in a UI request path
with a two-second cold budget, and `AutoConfig.from_pretrained` is a heavyweight
import that will also happily reach for weights.

**A resolve per search hit.** `search_models` returns names, popularity, tags
and a `quant_hint` guessed from the name, and marks the payload
`resolved: False` on the wire rather than leaving the client to infer it from
missing keys. Resolving there would be one network round trip per row per
keystroke.

**A probe script for SGLang.** `imageprobe.SCRIPTS` holds vllm only. SGLang's
registry is a different shape and the pinned image is not on the box this was
written on, so a script for it would be guesswork wearing the costume of
evidence. That runtime keeps its static table and the module says so.

**A ternary key in `BYTES_PER_PARAM`.** GGUF file types 36 and 37 (`TQ1_0`,
`TQ2_0`) are left unmapped rather than approximated, because adding the key is a
change to a frozen contract and belongs there rather than here.

**A blocking avatar image route.** `/api/publishers/{owner}/avatar` serves what
is cached and 404s otherwise; it never fetches. On HTTP/1.1 a browser opens about
six connections per origin, so an image route that waited on the network would
sit on all six and starve `/api/*` behind it.
