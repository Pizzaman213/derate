# TODO

Future features to implement, consolidated from `ROADMAP.md`,
`00-architecture.md`'s dated appendices, and `ui/mockups-next/derate.html`'s
"not built yet" card. See `ROADMAP.md` for the full reasoning behind each
onboarding/model-surface/cluster item; this file is the flat checklist.

## Evidence

- [x] **Measure PP=2 against TP=2 on two Sparks and put real numbers
      somewhere public.** Done for `openai/gpt-oss-120b` at 131072 context and
      concurrency 16 on `spark-4d38` and `spark-26af`, which is what the
      README's cut "Why this is defensible" section would have needed. The
      result argues against this project's own planner, so it is written down
      here in full rather than summarised.
      **The link was never the blocker it was described as.** `spark-4d38 <->
      spark-26af` reads 5.728 GB/s all-reduce -- `ib_write_bw` at 13.639 GB/s
      raw scaled by the 0.42 NCCL ratio, 1.44 us, GDR off, "2 of 4 QSFP ports
      up". The old note here read `measured: false` as though something were
      broken; nothing was. There is deliberately no timer and no on-join probe
      (`links/README.md`; `gateway/app.py` says so at the startup step), so
      `POST /api/links/measure` had simply never been called -- 0 hits in
      31,829 lines of coordinator log. It takes 12 seconds. Note 13.64 GB/s raw
      is well under the 24.6 the docs cite for a GB10 pair: it is about
      109 Gb/s, one PCIe Gen5 x4 cable's ceiling, which is what "2 of 4 ports
      up" means. Cabling the second pair of cages is the thing to test before
      treating this as the hardware's number.
      **The measurement.** Both arms were given identical KV budgets --
      `--kv-cache-memory-bytes 38744883200`, `--max-model-len 131072`,
      `--max-num-seqs 16` -- so the only difference between them is the
      parallelism. `tests/load/loadtest.py -c 16 -d 120 --max-tokens 128`,
      zero errors on either arm, 600+ requests each:

      | arm | streamed | exact | p50 | p99 | TTFT |
      |---|---|---|---|---|---|
      | PP=2 | 255 tok/s | 273 tok/s | 7.32 / 8.02s | 9.24 / 8.45s | 419ms |
      | TP=2 | 349 tok/s | 409 tok/s | 5.54 / 5.07s | 5.88 / 5.57s | 307ms |

      **TP is 1.37x (streamed) to 1.50x (exact) faster, and wins latency too**
      -- at the exact concurrency where the planner refuses it: `TP=2: measured
      all-reduce 5.7 GB/s is below the 40 GB/s threshold; 2 all-reduces per
      layer across 36 layers is 72 cross-node exchanges and 6.6 MB per step,
      which would dominate at concurrency 16`. Measured, it does not dominate.
      Streamed and `--no-stream` agree within each arm, so the figure is not an
      SSE-framing artefact.
      **The docstring's single-stream half is right; its conclusion is not.**
      `planner.py:118-124` records ~40 tok/s TP against 29 PP for this exact
      model at SINGLE STREAM and concludes "above single stream the ordering
      reverses". Single-stream PP measured here at 28 tok/s, so the recorded
      figure reproduces. The reversal does not: at concurrency 16 tensor
      parallel is still 1.4-1.5x ahead. Corroborated on a different
      architecture -- `Qwen/Qwen3-8B` (dense, not MoE) at 8192/16 gave TP 350
      against PP 188-213, the same direction at 1.6-1.9x.
      **So `TP_VIABLE_THRESHOLD = 40.0` (`contracts/constants.py`) is wrong, or
      wrong for this class of model, and `_family_rank` plus the reversal claim
      need re-deriving from these numbers.** That is the follow-up item below.
      Not yet done: putting these figures in the README. They contradict the
      planner, so the README sentence has to be written around that rather than
      quoting a ratio as though the product predicted it.
- [ ] **Re-derive `TP_VIABLE_THRESHOLD`, or replace it.** 40.0 GB/s refuses
      tensor parallel at 5.7 GB/s and the refusal is measurably wrong by
      1.4-1.9x on two different architectures. A single bandwidth threshold
      cannot be the whole model: what the measurement suggests is that the
      pipeline bubble at concurrency 16 costs more than 72 cross-node
      all-reduces do, and nothing in the planner prices the bubble. Until it
      does, the honest fix may be to stop ranking on the threshold and say the
      ordering is unmeasured for the operator's shape.
      **Half done, 2026-09-11: the CLAIM is retracted, the model is not
      fixed.** The refusal no longer ends "which would dominate at concurrency
      N" -- the exchange count and the payload were computed, that clause was
      asserted, and this project's own measurement contradicts it. It now
      states the counts, says the ordering above single stream is unmeasured
      for the shape, and names `parallelism.tensor_parallel` + `node_ids` as
      the way to try the other one. `latency_override`'s "Above single stream
      the ordering reverses" is retracted in the same pass.
      **The threshold was never the blocker, and better inputs do not fix
      it.** Two findings behind that. `_Scored.sort_key` sorts `family_rank`
      FIRST, so `step_seconds` -- the only term in the planner that has ever
      seen concurrency, payload or a bubble -- is a third tiebreaker consulted
      only WITHIN a family and never between TP and PP; moving the threshold
      only flips a boolean. And the link was re-measured on the new
      `TorchNcclMeasurer` rung (below), which cut TP's modelled cost from 9.04
      ms to 6.27 ms per step at c=16 and moved the crossover from c~2 to
      c~4-8 -- and PP still wins the arithmetic at c=16 by 1.18x while the
      stopwatch says TP by 1.37-1.50x. The cost model is wrong, not its
      inputs. Most likely it prices TP's all-reduces as fully serialised with
      compute where vLLM overlaps them.
      `TP_VIABLE_THRESHOLD` is deliberately untouched: frozen, pinned at
      `test_contracts.py:78`, and not the bug.
- [x] **The link the planner reasons about was an estimate, and a stale one.**
      `POST /api/links/measure` on 2026-09-11 replaced a 2026-09-10
      `ib_write_bw` record -- 5.687 GB/s, `estimated: true`, `latency_us:
      None`, so `comm.link_seconds` charged
      `UNMEASURED_COLLECTIVE_LATENCY_US = 40.0` per exchange -- with a real
      two-rank `torch.distributed` all-reduce run inside the serving image:
      **19.793 GB/s and 13.17 us**, `estimated: false`, in 4.25 seconds. 72
      exchanges went from 2.88 ms of assumed latency to 0.95 ms of measured.
      The rung (`links/measure.py::TorchNcclMeasurer`) already existed; the
      coordinator predated it, so nothing had called it. There is deliberately
      no timer and no on-join probe, which is why a measurement this cheap sat
      unused twice now.
      It also settles a question that had been read as a gap: *"GPUDirect RDMA
      is off, so every byte stages through host memory -- on GB10 that is not
      a misconfiguration, it is what unified memory means."* GDR-off is
      correct here, not a fix waiting to happen.
- [ ] **A launch can initialize fully and never bind its port.** Both workers
      loaded, graph capture finished, uvicorn logged `Application startup
      complete`, and nothing was listening -- on the host or inside the
      container -- for 13 minutes with every process alive. Retrying does not
      help by itself: sparkrun reuses the cluster id, the stale
      `sparkrun_<id>_node_0`/`_node_1` pair survives `DELETE
      /api/deployments/<id>`, and the next attempt collides with it and fails
      `Head node failed to become ready`. Clearing it needs `docker stop` on
      both nodes. `deploy/reap.py` will not do it: a container whose backend
      answers as itself is an independent veto, which is right when another
      coordinator owns it and wrong when the owner is gone.
