# Resolver, fit and planner brief

Owns: turning a HuggingFace id into a `ModelShape`, refusing a launch that will run out of memory, and choosing TP/PP/EP from measured facts.
Code: control_plane/resolver/, control_plane/fit/, control_plane/planner/   Tests: tests/unit/test_fit.py, test_live_memory.py, test_planner.py, test_resolver.py, test_model_corpus.py, test_imageprobe.py, test_speculative.py, test_spec_measure.py, test_integration_kv.py, tests/model_sweep.py   Read first: the three package READMEs (`control_plane/resolver/README.md`, `control_plane/fit/README.md`, `control_plane/planner/README.md`) and CLAUDE.md "Rules that are not style"

An agent reads Depth 1 for every part it touches, and goes deeper only as far as the task needs.
Long form lives in the three package READMEs; this page is the rules and the entry points.

## Depth 1 -- 30 seconds
- Three stages, in one direction: resolver (id -> shape, warnings, measured bytes) -> fit gate (shape + plan + nodes -> verdict) -> planner (shape + measured link -> TP/PP/EP). Contracts they share are in `control_plane/contracts/` and are frozen.
- The gate is one inequality per rank: weights + kv_cache + activations + comm_buffers + replicated + framework_overhead <= usable. There is no flag that turns WONT_FIT into a launch.
- Every refusal, warning and plan `reason` is a sentence the screen shows verbatim. A reason that does not name the term and the change that would work is a bug.
- Unknown is said out loud: no link measurement, no bandwidth, no live memory, no config key. Each degrades to a stated assumption or a refusal; none becomes a quiet guess.
- `calculator.py`, `resolver.py` and `planner.py` are at their line budget (0 lines left). New logic goes in a new module in the same package, not into those files.

