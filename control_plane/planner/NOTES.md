# Planner: where the specification disagrees with itself

`agents/E-planner.md` gives both a decision function and an acceptance list. In
three places they cannot both be satisfied. Each resolution below picks the
reading that keeps the acceptance criteria testable, because those are the
checkable half of the specification, and each one is pinned by a test.

Anyone changing these should change the test first.

## 1. The MoE family branch versus "flips to tensor parallel"

**The conflict.** Step 3 sends any MoE model to data-parallel attention plus
expert parallel whenever `all_reduce >= EP_VIABLE_THRESHOLD and gpudirect_rdma`.
Acceptance criterion 3 says GPT-OSS-120B — a MoE model — must flip to *tensor*
parallel when the link is raised to 50 GB/s with GDR enabled. On two nodes those
are different answers.

**Resolution.** Cross-node expert parallel additionally requires
`MIN_NODES_FOR_CROSS_NODE_EP` (4) nodes. At EP=2 the expert weights halve, which
is exactly what TP=2 and PP=2 also do, while an all-to-all is added that neither
of those needs. DeepEP's overlap only starts paying at real expert-dimension
width. So on two nodes the MoE branch falls through to the general bandwidth
rule and 50 GB/s selects tensor parallel, satisfying criterion 3; on a wide
cluster the EP branch fires exactly as Step 3 describes.

Criterion 6 — "a MoE model with the link below threshold never returns cross-node
expert parallel" — is unaffected: the bandwidth and GDR gates are still hard and
still independent of the node count.

Tests: `test_answer_flips_when_only_the_measurement_changes`,
`test_moe_uses_expert_parallel_when_the_link_and_the_cluster_support_it`,
`test_expert_parallel_requires_a_wide_enough_cluster`.

## 2. The latency override is not restricted to dense models

**The conflict.** Step 3 places the `target == "latency" and concurrency <= 2`
rule inside the dense branch only. Acceptance criterion 2 applies it to
GPT-OSS-120B, which is MoE.

**Resolution.** The rule applies to both families. Its evidence — roughly 40 tok/s
under tensor parallel against 29 under pipeline at single stream — was measured
*on GPT-OSS-120B*, so restricting it to dense models would contradict its own
source. The override is evaluated before the family split.

Test: `test_latency_target_at_single_stream_chooses_tensor_parallel`.

## 3. Criterion 2's context, and why one acceptance case is tested differently

**The conflict.** Criterion 2 says "same model and link at concurrency 1 with
`target="latency"` gives tensor parallel". But Step 1 is absolute: a model that
fits on one node is never split. GPT-OSS-120B at concurrency 1 fits on a single
Spark at every context it supports — 57.8 GiB of MXFP4 weights and under 5 GiB
of KV at the full 131072 tokens, against 107.7 GiB usable. There is no context
at which criterion 2's literal setup requires two nodes.

**Resolution.** The single-stream rule is tested two ways rather than one:

- `test_latency_target_at_single_stream_chooses_tensor_parallel` pins `min_nodes`
  to 2 with a stub and asserts the decision, which is what the criterion is
  actually about.
- `test_latency_flip_end_to_end_on_a_model_that_needs_two_nodes` runs the same
  flip end to end on Llama 3.3 70B, which needs both nodes at any concurrency
  because its bf16 weights are 131 GiB.

And `test_model_that_fits_one_node_is_not_split` asserts the other half: at
concurrency 1 GPT-OSS-120B correctly returns a single-node plan. That is Step 1
working, not criterion 2 failing.

## 4. Criterion 1's context is stated explicitly

Criterion 1 (2 Sparks, 10.2 GB/s, concurrency 16 → pipeline) only holds at a
context where the model actually needs two nodes. At 32768 tokens GPT-OSS-120B
fits on one Spark and the planner correctly says so. The test uses the model's
native 131072, which is the real demo configuration and where 16 concurrent
sequences genuinely exceed one node.

`PlannerPort.plan()` is frozen without a context parameter, so context arrives as
a keyword-only extra and defaults to `DEFAULT_PLAN_CONTEXT`.

## Dependencies taken as stubs

`fit_bridge.py` imports `min_nodes_required` and `kv_bytes_per_token` from
`control_plane.fit` when Agent D's module exists, and answers them itself
otherwise, using the budget from the fit specification. The fallback is
conservative by construction: it may ask for one node more than necessary, never
one fewer. Nothing in `control_plane/fit/` is written or edited by Agent E.