- [ ] **sparkrun cannot reach the fabric for transfers.** `Control machine
      cannot reach any IB IP for host 192.168.0.172 (tried 10.100.0.2,
      169.254.212.153)`, then `Falling back to management network for
      transfers`. The `ib_write_bw` probe reaches those same RoCE devices and
      measures 13.6 GB/s over them, so the measurement path and the launch
      transfer path disagree about whether the interconnect is usable, and
      every staged byte pays for the disagreement.
- [ ] **`loadtest.py` against a long-context deployment kills the engine.**
      The symptom is real: `TimeoutError: RPC call to sample_tokens timed out`
      -> `EngineDeadError`, and the backend is gone. With `--max-tokens 128`
      the same launch served 600+ requests with zero errors.
      **The mechanism named here was wrong, corrected 2026-09-11.** This
      entry blamed `max_tokens_for` for clamping *to* 90% of context. That
      function cannot do it -- `return max(16, min(args.max_tokens, room))` is
      a CEILING, not a target, so at 131072 a default run asks for 64 tokens.
      `git log -S` shows the body has never differed and
      `tests/unit/test_loadtest.py:175,184` pins the reduce-only behaviour.
      Anyone "fixing" that function would change nothing.
      What actually does it is `--hammer`'s other defaults:
      `HAMMER_CTX_FRACTION = 0.5` gives an 8192-token prompt per request
      (`HAMMER_PROMPT_MAX`), 2048-token completions, and an open-loop ramp
      doubling from 8/s against `--max-inflight 20000` with no back-pressure.
      That is hundreds of 8k prefills in flight at a cross-node engine. The
      run that survived was closed-loop `-c 16`, which caps in-flight at 16 by
      construction -- that, not the completion length, is why.

- [x] **Every deployment on this cluster served one sequence at a time.**
      `gateway/internal_api.py` read `int(payload.get("concurrency") or 1)`
      three lines under a comment explaining why that is the wrong shape of
      answer -- *"Absent is not 8192. Absent means 'choose one', and the choice
      is made below, once the plan and the machines it lands on are known -- a
      context picked before the placement is a number, not a fit."* Context
      obeyed it; concurrency did not. Measured on this box 2026-09-11: all
      five live deployments at `max_concurrent_seqs=1`.
      Decode is bandwidth bound -- the weights are read once per step whatever
      the batch holds -- so at one sequence that read produces one token. The
      only batched measurement this project has ever taken (the TP/PP run at
      concurrency 16) reached 349-409 tok/s aggregate on a 120B model.
      `fit/capacity.py::concurrency_for` now derives it, beside `context_for`
      and in its shape, over a new public `FitCalculator.max_seqs` seam onto
      the `_largest_max_seqs` binary search that already existed and was
      reachable only from inside a refusal. Three things worth knowing:
      it is derived at `MIN_USEFUL_CONTEXT` rather than at the real context,
      because the two cannot be solved from each other -- `context_for`
      already takes concurrency as an input, and a fixed-point search between
      two budgets would not be explicable; it is derived AFTER the plan, not
      before, because asking the planner for a node count at a concurrency
      nobody has agreed to yet would demand machines to serve a batch that was
      never requested and could refuse a launch that fits; and
      `MAX_DERIVED_CONCURRENCY = 16` is a CEILING on a derivation rather than
      a default, cited to the only batched concurrency measured on this
      hardware. `target: latency` derives at most
      `LATENCY_CONCURRENCY_CEILING` (2), which is the first time that constant
      has meant anything outside the planner's own boolean.
      Verified end to end: a launch with no `concurrency` in the body comes up
      at `max_num_seqs: 16` in the rendered recipe, against 1 before.
      `ui/src/state/README.md` has always claimed absence means the
      coordinator picks from the fit arithmetic. It does now.

- [ ] **The NCCL sweep corpus is keyed under an address no plan names, and
      un-stranding it would pick the worse setting.** `tests/nccl_sweep.py`
      wrote `dst = a.dst_node or a.peer`, three lines under a comment saying
      *"Node IDS, not addresses... storing `192.168.0.172` makes a record the
      planner can never match"*. So an 80-row sweep (20 environments, 5 sizes,
      zero errors) sits under the address while `measurements.matching_nccl`
      keys on sorted node ids. Fixed at source 2026-09-11: `_node_id_for`
      reverse-resolves the peer (`192.168.0.172` -> `spark-26af.localdomain`
      -> `spark-26af`), returns a name unchanged, and warns rather than
      guessing when the lookup fails.
      **The corpus was deliberately NOT re-keyed, and the reason is the
      finding.** The stranded row this was chased for --
      `NCCL_IB_QPS_PER_CONNECTION=1` at 12.22 us against 15.40 default -- is a
      win at **8 bytes** and a **27% REGRESSION at 4096** (20.53 vs 16.21),
      which is the size band the real decode all-reduce lands in. The
      selection rule already rejects it correctly. Worse:
      `tuning_env` reads `NCCL_MAX_NCHANNELS=2` from the 6 hostname-keyed
      calibration rows and `=4` from the 80 address-keyed ones, because their
      4 MiB figures (18.15 vs 18.55 GB/s) are within run-to-run noise of each
      other -- and `=4` carries a measured 24% decode regression. Making the
      big corpus readable would flip the live pick to the worse option on
      noise.
      So the open item is not the key: it is that **the rule breaks a tie on
      bulk bandwidth when the two candidates are within noise, and does not
      prefer the decode-safe one.** `NCCL_MAX_NCHANNELS=2` is live today and
      is the right answer; it is the right answer by luck of which corpus was
      readable.
      Also unresolved: `python3 -m tests.nccl_sweep --peer ... --sweep` timed
      out on its baseline (300s) on 2026-09-11, on a box where
      `POST /api/links/measure` measured the same fabric through
      `links/collective.py` in 4.25 seconds. Two paths to one collective, one
      of which works.

- [ ] **Measure speculative heads in parallel, one per node.**
      `tests/spec_sweep.py --measure-top` launches its shortlist one after
      another, so four heads is four sequential launches of a model that takes
      minutes to load. Nothing in sparkrun forces that: `generate_cluster_id`
      hashes runtime + model + hosts + port + served name + non-default tp/pp,
      explicitly "so that two instances of the same model on different ports get
      distinct IDs", and derate already allocates a distinct port and served name
      per deployment. The real constraint is derate's own and it is correct --
      `manager._find_conflict` refuses the same model twice ON ONE NODE, because
      the second copy shares that node's unified memory and the fit gate budgeted
      for one. So the parallelism is across NODES: one head per GPU node, each
      under its own served name (`qwen3-4b+eagle3`), which satisfies the
      cluster-wide name rule and keeps the cluster floor's captions unambiguous.
      `connor-pi` does not count -- it is CPU, and `flags.placement_refusal` will
      not let it carry a vLLM rank.
      The check that matters is that parallelism does not perturb the
      measurement. `SpecRecord` is already keyed by workload, GPU, bandwidth and
      image version, so two nodes of the same GPU and bandwidth must reproduce
      each other, and a mismatch on any key must MISS rather than approximate.
      Compare against the serial numbers already in
      `data_dir()/measurements/spec/` before trusting a parallel run.

## Onboarding and reach

- [ ] Double-click installer for Mac and Windows (needs signing/notarization;
      blocked on native support below)
- [ ] Run natively on macOS and Windows (Linux-only today; `derate` is taken
      on PyPI, needs a distribution name/channel)
- [ ] Inbound authentication on the gateway — currently none; biggest gap,
      blocks guest sharing below
- [ ] Shareable guest access via QR code (person-scoped, revocable token
      variant of the existing node-enrollment model)
- [ ] Auto-discovery on the local network / `derate.local` hostname (mDNS
      advertises nodes only today; needs `_http._tcp` too)
- [ ] Household framing in the product copy (gated on the four items above)