## Depth 2 -- the rules
- **Every number in a refusal is verified, never extrapolated.** "Reduce context to N", "requantize to X", "spread over N nodes" come from searches that re-check themselves. Where: `control_plane/fit/calculator.py::FitCalculator._largest_context`, `control_plane/fit/calculator.py::FitCalculator._suggest_quant`, `control_plane/fit/calculator.py::min_nodes_required`.
- **A context of 0 never crosses the port.** 0 means "not one rounding step fits", which is not "zero tokens"; the wire carries `None`. Where: `control_plane/fit/calculator.py::_reported_context`.
- **Refusals are ordered: ranks, then memory, then the model's own window.** A weights-only overflow is refused as weights before a smaller context is suggested. A context past `native_window` is refused as a config limit, because vLLM refuses to start regardless of free memory (a launch died on this 30 minutes into a readiness wait). Where: `control_plane/fit/calculator.py::FitCalculator.check`, `control_plane/fit/calculator.py::FitCalculator._diagnose`.
- **Live memory is an optional kwarg that degrades.** `allocatable` replaces the static ceiling when present; an unread node falls back to its own ceiling with a warning; the gate never refuses for want of a live number. Where: `control_plane/fit/calculator.py::FitCalculator._budget`, `tests/unit/test_live_memory.py::test_absent_node_falls_back_to_its_ceiling_and_says_so`.
- **A measured `weight_bytes` beats `total_params * bytes_per_param`, and the larger wins.** Formulas miss padding and packing (about 3 GiB on GPT-OSS-120B). The measured figure is valid only at the dtype it was measured at, so quantization rungs below the native one use the formula and say so. Where: `control_plane/fit/calculator.py::weight_bytes_per_rank`, `control_plane/fit/capacity.py::_rungs`, `control_plane/resolver/types.py::Resolution.effective_weight_bytes`.
- **KV cache is sized from `num_kv_heads`, sliding windows and MLA latents, never from head count or context alone.** Query heads over-charge by the GQA ratio (8x on Llama 3.3 70B). A windowed layer caches the window. MLA charges the latent plus the RoPE half (dropping it is 11% low, in the OOM direction). Where: `control_plane/fit/kv.py::kv_cache_bytes`, `control_plane/fit/kv.py::kv_divisor`, `control_plane/fit/kv.py::cached_layer_tokens`.
- **The model's own KV width is never `shape.dtype`.** That is the WEIGHT scheme: an fp8-weight checkpoint caches at bfloat16, so `auto` is priced at 16 bits. On vLLM a KV scheme the checkpoint's quantization config declares is the own width, priced by name and never passed (vLLM reads it itself); a 16-bit name that is not the compute dtype is refused, because the engine copies the bits unconverted. Where: `control_plane/fit/kv.py::kv_elem_bytes`, `control_plane/gateway/kv_widths.py::own_width`, `control_plane/gateway/kv_widths.py::settle`, `control_plane/resolver/quant_detect.py::checkpoint_kv_dtype`.
- **Estimates err toward the refusal side.** Unknown KV dtype charges bf16; an unparseable quantization is bf16 with a warning; when the hub tally and the config disagree the larger footprint wins unless shard bytes arbitrate. Over-stating costs a refusal, under-stating costs an OOM. Where: `control_plane/fit/constants.py::KV_FALLBACK_DTYPE`, `control_plane/resolver/quant_detect.py::detect`, `control_plane/resolver/resolver.py::ModelResolver._measure_params`.
- **tok/s is per sequence and labelled so.** `predict_decode_tps` is a single-stream bandwidth model reading ACTIVE parameters; the cache it is handed is one sequence's whatever concurrency was asked. Do not pass concurrency in to "fix" it: that gives a slower per-sequence number, not an aggregate. Where: `control_plane/fit/calculator.py::predict_decode_tps`, `control_plane/fit/calculator.py::FitCalculator.check`.
- **The two efficiencies are the two ends of one measured range, neither an average.** 0.55 never over-promises (the degraded threshold and routing seed from it); 0.75 is a real upper bound. Where: `control_plane/fit/constants.py::DECODE_EFFICIENCY`, `control_plane/fit/constants.py::DECODE_EFFICIENCY_BEST`.
- **Speculative decoding is charged into the existing terms, and throughput is a floor and a ceiling, never one number.** The draft's weights go into `weights`, its positions into `kv_cache`, so every refusal string works unchanged. There is no assumed acceptance rate; a measured one narrows the range through the same arithmetic (all-accept reproduces the ceiling exactly). Where: `control_plane/fit/calculator.py::speculative_decode_tps_range`, `control_plane/fit/calculator.py::speculative_best_k`, `tests/unit/test_spec_measure.py::test_all_accepted_reproduces_the_ceiling_exactly`.
- **A speculative option that cannot be budgeted is offered but refused.** DSpark has `draft_params=None` and is not launchable. A config-only check is silent about separately published heads, so those go through `head_option`, which prefix-matches the class, requires matching `hidden_size`/`vocab_size`, refuses a declared target-type mismatch (segment match, absence is never a refusal) and refuses a head whose shards cannot be measured. Where: `control_plane/resolver/speculators.py::detect`, `control_plane/resolver/speculators.py::head_option`, `control_plane/resolver/speculators.py::target_type_conflict`.
- **ngram wins the ceiling by construction (`draft_ratio` 0) and must never be the auto-pick.** Test `draft_params == 0`, never falsy: `None` means the cost was not derived. Where: `control_plane/head_scan.py::recommend`, `control_plane/head_scan.py::WEIGHTLESS_NOTE`.
- **The resolver reads metadata, never weights and never `transformers`.** Resolution sits in a UI request path with a cold budget of seconds. A field the config never states is a documented assumption plus a warning in `Resolution.warnings`, which is shown beside any fit or plan. Where: `control_plane/resolver/hf.py::HubClient`, `control_plane/resolver/config_map.py::map_config`.
- **A hand-kept table about someone else's software is checked against evidence.** `VLLM_ARCHITECTURES` is generated from the pinned image's registry; the image probe prefers the image's own answer, is keyed by image id (never the tag), never pulls, and degrades to `None`. A refusal names the image version when a probe ran. Where: `control_plane/resolver/support.py::VLLM_ARCHITECTURES`, `control_plane/resolver/imageprobe.py::probe`, `control_plane/resolver/support.py::record_probe`.
- **Loadable and answerable are separate claims.** `TTS_ARCHITECTURES` means somebody ran that checkpoint through `control_plane/runtimes/tts.py`; `AUDIO_ARCHITECTURES` decides the route, not the loading. Where: `control_plane/resolver/support.py::TTS_ARCHITECTURES`, `control_plane/resolver/support.py::modality_for`.
- **A GGUF repo is a directory, and only some files are weights.** Projectors, imatrix files, `-mtp`/draft heads and big-endian rebuilds are excluded; a file that cannot be classified IS a weight file (a false inclusion is visible, a false exclusion hides a quantization). A resolved GGUF id has no `hf://` scheme, so test both spellings. Where: `control_plane/resolver/gguf_names.py::is_weight_file`, `control_plane/resolver/gguf_names.py::is_hub_gguf_blob`.
- **The resolution cache stores this build's opinions, not only facts.** Adding a runtime or a `Resolution` field means bumping `SCHEMA_VERSION` AND adding the field to both serializers, or an old entry reads back as the value that lets a bad model through. Where: `control_plane/resolver/cache.py::SCHEMA_VERSION`, `control_plane/resolver/cache.py::to_dict`, `control_plane/resolver/cache.py::from_dict`.
- **Nothing here hardcodes a bandwidth or a threshold.** The planner reads the measured link; shared thresholds live in `control_plane/contracts/constants.py::TP_VIABLE_THRESHOLD` and `control_plane/contracts/constants.py::EP_VIABLE_THRESHOLD` and are imported, never re-declared. Where: `tests/unit/test_planner.py::test_no_bandwidth_is_hardcoded_in_the_planner`, `tests/unit/test_planner.py::test_thresholds_come_from_contracts`.
- **No measurement means pipeline wins and the reason says so.** `measured_link_gbps=0.0` is a sentinel rendered "not measured". A collective latency that was not measured is charged `UNMEASURED_COLLECTIVE_LATENCY_US` and the sentence says so. Where: `control_plane/planner/planner.py::_Facts.tp_preferred`, `control_plane/planner/planner.py::_Facts.latency_clause`, `control_plane/contracts/constants.py::UNMEASURED_COLLECTIVE_LATENCY_US`.
- **A model that fits on one node is never split.** The spare node is worth more as a replica than as a rank paying interconnect on every token; widening is conditional on a whole second replica not fitting. Where: `control_plane/planner/planner.py::_family_rank`, `control_plane/planner/planner.py::_Scored.sort_key`.
- **Legality is decided before preference, and by one authority.** TP must divide both attention and KV head counts; EP must divide the expert count and equal `dp * tp`. The planner does not re-check "does it fit" (the fit gate says that, better); an operator's degrees go through the same refusal wording. Where: `control_plane/planner/legality.py::valid_tp_degrees`, `control_plane/planner/legality.py::ep_rejection`, `control_plane/planner/legality.py::check_degrees`.
- **`plan()` is the head of `alternatives()`, and `plan_for()` renders an operator's shape through the same path.** A gateway-written reason would be persisted on the deployment forever. A plan never lists its own shape in `rejected`. Where: `control_plane/planner/planner.py::Planner.plan`, `control_plane/planner/planner.py::Planner.plan_for`.
- **`min_nodes < 1` means impossible, not one node.** The fit gate returns `-1` for "no node count in range holds this"; clamping it to 1 made a single-node plan look legal. Where: `control_plane/planner/planner.py::_Facts.capacity_impossible`, `control_plane/planner/planner.py::_Facts.capacity_floor`.
- **Step-time estimates order candidates and are never shown as tok/s.** The user-facing rate is `predict_decode_tps`. Where: `control_plane/planner/comm.py::estimated_step_seconds`.
- **Unlike hardware is not pooled silently.** A mixed selection is partitioned into homogeneous groups; pooling is a stated permission with a warning. Where: `control_plane/planner/topology.py::homogeneous_groups`, `control_plane/planner/topology.py::pooling_note`.

## Depth 3 -- how to change it
**Teach the resolver a new config shape (a model that is refused or mis-sized)**
1. Add the real `config.json` to `tests/resolver_data/` as `<name>.config.json`.
2. Add the key spelling to the right tuple, or a rule, in `control_plane/resolver/config_map.py::map_config` (`_KV_HEAD_KEYS` and its siblings are the file); every field it cannot read must leave a warning. `config_map.py` has ~100 lines of headroom.
3. Run `python3 -m tests.model_sweep` (it fails on drift both ways), review the diff, then `python3 -m tests.model_sweep --regenerate`. Add a focused test in `tests/unit/test_resolver.py` pinning the field that mattered.
4. If you added a field to `Resolution`, bump `control_plane/resolver/cache.py::SCHEMA_VERSION` and update `to_dict` and `from_dict`.

**Add a quantization scheme or a GGUF file type**
1. Price it in `control_plane/contracts/quant.py::BYTES_PER_PARAM` (a frozen contract; announce it) and the dtype name in `control_plane/contracts/quant.py::normalize_dtype`.
2. Map the file type in `control_plane/resolver/gguf.py::GGUF_FILE_TYPES` and tensor block sizes in `control_plane/resolver/gguf.py::GGML_TYPES`; add a name pattern in `control_plane/resolver/quant_detect.py::from_name` only if repos name it.
3. Add the runtime-support rows in `control_plane/resolver/support.py` and a `control_plane/fit/constants.py::QUANT_SUGGESTION_ORDER` entry if the gate may suggest it.
4. Tests: `tests/unit/test_resolver.py` (detection) and `tests/unit/test_fit.py` (the weights term). Regenerate with `python3 -m control_plane.contracts.manifest --write` then `python3 -m control_plane.contracts.document --write`.