## Model surface

- [x] Curated "latest" model strip, larger and refreshable. Eight now, and
      the cap was inert: `MAX_CATALOG_MODELS` has always allowed eight and the
      list held four. The four added are the shapes the strip could not show
      -- a dense model that fits on one Spark (`Qwen3-8B`), a vision tower
      (`Qwen2.5-VL-7B-Instruct`), an embedding model (`Qwen3-Embedding-0.6B`)
      and a 512-expert MoE (`Qwen3-Next-80B-A3B-Instruct`) -- because four
      research shapes that mostly do not fit taught nothing about this
      hardware except that it refuses things. Every `detail` string is
      measured off `resolve_full`, not copied from a model card.
      "Refreshable" is read as *a stale entry fails something* rather than
      *operator-editable at runtime*: the list stays code-owned, because a
      curated list derived from hub trending is not curated.
      `tests/unit/test_catalog.py` holds it to its cap and its shape
      hermetically, and `python3 -m tests.model_sweep --curated` resolves
      every id for real. That immediately found one: **`meta-llama/
      Llama-3.3-70B-Instruct` has been on this list since the beginning and
      401s on this box**, rendering its gated-repo error verbatim as a dead
      row. Gated is reported apart and does NOT fail the check -- it is a
      licence this box has not accepted, not a broken entry, and counting it
      would make the result depend on whose token is in the environment.
- [x] Never show a model that can't run — offer the quant that does. Two
      offers existed and the wrong one was on the list screen.
      `capacity._walk` steps down `QUANT_SUGGESTION_ORDER` and answers which
      scheme WOULD hold; it never asks whether anybody published the model at
      that scheme, so "requantized to q2_k" could name a build that does not
      exist. That string now reads `would fit at q2_k` -- what it is, an
      arithmetic result. The real offer is the variant ladder on the model's
      own page, where every row is a repository that was SIZED (the item
      below), and a refused row in `CatalogList` now carries a link to it.
      Deliberately a link and not the ladder itself: enumerating it costs a
      hub search plus a resolve per candidate, which is the denial-of-service
      `MAX_CATALOG_MODELS` exists to prevent. It is announced rather than run.
- [x] **A variant whose shards were never measured still gets a confident
      verdict, and the estimate can be 2.4x wrong.** Measured on
      `Qwen/Qwen3.8-Flash-Next` on 2026-09-11: `GET /api/models/variants`
      returned 58 rows, 52 of them carrying a measured `file_bytes` and **six
      carrying `None`** — and the six are exactly the vLLM-launchable ones
      (`nvfp4` x4, `awq_int4`, `fp8`; every GGUF row got measured). Each of the
      six still came back `static_verdict: fits_degraded`, `launchable: true`,
      with `nvidia/Qwen3.8-Flash-Next-NVFP4` claiming **57.9 GiB to spare** at
      an assumed 4.5 bits/weight and `shard_count: 1`, `shard_files: []`.
      It was then downloaded -- 132.7 GB, 11 shards, genuinely 4-bit NVFP4
      (`num_bits: 4`, group_size 16) -- and the fit gate measured it and
      refused: `weights alone are 111.8 GiB per rank (including 0.8 GiB of
      replicated vision weights) against 107.7 GiB usable`. Predicted 47.3
      GiB/rank against a measured 111.8: **2.4x, in the direction that turns a
      refusal into an invitation.** 62 GiB of claimed headroom that was really
      4.1 GiB of overflow, on an idle machine.
      The rule this needs already exists one subsystem over. `speculators.py`
      refuses a head whose shards cannot be counted rather than estimating it,
      because "the analytic split for a head is a floor and lands ~16% low,
      which is the wrong direction for a gate" -- and 16% is the case that was
      judged intolerable. `resolver/params.py:232` says the same thing for
      parameters: "The measured total is authoritative for capacity." The
      variants path (`gateway/capacity_api.py`) is where the principle is not
      applied: `file_bytes` is documented there as "Measured or absent. Never
      an estimate dressed as a size" -- which is honest about the *size* field
      and says nothing about the *verdict* computed when that field is absent.
      A row with no measured shards should answer "not sized" rather than
      `fits_degraded`, the same way a head does.
      **Done, and the missing half was that the absence was never necessary.**
      The six were `source: repo_name` rows -- built from a hub SEARCH HIT,
      which carries no file list, so `quant_variants` never passed
      `file_bytes` at all. The hub client already fetches with `blobs=true`
      and `ModelInfo.shard_bytes()` already sums them: sizing all six measured
      0.32s, one bounded `model_info` each, run concurrently under a
      `_MAX_SIZE_PROBES` budget beside the existing header-probe one. So the
      fix is measure first, then refuse what is left -- refusing alone would
      have left the item above with nothing to offer, and measuring alone
      would repeat this the first time the hub 429s.
      Verified end to end: `nvidia/Qwen3.8-Flash-Next-NVFP4` now reports
      123.6 GiB across 11 shards and `wont_fit`, against `fits_degraded` with
      54.7 GiB to spare before. **Two of the six genuinely DO fit** (62.6 GiB
      and 14.9 GiB), which is the case a blanket refusal would have hidden.
      Anything still unsized -- gated, 429, no index -- comes back
      `verdict: null` with `_unsized_reason` naming the repository, modelled
      on `speculators.py`'s own refusal. `contracts/plan.py::Verdict` was NOT
      touched: it stays three values, the row carries the absence, and
      `fitLamp` says "cannot be sized" rather than "not checked", which reads
      as a spinner that has not landed.
- [x] **Weights are not always split across ranks in the fit accounting.**
      Seen on two models the same day. `gpt-oss-120b` splits correctly --
      30.4 GiB per rank from a ~61 GiB checkpoint. But
      `nvidia/Qwen3.8-Flash-Next-NVFP4` reports 111.8 GiB *per rank* against
      123.6 GiB of safetensors on disk, and
      `deepseek-ai/DeepSeek-V4-Flash-0731` reports 141.2 GiB per rank against a
      144.0 GiB total with "over budget by 36.3 GiB in total" -- both read as
      the whole checkpoint being charged to one rank. The two that misbehave
      are the ones with very large expert counts (512 and 256) and, for the
      Qwen, a vision tower; `gpt-oss-120b` has 128. Whether that is correct
      (expert weights genuinely not shardable in the shape evaluated) or an
      accounting bug, the refusal string should say which, because "it needs 3
      nodes" and "it needs 2 nodes and we counted twice" are different
      instructions to the operator.
      **It was both, in different places.** The two models named above are
      accounted CORRECTLY: `valid_tp_degrees` requires a degree to divide both
      head counts, and `DeepSeek-V4-Flash-0731` publishes **one KV head**
      against 64 attention heads, so no tensor-parallel degree above 1 is
      legal at any node count; the Qwen was sized on one ticked node, where
      there is nothing to split. Neither said so on screen. `_split_note` now
      names the split the figure was divided by and, when it is 1, why --
      "charged whole: this checkpoint publishes 1 KV head ... which admits no
      tensor-parallel degree above 1", "data parallel 2 replicates the model,
      so every rank holds a full copy", or "charged at TP 2 x PP 1".
      **The real bug was next door: `expert_parallel` divided nothing.**
      `weight_bytes_per_rank` divided by TP and the pipeline stage fraction
      and by nothing else, so DeepSeek-V4-Flash measured 276.0 GiB per rank at
      `ep=8, dp=8` across eight nodes and 276.0 GiB at `ep=1` on one --
      byte-identical -- while `recipes.py` passed `--enable-expert-parallel`
      on exactly those plans and the engine really did place `num_experts/ep`
      per rank. `resolver/params.py` already computed
      `ParamBreakdown.routed_experts` and `_shape_from` threw it away;
      `ModelShape.routed_expert_params` carries it now (trailing and
      defaulted, exactly as `vision_params` is) and the routed share is
      divided by `max(tp, ep)` -- conservative, because the planner only ever
      emits `tp>1, ep=1` or `tp=1, ep=world`. The same checkpoint at ep=8 is
      now 50.3 GiB per rank. 0 means NOT DERIVED and charges the experts
      whole, which is the refusing direction. `cache.py::SCHEMA_VERSION` went
      6 -> 7 for it, because a cached entry decoding the field as 0 is the
      same class of silent wrongness that bump has caught three times before.
      Two smaller honesty fixes alongside: the overage clause said "over
      budget by X in total" inside a sentence whose subject was weights alone
      (it is the whole six-term overage, and now says so), and the rank
      shortage sentence named TP x PP x DP and omitted EP entirely.
      Still true and deliberately unchanged: `min_nodes_required` searches
      TP/PP shards only (`_candidate_shards` hardcodes `ep=1`), so "it needs 8
      nodes" is a floor an expert-parallel plan may beat. That is the safe
      direction for a gate, and it is the next thing to look at here.