**Add a supported architecture or runtime**
1. Do not hand-edit `VLLM_ARCHITECTURES`: run `python3 -m tests.model_sweep --arch` and regenerate from the image, using the command in `control_plane/resolver/support.py`. A TTS name goes into `TTS_ARCHITECTURES` only after that checkpoint ran through `control_plane/runtimes/tts.py`.
2. A new runtime goes in `control_plane/resolver/support.py::RUNTIMES`, which is a declared copy of `control_plane/deploy/flags.py::SUPPORTED_RUNTIMES` (`tests/unit/test_single_source.py` fails if they differ), and bumps the cache `SCHEMA_VERSION`.
3. Tests: `tests/unit/test_resolver.py`, `tests/unit/test_imageprobe.py`; run `python3 -m tests.model_sweep --regenerate` if the corpus moved.

**Add a memory term or change how one is sized**
1. Put the arithmetic in `control_plane/fit/kv.py` or a NEW module; `calculator.py` has no headroom. Wire it through `control_plane/fit/calculator.py::memory_breakdown` so refusals, `_largest_context` and the capacity ladder all see it.
2. The refusal wording for the new term must name the term and a verified fix (see `_diagnose`).
3. If the launch must carry it, the recipe path in `deploy/` carries it or refuses (a priced thing that is silently dropped is the failure; see `tests/unit/test_integration_kv.py`).
4. Tests: `tests/unit/test_fit.py` (breakdown shifts by exactly the term; refusal names it), `tests/unit/test_live_memory.py` if it touches the budget. Extend the exports in `control_plane/fit/__init__.py` only for a function another package needs.

**Change how a plan is chosen or explained**
1. Legality goes in `control_plane/planner/legality.py`; arithmetic in `control_plane/planner/comm.py`; the decision in `control_plane/planner/planner.py::_family_rank`/`_Scored.sort_key`, which has no headroom, so move helpers out first.
2. A new tunable gets a measurement in its comment and goes in `control_plane/planner/constants.py`; anything shared goes in `control_plane/contracts/constants.py`, announced.
3. Update the matching rejection and justification sentences together; a plan must stay self-consistent (`reason`, `rejected`, `warnings`).
4. Tests: `tests/unit/test_planner.py`, including an "answer flips when only the measurement changes" case. Regenerate contracts only if a contract changed.

**Judge a new speculative method**
1. Detection from the config goes in `control_plane/resolver/speculators.py::detect`; a head in its own repo in `head_option`. Give it `draft_params` (an int) or `None` with `launchable` False; never an estimate.
2. Price it through `control_plane/fit/calculator.py::speculative_weight_bytes_per_rank` and `speculative_kv_bytes_per_rank`; the runtime must have a flag for it, else `control_plane/deploy/flags.py::speculative_refusal` refuses.
3. Tests: `tests/unit/test_speculative.py`, `tests/unit/test_spec_measure.py`.

## Pitfalls
- **Stale support table.** `Gemma4ForConditionalGeneration` was refused for weeks while the image loaded it; `MllamaForConditionalGeneration` was claimed while the image could not. Both directions fail; `python3 -m tests.model_sweep --arch` finds them and says "unchecked" (not "agreed") when the image cannot be read.
- **A shape that moves is a regression either way.** If a corpus number changes, either the old one was wrong (the gate has been lying) or the new one is. `tests/model_sweep.py::compare_corpus` reports regressions and improvements separately; do not `--regenerate` to make it green.
- **The planner takes no fit object.** `Planner()` with no argument resolves the module-level helpers; passing a `FitCalculator` skips the `hasattr` gate and raises `AttributeError` at the first capacity question. Where: `control_plane/planner/fit_bridge.py::default_fit_helpers`.
- **Fit helpers disappearing from `__all__` swaps in the fallback silently.** `min_nodes_required` and `kv_bytes_per_token` in `control_plane/fit/__init__.py` are looked up by name; removing one is not a refactor.
- **A ladder needs one context, not one per rung.** Per-rung "largest that fits" makes the native dtype always win and makes a variants table incomparable. Use `control_plane/fit/capacity.py::ladder_context`; both clamps (`MIN_USEFUL_CONTEXT`, the model's window) matter, and a rung holding nothing is skipped on the raw figure.
- **Two answers to "what context did we choose".** `control_plane/fit/capacity.py::context_for` is the one rule, probed by `inspect.signature` (not `try/except TypeError`, which swallows genuine errors). Do not add a copy.
- **`-1` rendered as a node count.** It named the remedy the search had just ruled out. `_nodes_phrase` handles it; a machine with no addressable memory gets its own sentence keyed on the ceiling, not the live budget.
- **Equal-looking figures.** "15.6 GiB against 15.6 GiB left" reads as impossible; `control_plane/fit/calculator.py::_gib_vs` widens precision until they differ.
- **Tensor parallel past the KV heads.** TP cannot shard KV beyond `num_kv_heads`, so TP=8 on a 4-KV-head model replicates; the shard search must include pipeline degrees, or a pipeline split that holds comfortably is reported impossible.
- **Pipeline at concurrency 1 buys capacity, never speed.** The bubble `(p-1)/p` cancels the `1/p` compute saving exactly (`control_plane/planner/comm.py::pipeline_bubble_fraction`). A "more nodes = faster" claim at single stream is wrong.
- **TP's exchange count is a floor.** `num_layers * ALLREDUCES_PER_LAYER` exchanges do not shrink with degree, so latency, not bandwidth, stops TP scaling; compression does not help.
- **Expert parallel is a recommendation gate and an override.** The planner will not choose cross-node EP on a two-Spark cluster (needs the bandwidth threshold, GPUDirect RDMA and `MIN_NODES_FOR_CROSS_NODE_EP` nodes), but `plan_for` plans an operator's EP and must say it was asked for, not chosen.
- **A single-node plan names one host even at world size > 1.** `plan_for` matches on kind as well as the degree tuple; a single-node TP=4 and a cross-node TP=4 are different plans.
- **Resolved GGUF ids lose their scheme.** `hf://owner/repo/file.gguf` becomes `owner/repo/file.gguf`; a predicate that tested for `hf://` read hub files as local paths. One predicate (`is_hub_gguf_blob`), both spellings in tests.
- **Header probes spend the hub's rate limit.** `_MAX_GGUF_REPOS` and `_MAX_HEADER_PROBES` bound one click; when the budget is spent say the file was priced at the default, never that a header "could not be read".
- **Substring variants.** `Qwen3-30B-A3B` also matches `-Thinking-2507`; `ModelResolver._quant_stem` demands an exact stem after suffix stripping, so a different model is not offered as a quantization.
- **A GGUF-only repo's `config.json` says bf16.** That repo is `launchable=False`; the confident dtype is the most misleading answer the resolver can give.
- **Publisher marks are resolved once, by the coordinator.** Letting each browser resolve ~45 publishers on every page load hit the hub's rate limit and the grid fell back to monograms for half an hour. `control_plane/resolver/avatars.py` caches them; the UI subscribes once per list (see the UI brief).

## Functions
- `control_plane/fit/calculator.py::FitCalculator.check` -- the gate; returns a `FitResult`; branch on `.ok`, never on `verdict is FITS`.
- `control_plane/fit/calculator.py::FitCalculator.max_context` -- largest verified context for a plan; frozen port signature plus probed extras.
- `control_plane/fit/calculator.py::memory_breakdown` -- the six terms; the single place a new term is added.
- `control_plane/fit/calculator.py::weight_bytes_per_rank` -- weights per rank, measured bytes first, vision share subtracted.
- `control_plane/fit/calculator.py::comm_buffer_bytes` -- comm plus the separate expert-parallel staging charge.
- `control_plane/fit/calculator.py::min_nodes_required` -- fewest nodes that hold a shape, searching TP and PP shards; `-1` means impossible.
- `control_plane/fit/calculator.py::predict_decode_tps` -- per-sequence decode estimate from active parameters; used by the card and the router.
- `control_plane/fit/calculator.py::speculative_decode_tps_range` -- floor and ceiling for a drafted decode; never one number.
- `control_plane/fit/calculator.py::speculative_best_k` -- the draft length that maximizes a measured acceptance curve.
- `control_plane/fit/kv.py::kv_cache_bytes` -- the real KV total (windows, MLA, dtype); use this, never rate times context.
- `control_plane/fit/kv.py::kv_bytes_per_token` -- the full-attention marginal rate only; the planner and admission use it.
- `control_plane/fit/capacity.py::largest_runnable` -- what runs on these machines as they stand, one row per shape.
- `control_plane/fit/capacity.py::ladder_context` -- the one context to judge a whole quantization ladder at.
- `control_plane/fit/capacity.py::context_for` -- min(native window, largest that fits); the only "what context" rule.
- `control_plane/fit/capacity.py::probe_plan` -- a stated placement with `measured_link_gbps=0.0`, never mistaken for a planner result.
- `control_plane/fit/constants.py::QUANT_SUGGESTION_ORDER` -- the schemes the gate will suggest, best quality first; also the default ladder.
- `control_plane/planner/planner.py::Planner.plan` -- the recommendation; `alternatives` gives the ranked list it heads.
- `control_plane/planner/planner.py::Planner.plan_for` -- plan an operator's chosen degrees with the planner's own reason text.
- `control_plane/planner/legality.py::valid_tp_degrees` -- degrees that divide both head counts; ask this before offering a TP.
- `control_plane/planner/legality.py::check_degrees` -- refuse illegal degrees in the planner's words; raises `IllegalDegrees`.
- `control_plane/planner/legality.py::enumerate_candidates` -- hybrid TP x PP x EP x DP candidates; only arithmetic ranks them.
- `control_plane/planner/comm.py::tensor_bytes_per_step` -- ring all-reduce volume per token; the numbers in reasons are computed here.
- `control_plane/planner/topology.py::homogeneous_groups` -- partition nodes into pools safe to serve one model across.
- `control_plane/planner/fit_bridge.py::default_fit_helpers` -- the two fit numbers the planner needs, with a loud conservative fallback.
- `control_plane/resolver/resolver.py::ModelResolver.resolve_full` -- id (hub, local dir, local or `hf://` GGUF) to a `Resolution` with warnings and measured bytes.
- `control_plane/resolver/resolver.py::ModelResolver.quant_variants` -- the launchable quantizations of a model, each a repo id with measured size.
- `control_plane/resolver/resolver.py::ModelResolver.supported_by` -- whether a runtime can load this shape; says "unverified" when no architecture is on record.
- `control_plane/resolver/config_map.py::map_config` -- `config.json` to shape fields, explicit rules, warnings for anything assumed.
- `control_plane/resolver/quant_detect.py::detect` -- the quantization, in order of trust, never a smaller guess.
- `control_plane/resolver/params.py::reconcile` -- combine the analytic split with the measured total (MTP subtracted, active params derived).
- `control_plane/resolver/gguf.py::read_gguf_file` -- header and tensor directory of a GGUF, sized per tensor; never reads weights.
- `control_plane/resolver/speculators.py::head_option` -- price and vet a draft head published in its own repo.
- `control_plane/resolver/imageprobe.py::probe` -- ask the runtime image what it can load; cached by image id, never pulls.
- `tests/model_sweep.py::compare_corpus` -- the hermetic corpus check behind `tests/unit/test_model_corpus.py`.