- [x] Simple on/off switch per model everywhere. `Deployment.serving` is the
      deployment half of the provider allowlist, and the seam is the same
      line: `targets.py::build_index` skips a non-serving deployment exactly
      where it skips a disabled provider, so `/v1/models`, routing, the chat
      picker and the topology graph change together with no restart and
      nothing else to press. `PATCH /api/deployments/{id}` takes
      `{"serving": bool}` and rebuilds, mirroring `PATCH /api/providers/{id}`.
      **The container keeps running and keeps holding its GPU memory**, which
      is the whole difference from DELETE -- so the inspector says so, in
      GiB, beside the switch. A switch that hid an idle 30 GiB would be worse
      than no switch on this hardware.
      Durable rather than an admission block: `admission.py`'s three blocks
      are in-memory and `reconcile()` re-derives them twice a second, so a
      switched-off deployment would have come back on the next restart or
      sooner. `store.py::SCHEMA_VERSION` 2 -> 3; an absent field decodes True,
      because defaulting it False would silently take every pre-upgrade
      deployment off the API.
      One thing found on the way: `router.parkable()` declines to park a
      request to a target carrying an admission block, but a switched-off
      deployment is not in `index.targets` at all -- it is in `pending` -- so
      that check iterated an empty list and a deliberate switch-off read as a
      ten second stall before the 503 it was always going to be. It checks
      `pending` now.
- [x] Custom quant support behind an advanced flag. The mechanism turned out
      to be half-built and half-refused: `/api/plan` has always taken a
      `dtype` override that re-prices the weights, and `POST /api/deployments`
      refused the field outright -- correctly, because neither serve command
      carried `--quantization`, so honouring one would have budgeted 4-bit
      weights and started 16-bit ones.
      Both templates carry it now, and the blanket refusal is replaced by the
      rule `kv_cache_dtype_refusal` already follows: **refuse, never drop**.
      This one matters more than that one -- `BYTES_PER_PARAM` differs by 3.5x
      between bf16 and nvfp4, against 2x for a cache width -- so a runtime
      that cannot be told (tts) refuses the launch rather than quietly loading
      the checkpoint's own packing into a budget sized for something else.
      **Not all 33 formats, and the list was read off the image rather than
      documentation.** `VLLM_QUANTIZATION_METHODS` is the pinned image's own
      `QUANTIZATION_METHODS` (30 names), and `_QUANT_TO_VLLM` maps the eight
      derate keys that have a loader there -- `nvfp4` is `modelopt_fp4`, not
      `nvfp4`. The GGUF ladder (21 of the 33) is llama.cpp's format and
      `resolver.py` already marks every GGUF variant unlaunchable; **`nf4` is
      refused because `bitsandbytes` is absent from that registry**, read
      rather than assumed. Those refuse by name and say to pass the
      quantization's own repository id instead, which is still right for them.
      Verified on a real launch: the rendered recipe carries
      `quantization: modelopt_fp4` and `--quantization {quantization}`, and
      the record persists the scheme it was gated at. The Serve panel's
      control sits beside the KV cache width for the same reason that one does
      -- the panel re-plans when it moves, so every figure in the card is
      already the figure for the chosen scheme.
- [x] One-click speculative decode. `resolver/speculators.py` reads what the
      checkpoint declares (`num_nextn_predict_layers` -> MTP, `dspark_*` ->
      DSpark) and offers ngram for everything else, since it loads no weights;
      the fit gate charges the draft's weights and its drafted positions;
      `?spec=method:k` carries it and `--speculative-config` launches it on
      vLLM. Two things deliberately NOT done: DSpark is detected and refused
      rather than priced, because nothing here has derived what that module
      weighs, and no throughput figure is claimed — the card states a floor and
      a ceiling and says the acceptance rate between them is not measured.
      Externally-published heads are supported too: name an EAGLE3/DSpark/MTP
      head repo and `speculators.py::head_option` resolves it, checks its class
      against the image's own 61-class speculator registry, gates it on
      hidden_size+vocab_size and charges its MEASURED weights. A head whose
      shards cannot be counted is refused rather than estimated.
      Still absent: pairing a big model with an ordinary small model as the
      draft (`method: draft_model`), and any auto-suggestion of heads --
      derate prices what you name and never proposes third-party weights.
- [x] Measure acceptance instead of stating a range. `tests/spec_sweep.py`
      launches, drives three prompt sets, and reads vLLM's own
      `vllm:spec_decode_*` counters; because the image exports acceptance per
      draft POSITION, one launch at k=10 answers for every k below it. Records
      land in `data_dir()/measurements/spec/` keyed by workload, GPU and image
      version, and the card cites a match beside the range rather than instead
      of it. What is NOT done: nothing feeds `strength.py`, deliberately -- a
      figure from a synthetic workload must not outrank the real proxied
      traffic that routing already measures.
      Since 2026-09-10 the LIVE rate is on the frame too, per deployment:
      `MetricsHub` reads `vllm:spec_decode_*` off each ready vLLM on its own
      10s clock and differences it, so `MetricsDeploymentFrame.speculative`
      carries acceptance, acceptance per draft POSITION and tokens settled per
      step for the workload that actually ran -- drawn by
      `DeploymentInspector`. Same two rules as the stored records: it sits
      beside the floor/ceiling range and never replaces it, and `null` means
      not measured rather than zero, because most deployments run no draft
      head and a model that drafted nothing has no acceptance rate. Still
      nothing feeds `strength.py`.
      Proved on a real launch rather than a fixture: Qwen3-0.6B served with
      `--speculative-config {"method": "ngram", "num_speculative_tokens": 10}`
      reported 73.3% of drafted tokens kept and 7.33 settling per step, with
      the cumulative curve 0.89 -> 0.78 -> 0.67 across ten positions, read
      straight off `/api/metrics/stream`. `tests/fixtures/
      vllm_ngram_spec_real.txt` is that engine's own body, and it is the
      fixture the hub test asserts against -- the tidy hand-composed one is
      kept beside it for the arithmetic cases only.
      The same model and method measured 0.203 on `spec_sweep --smoke`'s
      code_edit set minutes earlier: same GPU, same image, same k, three and a
      half times the acceptance, because the workload differed. That pair is a
      test, and it is why a record misses rather than approximates.
- [x] Say what the throughput figures are about. Every tok/s the fit gate
      reports is per sequence -- `kv_read` is computed at one sequence
      deliberately -- so the concurrency control moved the memory bars and left
      the rates alone, and a plan sized for 16 still advertised a 6x
      speculative ceiling. Both readouts now say `per sequence`, and above one
      sequence `_speculative_range` adds that speculation pays most where the
      batch is small enough for decode to be bandwidth-bound. What is NOT done,
      and is the next real step: nothing measures where that stops. An
      aggregate rate would need a compute model this project does not have, so
      the sentence names the boundary rather than extrapolating past it.
- [x] Refuse a vision head for a text model. Geometry cannot see it --
      `AngelSlim/Qwen3-VL-30B-A3B-Instruct_eagle3` and a text EAGLE3 head report
      the same hidden size, vocabulary and vision_params against the same
      target. The head's own config declares `target_model_type`, so
      `speculators.target_type_conflict` refuses on a declared MISMATCH and
      never on absence. Verified against the live hub: usable drops by exactly
      one and the refusal names both types.
- [x] Recommend a head, and remember the answer. `GET
      /api/models/speculative-heads` ranks the checkpoint's OWN options
      (`mtp`, `dspark`, ngram) beside every published head in one list, and
      `head_scan.recommend` picks one: weightless drafts dropped, a measurement
      beating any arithmetic, then ceiling across families and downloads within
      one. The scan is written to `data_dir()/scans/heads` keyed by model AND
      image version, so a model is scanned once rather than once per view --
      which is what lets the UI offer a checkbox instead of a button. The
      checkbox arrives UNCHECKED with the head, its method, its size and a
      speedup multiple beside it; `n` is an input bounded by what the source
      says it can draft. What is NOT done: the checkbox never auto-enables,
      because enabling costs memory the fit gate then charges and a model that
      fits can stop fitting. And `recommend` cannot tell two heads of one
      family apart on anything but downloads -- `SpecRecord` is keyed by
      method, so even a measurement only picks the mechanism.
- [x] Auto-scan the hub for draft heads. `--scan` searches, prices every
      candidate through `speculators.head_option` and ranks them by ceiling,
      launching nothing — 84 candidates and 41 usable for Qwen3-4B in seconds,
      on a box with no free memory. `--measure-top N` shortlists one head per
      METHOD FAMILY (by downloads) and hands it to the sweep, because within a
      family the ceiling ties. Still absent: the geometry gate cannot tell a
      vision head from a text one when the dims match, so a shortlist is a
      shortlist rather than a verdict.

- [ ] **Choose the container image per deployment, in the Serve panel's
      Advanced disclosure.** The image is a process-wide env var today:
      `recipes.container_image` returns `env.get(spec.default_image_env) or
      spec.default_image` and nothing else can reach it, so changing it means
      editing the coordinator's environment, restarting, and accepting that every
      deployment of that runtime moves with it. The failure that prevents is
      already recorded here: the tts image is a locally-faked tag held only by
      `DERATE_TTS_IMAGE`, that variable was lost to a restart at 22:58 on
      2026-09-07, and the next launch went straight back to `manifest unknown`.
      An image on the deployment record cannot be lost that way.
      Half-plumbed already -- `recipes.synthesize` takes `image=` and validates
      it, and nothing passes one. What is missing is the thread from
      `POST /api/deployments` through `launch` and `recipe_for`, an
      `image: str | None = None` field on `Deployment` following `extra_args` and
      `custom_command` exactly (keyword-only, defaulted, so every older record
      still decodes), and a grammar of its own: `_COMMAND_SAFE` has no `@` and so
      refuses `name@sha256:...`, which is the one form worth pinning.
      The part that is easy to get wrong is not the plumbing. Four stored facts
      are keyed by image -- architecture support (`VLLM_ARCHITECTURES`), the
      speculator registry behind `head_scan`, the spec measurements, and the NCCL
      tuning records -- plus `VLLM_KV_CACHE_DTYPES`, which was read off the pinned
      image's own `CacheConfig`. Every one must MISS rather than approximate when
      the operator picks a different image. A number that survives an image change
      silently is the `--kv-cache-dtype` bug again: the gate and the engine
      agreeing with each other and disagreeing with what is running.

## Cluster

- [ ] **Expert parallel is BETA, and should say so on screen before anyone
      relies on it.** The EP field, the planner prose and the launch gate all
      landed together, and what has actually been exercised is one model on one
      pair of machines: `openai/gpt-oss-20b` at EP=2/DP=2 across `spark-4d38`
      and `spark-26af`, launched through derate's own route, served through its
      own `/v1`, on a sparkrun installed into a scratch venv. That is a
      demonstration, not coverage. What is NOT known: whether any other MoE
      checkpoint launches this way, what EP actually costs against TP or PP at
      any concurrency (the planner's own numbers for it are arithmetic and
      nothing has measured them), whether the shape survives a restart or a
      node leaving, and whether the single-node `tp=ep, dp=1` form works at all
      -- it is unreachable on a 1-GPU-per-node Spark, so the code path that
      `_score` now emits has never run anywhere.
      Mark it beta where the operator meets it: a caption on `DegreeFields`'s
      EP box, and a line in the Verdict card's EP wording. Do not mark it beta
      in the refusal strings -- those are already specific about what they
      know.
      Two things gate promoting it out of beta, and neither is code here:
      sparkrun >= 0.3.7 has to be the installed launcher (`uv tool upgrade
      sparkrun`; the version gate in `data_parallel_refusal` lifts itself), and
      somebody has to run `tests/load/loadtest.py` on an EP arm against the
      TP/PP arms the Evidence section above already has numbers for. Until that
      table exists, EP is a shape derate can plan, price and launch, and cannot
      recommend.

- [ ] Split coordinator role from worker role explicitly (decision logic
      exists; needs its own deployment shape/image)
- [ ] Handle coordinator-down honestly — needs an architecture decision
      (standby + roster handoff, or client-side cloud fallback) before any
      implementation
- [ ] Full downloads tab, server-side transfer records that survive a refresh
- [ ] Kill a worker mid-stream and have the stream continue — pick the
      guarantee (replay vs. clean fail) before building it
- [x] Manual placement. The planner does have a placement field, and a degree
      field beside it: `node_ids` and `parallelism` on both `POST /api/plan` and
      `POST /api/deployments` (`internal_api.py`'s `_PLACEMENT_PARAM` and
      `_DEGREES_PARAM`), reaching `Planner.plan_for()` instead of the ranked
      recommendation, and wired through the UI in `tabs/models/DegreeFields.tsx`.
      `node_ids` order is preserved deliberately -- the first node is the
      pipeline head sparkrun SSHes to first -- and an omitted axis means 1,
      never "whatever the planner would have picked". The TP-vs-PP measurement
      under **Evidence** was taken with exactly these two fields, which is what
      proves they work end to end. Illegal degrees come back 400
      `illegal_parallelism` naming the legal ones, and a placement that does not
      fill the named nodes comes back 400 `placement_underfilled`.
- [ ] Link utilisation surfaced in the UI (no bytes-on-the-wire telemetry
      exists)
- [ ] Managed remote node tier (today: a target is local or a provider, no
      third kind)
- [ ] Add a node by address (today: join is worker-to-coordinator and
      token-gated)
- [x] Prefix cache hit rate. The backend reported null because nothing read the
      counters, not because none exist: the pinned image exports
      `vllm:prefix_cache_{queries,hits}_total`, confirmed by scraping a live
      engine. `metrics_scrape.prefix_cache` reads them and `MetricsHub` scrapes
      every ready vLLM on its own 10s clock -- never inside `snapshot()`, which
      is synchronous at 1 Hz and would stall the whole stream on one wedged
      backend. The figure is a WINDOWED rate, not the engine's lifetime ratio,
      and `null` survives everywhere nothing was measured: no ready vLLM, metrics
      off, the first round after startup, or a window in which nothing was asked
      of any cache. An idle cluster has no hit rate; only one that asked and
      missed has a real 0.0. Two traps in the names, both pinned by tests:
      `_created` is a unix timestamp, not a count, and `external_prefix_cache_*`
      is the KV offload tier, a different store.
- [ ] Scheduled model swaps (time-based placement)
- [ ] Auto-eviction policy (currently always manual)
- [x] Alerting — node down, cap reached, OOM. `control_plane/alerts.py` folds
      the three into standing conditions and `GET /api/alerts` answers what is
      wrong RIGHT NOW; the rail draws them above the roster, because "something
      is wrong" outranks "here are the machines".
      Almost none of this was detection: `node_lost`/`node_recovered` and
      `fit_miss` were already edge-triggered and already durable. What was
      missing was the fold from moments into conditions, and a place to read
      it. `/api/history/events` is unchanged and stays the audit trail -- it
      cannot answer "now", because it is windowed, it truncates (losing an
      open while keeping its close folds to "nothing is wrong"), and it is off
      entirely on a box with no archive.
      Only ONE trigger was genuinely absent: a cap crossing. `over_budget()`
      was only ever ASKED, at admission, so nothing knew when it happened.
      `_check_budget` emits on the edge in both directions -- spend rising past
      a fixed cap, and an operator lowering the cap below today's spend.
      Consumes registry's `node_lost`, never deploy's `NODE_UNHEALTHY`:
      telemetry/events.py already documents that they are different facts, and
      the latter fires once per DEPLOYMENT on the node -- three alerts for one
      dead machine, and none at all for an idle one.
      Nothing fires twice, and `since` comes from the event rather than from
      when the book started: `node_lost` carries `last_seen_age_s`, so a
      restart that rediscovers a three-hour outage still reports it as three
      hours old. A crash loop is one alert with a count, which is why OOM is
      keyed on the served name -- every retry is a new deployment id, and this
      box was producing one every five seconds.
      Fixed on the way past: the budget sentence existed TWICE and the two
      disagreed -- `admission_block` said "($12.31 spent today)" and the
      refusal in `_admit` dropped that half. `ProviderRuntime.budget_block` is
      the one author now, and the alert carries it verbatim. Same for the fit
      gate's OOM sentence, which existed only as lazy %-args inside a
      `logger.error` and is now `alerts.fit_miss_sentence`.
      NOT done, deliberately: no notification delivery (email/webhook/push) --
      that is a second system with its own auth and retry; no acknowledge or
      snooze, which would need durable per-alert state; and memory pressure
      stays a LEVEL on `/api/memory` rather than becoming a fourth alert,
      since `memory_severity` already surfaces it and `admission.py` already
      acts on it.

- [ ] **Allocate ports against the node that will hold them, not the
      coordinator.** `DeploymentManager._allocate_port` probes `_port_free`
      locally, and `manager.py:267` already says why that is wrong: `port_is_free`
      is local by design, so the coordinator cannot see a port already held on the
      node it is placing on. Measured cost: 1,094 `launching -> failed`
      transitions in two days, every gap exactly `RESTART_BACKOFF_S[0]`, because
      the underlying launch was never retryable -- `Errno 98` on a fixed port --
      and nothing in the restart path could see it.
      Ask the node agent instead: either a narrow route beside
      `/agent/processes`, or listening ports carried on the telemetry payload that
      already crosses. Keep the current semantics rather than tightening them --
      this is "an improvement on a guess, not a gate", and the identity probe is
      still what actually stops a deployment adopting a stranger.
- [ ] **Make a launch's container observable, under any launcher.** Three pieces,
      all of which outlive whatever starts the container.
      A `docker inspect` fixture per runtime and topology under
      `tests/fixtures/launch/`, so "which flags does a real launch actually get"
      is recorded rather than remembered. The one captured on 2026-09-11 already
      corrects two claims in CLAUDE.md: `PidMode` is EMPTY -- there is no
      `--pid=host`, `CAP_SYS_PTRACE` is what gets added -- and 0.3.8 mounts a
      second volume at `/cache/runtime`.
      The container's exit code and PID on every transition. sparkrun hands back
      only a cluster id, which is why `procmatch.py` exists at all and why
      `_post_mortem` reconstructs a cause of death out of log strings. An exit
      code is evidence; a reconstruction is a guess.
      And an assertion that NCCL actually bound IB: require
      `NET/IB : Using [0]rocep1s0f0`, flag `NET/IB : No device found`. Nothing
      checks this today and the failure is silent and expensive -- 243 us against
      17 us for an 8 KB all-reduce, 0.12 GB/s against 18 at 4 MiB, which is the
      entire interconnect the planner plans against.

## Launcher

Replacing sparkrun with a derate-native launcher. Each stage ends with the
system working and rollback available through `DERATE_LAUNCHER=sparkrun|native`.

- [ ] **Why this is worth doing, in one place.** derate does not integrate with
      sparkrun's CLI. `flags.py` and `recipes.py` are written against a dozen of
      its PRIVATE modules by file and function (`core/recipe.py::_KNOWN_KEYS`,
      `orchestration/ssh.py`, `runtimes/_cluster_ops.py::run_native_cluster`), so
      what is maintained here is a shadow copy kept in step by reading somebody
      else's source. The contract is stdout, and it has already failed once: the
      cluster id gained a second hex run between 0.2.40 and 0.3.8, and a launch
      whose containers were up on both hosts was recorded FAILED with the
      workload still running. And derate cannot ship as one thing while
      `install.sh` mounts `~/.config/sparkrun`, `~/.ssh` and the docker socket and
      the Dockerfile carries openssh-client, git and an 18 MB docker CLI for
      somebody else's program.
      Two things it is NOT, both of which made this look harder than it is.
      sparkrun is not NVIDIA's and not a binary -- it is scitrera.ai's,
      Apache-2.0, pure Python, 44,294 lines, alpha. And the multi-node path is
      not Ray and not MPI: `runtime: vllm` resolves to `vllm-distributed`, which
      is plain vLLM `--nnodes/--node-rank/--master-addr/--master-port` with
      `--headless` on workers. `links/collective.py` is already a working
      two-rank version of exactly that mechanism, proven on this fabric.
- [x] **The engine package exists, and the launcher's table is pinned to it.**
      `control_plane/engines/` -- `spec.py` (`EngineSpec`, was `RuntimeSpec`),
      one module per engine, and a registry whose whole point is that adding an
      engine should be one file plus one line. It is NOT that yet: adding one
      today still means six files before a launch works at all (`flags.py`,
      `progress.py`, `adopt.py`, `resolver/support.py`, `envspec.py`,
      `contracts/deployment.py`) and about fourteen before it behaves.
      `deploy/README.md:284` has claimed "a fourth runtime is one entry in
      `RUNTIMES` and nowhere else" for a while; this is the work to make that
      sentence true rather than delete it.
      Top-level rather than `deploy/engines/`, and that is the load-bearing
      part. `deploy/__init__.py` eagerly imports the manager, the sparkrun
      adapter and the event bus, so one constant costs the whole launcher --
      which is why every consumer outside the package defers the import into a
      function body (six sites in `gateway/internal_api.py` alone, plus
      `capacity_api.py`, `links/measure.py`, `node.py`; nine in total, none at
      module level) and why `gateway/states.py` exists only to restate a
      frozenset. `redaction.py` left `providers/` for the same reason and says
      so in its own docstring. `test_engine_registry.py` enforces the rule in a
      subprocess, because `sys.modules` in the suite is already full of
      everything else pytest imported.
      **The table is duplicated on purpose, for now.** `flags.py` still owns
      `RUNTIMES` and `engines/` holds a copy, because the cut is ~700 lines out
      of a file with 1,197 uncommitted lines in a checkout several sessions are
      editing, and a move that lands mid-edit loses somebody's work. The copy is
      pinned field-for-field (84 parametrized assertions, one per engine per
      field) so it cannot drift while it waits. Next step is `flags.py`
      delegating, which deletes the copy rather than blessing it.
- [x] **Ask the engine which flags it accepts, instead of guessing.**
      `engines/flagcatalog.py` + `FLAG_PROBE` in each engine module. Measured
      against the pinned image on 2026-09-11: derate's template emits **16**
      distinct vLLM flags and the image's own parser declares **400 names
      across 295 options**, so 384 were reachable only as `extra_args` -- which
      reach the runtime through `_EXTRA_ARG_SAFE`, a regex that knows the SHAPE
      of a flag and nothing about whether the engine has heard of it.
      `--max-len 4096` passes that grammar and vLLM exits on it.
      Asked, never copied, for the reason `imageprobe.py` and `NCCL_TUNABLES`
      give: a table claiming a flag the image lacks is a launch that dies at
      argv, and one refusing a flag the image accepts is a 400 on a servable
      configuration. The probe has already earned that twice over. The module
      MOVED -- `vllm.entrypoints.openai.cli_args` is now
      `vllm.entrypoints.launchers.cli_args` -- so the import is a ladder, not a
      path. And reading only the argparse action class name records
      `--enforce-eager` as value-taking, because vLLM spells switches with a
      custom paired action (`--enforce-eager`/`--no-enforce-eager`) that is in
      none of the obvious sets; `nargs == 0` is what settles it, and without it
      87 switches looked like 6 and `--enforce-eager=yes` sailed through.
      Refusals name the flag, suggest a near miss through `difflib` (a
      shared-prefix rule was tried first and suggested `--max-lora-rank` for
      `--max-len`, which is worse than silence), check a value against the
      parser's own `choices`, and refuse NOTHING when the catalogue could not be
      read -- absence is "could not be asked", the mistake
      `SparkrunAdapter.is_running` was rewritten to stop making.
      Confirmed on the way past: `VLLM_KV_CACHE_DTYPES` still matches the
      image's `--kv-cache-dtype` choices exactly, 18 for 18, through a
      different surface than the one it was read off.
      Still to do: probes for sglang, tts and llamacpp (only vllm has one), and
      wiring `flag_refusal` into the launch path beside `check_extra_args_safe`.
- [ ] **`llamacpp` cannot start on its own default image.** The recipe emits a
      bare `llama-server`, `executor_config` clears the image ENTRYPOINT
      (`(("gpus", ""), ("entrypoint", ""))`) and sparkrun runs the rendered
      string under `bash -c` -- but in `ghcr.io/ggml-org/llama.cpp:server` the
      binary is `/app/llama-server`, `WorkingDir` is `/app`, and `/app` is NOT
      on PATH. Reproduced with sparkrun's exact invocation:
      `docker run --rm --entrypoint '' <image> bash -c 'llama-server --version'`
      answers `bash: line 1: llama-server: command not found`, while
      `/app/llama-server --version` prints `0.4.0-dev (build 10902)`.
      sparkrun's llama-cpp plugin renders `command:` verbatim and injects no
      PATH, so nothing downstream rescues it.
      Worth resolving rather than just patching: the flag spellings in
      `_LLAMACPP_COMMAND` were read off a real launch of that exact build, so
      SOMETHING ran -- most likely the other candidate image
      (`scitrera/dgx-spark-llama-cpp`, named in the `default_image` comment),
      which is not on this box. So the fix is probably not "hardcode
      /app/llama-server" but "the binary path is per-IMAGE, not per-engine" --
      which is A6's territory, and is the first concrete case of an engine fact
      that a different image legitimately changes.
- [ ] **Fold `resolver/support.py::RUNTIMES` into the engine modules.** It holds
      architectures, the quant ladder and the known-broken list -- all per-engine
      facts -- and `contracts/derived.py:83` exists SOLELY to stop it drifting
      from `flags.py::SUPPORTED_RUNTIMES`. That row is the strongest argument
      available that the two were always one object. Folding them makes the one
      enforced copy disappear rather than move; `derived.py` then points at
      `engines:SUPPORTED` with empty `copies`.
- [ ] **Retire the four hardcoded engine names.** `manager.py:1651`
      (`denominator = free if runtime == "sglang" else state.memory_total` --
      vLLM reads its fraction against device TOTAL, SGLang's
      `--mem-fraction-static` against FREE, and getting it wrong mis-sizes every
      launch), `adopt.py:163` (divides `ctx-size` by `parallel` in a hardcoded
      llamacpp branch while the write side reads
      `spec.context_is_shared_pool` -- two spellings of one rule),
      `adopt.py:186` (served-name flag by name, while the write side uses
      `spec.served_name_arg`), `gateway/metrics.py:133` (`if dep.runtime !=
      "vllm": continue`, so a new engine exports no telemetry at all). Each
      becomes an `EngineSpec` field: `utilization_denominator`, the existing
      `context_is_shared_pool`, the existing `served_name_arg`, `metric_prefix`.
- [ ] **Declare what an engine can serve.** `RuntimeSpec` has no modality field,
      so the link between the `tts` engine and `/v1/audio/speech` exists only
      because `TTS_ARCHITECTURES` is spliced into `AUDIO_ARCHITECTURES` at
      `resolver/support.py:275` -- an engine's endpoint family is currently an
      accident of an architecture table. A `modalities` field replaces that and
      `autoadopt.py:327`'s `if spec.runtime == "tts"`.
- [ ] **Publish the engine list to the UI.** There is no API that does.
      `ui/src/state/runtime.ts` hardcodes the union AND the picker options AND
      re-derives `memoryPool()` and `shardsAcrossNodes()` from names, mirroring
      `RuntimeSpec.memory_pool` and `.shards` server-side; `runtime.check.mjs`
      holds a third copy. None of the three is checked against the server, and
      the union is deliberately a SUPERSET (it carries `ollama`, which is a
      `ProviderKind`, not a launchable engine), so it can never be compared
      member-for-member until the list is served.
- [ ] Stage 1: `control_plane/launch/`, vLLM solo on the local node. Done when
      the parity fixtures above match and one real launch answers `/v1/models`.
- [ ] Stage 2: container verbs on the node agent (create/start/stop/inspect/
      logs/events), then remote solo. Done when a launch on `spark-26af` runs
      from the coordinator with a PID recorded. This is the stage that removes
      SSH from the launch path.
- [ ] Stage 3: multi-rank vLLM, then sglang, llamacpp, tts. Done when 2-node TP
      and PP both launch and the IB assertion passes.
- [ ] Stage 4: cut the default over, adapter kept behind the flag for a week of
      real launches.
- [ ] Stage 5: delete `deploy/sparkrun.py`, `deploy/recipes.py`, the three
      injection grammars, `_SPARKRUN_MARKERS`, `SPARKRUN_*_VERSION`, the reaper's
      stray-follower machinery, and the installer and Dockerfile mounts and pins.
      Three of those are deleted by construction rather than by hand. argv
      instead of a `bash -c` string retires `_check_yaml_safe`, `_COMMAND_SAFE`
      and `_EXTRA_ARG_SAFE`, which exist only because sparkrun substitutes
      `{model}` into a template and runs the result in a privileged
      host-networked container. The serve command as PID 1 retires "a dead engine
      does not kill its container", measured once as a launch that died in thirty
      seconds and was watched for 1800. And `/tmp/sparkrun_serve.log` going away
      retires the stranded `tail -f` reaper, which was cleaning up between 1,195
      and 3,007 followers per container.

## Housekeeping / smaller items

- [x] Trim CUDA graph capture time -- and then correct the premise once the
      numbers came in. Both levers named in the old entry are launch options
      now: `enforce_eager` (a bare `--enforce-eager`, no render/validate
      surface since there is no value to substitute) and a trimmed
      `cudagraph_capture_sizes` (`--cudagraph-capture-sizes 1 2 4 8`,
      confirmed against the live image's own `--help=CompilationConfig` to be
      a plain space-separated int list, not JSON -- simpler than
      `--speculative-config`'s grammar, and safe by construction once every
      element has round-tripped through `int()`). Neither reaches the fit
      gate: `FitResult.breakdown.framework_overhead` is a flat 1 GiB constant
      regardless of graph settings, so this is launch-only, the same tier as
      `extra_args`/`custom_command`. Both are refused on a runtime with no arg
      for them (`sglang`, `tts`) rather than silently dropped, mirroring
      `speculative_refusal`.
      `enforce_eager` genuinely does what the title says -- it skips capture
      entirely, and a real launch's own log said "Enforce eager set,
      disabling torch.compile and CUDAGraphs."
      `cudagraph_capture_sizes` does not, and the UI briefly said it did
      before this was checked with a stopwatch. A 2x2 (Qwen3-1.7B and
      pythia-70m, at concurrency 1 and 384) read vLLM's own "Graph capturing
      finished in X secs" line rather than assuming one: Qwen3-1.7B took
      ~30-45s whether it captured 3 sizes or 94, pythia-70m took ~1s whether
      it captured 20 or 94 -- capture wall clock tracks MODEL SIZE, not list
      length, because the dominant cost is a roughly fixed per-launch warmup
      rather than a per-size one. The trimmed Qwen3-1.7B run was, if
      anything, a few seconds slower than the untrimmed one, well inside
      run-to-run noise on a shared box. What trimming actually buys is peak
      capture MEMORY, which does scale with list length: 0.64 GiB held for
      ~94 graphs on Qwen3-1.7B against 0.01 GiB for 10 -- matching vLLM's own
      stated reason for capping its default list in the first place ("avoids
      OOM in tight memory scenarios with small max_num_seqs"). The Serve
      panel's copy for this field says memory, not time, now.
      Still absent: a UI round trip through `?ctx=`-style URL state (both
      fields are local component state in the Serve panel, not linkable),
      and sglang's own graph-capture flags, which are a different shape
      (`--disable-cuda-graph` plus batch-size-keyed capture) and were not
      reverse-engineered without the image in front of anyone at the time.
- [x] Verify sglang's `TORCHINDUCTOR_CACHE_DIR`/`TRITON_CACHE_DIR` settings
      against a real image. The premise had gone stale: `scitrera/dgx-spark-
      sglang:0.5.12` (34.2 GB) was already sitting on this box, unrelated to
      this TODO, and `docker manifest inspect` confirmed the actual pinned
      default (`0.5.9-t5`) is pullable too -- that exact tag was not launched,
      only `0.5.12`. Launched for real through the normal derate path (not a
      hand-rolled `docker run`), sent it a chat completion so anything lazy
      would fire, then read the host side of the one bind mount.
      `TRITON_CACHE_DIR` is confirmed working: real compiled kernels
      (`.cubin`/`.ptx`/`.so`, e.g. `create_flashinfer_kv_indices_triton.*`)
      landed under `derate-runtime-cache/sglang/triton/` and survived the
      container being removed. `TORCHINDUCTOR_CACHE_DIR` is correctly pointed
      but stays empty -- not a bug, just a fact worth knowing: this recipe
      never passes `--enable-torch-compile`, so SGLang's default path never
      calls Inductor at all, only Triton JIT, which is why setting the
      variable still matters (Triton compiles unconditionally and would
      otherwise recompile from cold under `HOME=/tmp` on every launch, same
      as vLLM before `VLLM_CACHE_ROOT`).
- [x] Reap orphaned `sparkrun_*_solo` containers. `deploy/reap.py` surveys and
      prints its evidence; `--yes` is required to act, and there is no timer --
      this box is shared, and an automatic reaper that guesses wrong destroys a
      peer's work. The predicate is IDENTITY-NEGATIVE rather than name-positive,
      which is the whole finding: all four `sparkrun_*_solo` containers here are
      live `ready` deployments, launched by the coordinator inside a container
      and independently ADOPTED by the one on the host. A rule of "sparkrun-named
      and my store has no record" would have destroyed four live models. So a
      backend that answers `/v1/models` as itself is an independent veto, and
      this module never needs to know other coordinators exist. Test
      `test_a_container_whose_backend_answers_is_never_reapable_even_with_no_record`
      is that guard.
      The larger orphan population turned out to be inside the containers, not
      beside them: `sparkrun logs` follows for ever, `_snapshot_serving_log`
      polled it every 60s, and each poll stranded a `tail -f` that outlives its
      client on a local docker socket. Measured here: 1195, 1467, 1476 and 3007
      of them. Fixed at source -- `SparkrunAdapter.log_snapshot` reads the same
      file with a `tail -n` that exits on its own -- and the reaper clears what
      was already left.
- [x] Reap empty `/tmp/derate-load-logs-*` directories. The TODO blamed killed
      runs; the ratio said otherwise (96 directories, 92 EMPTY). `LOG_DIR` was a
      module-level `tempfile.mkdtemp` and `tests/unit/test_load.py` imports the
      harness at module scope, so pytest's own COLLECTION made one on every run,
      including runs that deselected every load test. `log_dir()` is lazy now, so
      the leak is gone at source, and `sweep_stale_log_dirs()` clears the backlog
      -- empty only, `os.rmdir` never `rmtree`, and nothing younger than an hour,
      because another harness may be running right now. The four non-empty ones
      hold the only copy of a dead run's stderr and survive every sweep.
- [x] Account for audio spend on the multipart path. The old comment said this
      was unfixable because transcription is priced per audio-minute; the premise
      was wrong for the provider that matters -- OpenRouter returns a usage block
      with its own `cost`, and `providers/usage.py` already reads that field. The
      only real blocker was mechanical: `open_upstream` hardcodes `json=payload`
      and an opaque upload has no dict. `open_upstream_raw` is that path with the
      body left opaque, sharing ONE retry/429/401/5xx ladder with the JSON form.
      Pricing and accounting are now separate: a provider that reports nothing
      still counts an unpriced request, because `_record_usage` returning early
      is what made a day of provider audio render as "$0.00, nothing served
      today" on the Spend screen.
      Also fixed on the way past: `count_tokens` was derived from the REQUEST
      (`body is not None`), which is backwards for both audio endpoints in
      opposite directions -- /audio/speech buffered a megabyte of MP3 looking for
      a usage block, and /audio/transcriptions refused to read the one response
      here that has one.
      Still not done, and deliberately: `GatewaySettings.daily_spend_cap_usd` is
      stored, surfaced and never read at request time. The only cap that bites is
      the per-provider `daily_budget_usd`. That is its own item, not this one.
- [ ] **Move the sparkrun pin to 0.3.8.** `SPARKRUN_VERIFIED_VERSION` and the
      Dockerfile's `SPARKRUN_VERSION` both still read 0.2.40, and so do the three
      skip-gated live tests. What upgrading breaks is already established above
      under "Upgrading sparkrun is cheap": the cluster handle gained a second hex
      segment, which `CLUSTER_ID_RE` is already loose enough to match, and 0.3.8
      warns that the recipe's `VLLM_CACHE_ROOT` overrides a runtime-cache mount it
      now manages itself -- a warning rather than a failure, but it means the
      compile cache lands somewhere sparkrun does not persist.
      `data_parallel_refusal` must NOT be deleted on the way past. It is a VERSION
      gate, not a permanent refusal: on 0.3.7+ the shape launches unchanged,
      already verified through derate's own `/v1` on 0.3.8. Moving the pin retires
      it on its own, and deleting it would drop the guard for anyone still on
      0.2.40.
      Skip the `runtime_cache:` migration deliberately. It would retire the
      `RUNTIME_CACHE_DIR`-inside-the-HF-cache hack, but the Launcher work deletes
      that path anyway; live with the warning rather than paying for it twice.
