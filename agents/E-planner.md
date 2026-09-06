# Agent E: Parallelism Planner

Read `00-architecture.md` first. Contracts there are frozen.

**You own:** `control_plane/planner/**`, `tests/test_planner.py`
**You depend on:** `ModelShape` from C, `NodeProfile` from A, `LinkMeasurement` from B, and D's exported helpers
**Downstream of you:** F launches what you decide. H displays your reason string verbatim.

---

## What you build

The component that decides tensor, pipeline, expert, and data parallel degrees from measured hardware facts.

This is the product. Nothing else in the ecosystem does it. vLLM, SGLang, TensorRT-LLM, and Ray all take the degrees from the user and only validate legality at startup. Dynamo sweeps tensor-parallel sizes offline but has no GB10 profile in its hardware library and its profiler is single-node only. sparkrun maps `--tp N` straight to N hosts and never chooses N.

Your output includes a one-sentence `reason` that appears verbatim in the UI. That sentence is the entire product thesis made visible, something like: "PP=2, chosen over TP=2 because measured all-reduce is 10.2 GB/s and target concurrency is 16."

---

## The decision function

```
Step 0. Facts
    link = links.worst_all_reduce(node_ids)
    If link is None: conservative mode. Prefer pipeline parallel, note in reason
    that no measurement is available and recommend running one.

Step 1. Capacity
    min_nodes = D.min_nodes_required(shape, profile, context, max_seqs)
    If min_nodes == 1: SINGLE_NODE. Stop.
    Never cross the link for a model that fits on one node. Run a second
    replica on the second node instead and let the gateway load balance.

Step 2. Legality
    Tensor parallel requires num_attention_heads % tp == 0
                        and num_kv_heads % tp == 0
    Pipeline parallel has no head constraint and tolerates uneven layer splits.
    Compute valid_tp as the set of divisors satisfying both, bounded by
    available node count.

Step 3. Family
    if shape.is_moe:
        if link.all_reduce_gbps >= EP_VIABLE_THRESHOLD and link.gpudirect_rdma:
            plan = data parallel attention + expert parallel
        elif fits on one node:
            plan = single node, expert parallel within it
        else:
            plan = pipeline parallel across nodes
    else:
        if target == "latency" and concurrency <= 2 and 2 in valid_tp:
            plan = tensor parallel
        else:
            plan = pipeline parallel

Step 4. Pipeline bubble guard
    If pipeline parallel and expected in-flight requests < 4 * pp_degree,
    warn that the bubble will exceed roughly 20 percent and that tensor
    parallel may serve better at this concurrency.
```

---

## Why these rules

Each rule is measured, not folklore. Keep the citation in a comment so the next person does not "fix" it.

**Tensor parallel moves two all-reduces per layer. Pipeline parallel moves one activation handoff per token.** On an 80-layer model that is 160 cross-node exchanges per output token against one. For a dense 70B at batch 1, tensor parallel moves roughly 2.6 MB per token against pipeline's roughly 16 KB, a factor of about 160.

**At roughly 10 GB/s, pipeline wins batched serving decisively.** Measured on GPT-OSS-120B across two Sparks: tensor parallel reaches 252 tok/s at batch 128 while pipeline reaches 555, a 2.2x advantage. This contradicts NVIDIA's own playbook, which specifies tensor parallel only.

**Tensor parallel still wins at batch 1.** Same measurements: roughly 40 tok/s against 29 at single-stream. Pipeline idles half the time with two stages and no batching. This is why `target == "latency"` and low concurrency flips the answer.

**The threshold is `TP_VIABLE_THRESHOLD`, 40 GB/s.** Above it tensor parallel becomes competitive for batched serving again. Read the measurement every time. If a driver update enables GPUDirect RDMA the bandwidth roughly doubles and your answer should flip on its own. Never hardcode 10.2.

**Cross-node expert parallel is refused below threshold.** DeepEP assumes InfiniBand or RoCE with GPUDirect and loses most of its overlap benefit on a PCIe-fed path without it. Measured internode dispatch on a properly equipped H800 cluster is roughly 43 GB/s; the Spark link is four times slower than that already-degraded case.

**Even splits are not automatically optimal.** Published sweeps found TP2/PP8 beating TP4/PP4 for one model and TP4/PP4 beating both alternatives for another. Do not assume symmetry is best; it is model and workload specific.

---

## The rejected list

Populate `ParallelismPlan.rejected` with every alternative you considered and why you did not choose it, one line each:

```
"TP=2: measured all-reduce 10.2 GB/s is below the 40 GB/s threshold; 2 all-reduces
 per layer across 80 layers would dominate at concurrency 16"
"EP=2: GPUDirect RDMA disabled, cross-node all-to-all loses its overlap benefit"
"TP=4: illegal, num_kv_heads=8 is not divisible by 4 for this model"
```

This list is what makes the tool trustworthy rather than magic. Show the work.

---

## Heterogeneous nodes

A 3090 desktop and a Spark have different memory sizes, different bandwidth, and a slower link between them. Do not put them in one serving pool by default. Group nodes into homogeneous sets, plan within a set, and expose the other set as a separate deployment target. Say plainly in the reason when a node was excluded and why.

---

## Interface you must satisfy

```python
class PlannerPort(Protocol):
    def plan(
        self, shape: ModelShape, nodes: list[NodeProfile],
        link: LinkMeasurement | None, target: str, concurrency: int,
    ) -> ParallelismPlan: ...
```

Plus:

```python
def valid_tp_degrees(self, shape: ModelShape, max_nodes: int) -> set[int]
def alternatives(self, shape, nodes, link, target, concurrency) -> list[ParallelismPlan]
def explain(self, plan: ParallelismPlan) -> str
```

`alternatives` returns every legal plan ranked, so the UI can offer an override. The user can always override; you are a recommendation with reasoning, not a lock.

---

## Day 0 stub

Return PP=2 across two nodes with a fixed reason string. F and H need something callable immediately.

---

## Acceptance

- GPT-OSS-120B, 2 Sparks, link 10.2 GB/s, concurrency 16 gives pipeline parallel, and the reason names both the measured bandwidth and the concurrency.
- Same model and link at concurrency 1 with `target="latency"` gives tensor parallel.
- Same model with the link measurement raised to 50 GB/s and GDR enabled flips to tensor parallel at concurrency 16. This test is the proof that nothing is hardcoded.
- A model with 8 KV heads never produces TP=16 or any other illegal degree.
- Qwen3-30B-A3B on 2 Sparks returns single node, with a reason explaining that a second replica beats splitting.
- A MoE model with the link below threshold never returns cross-node expert parallel.
- Every plan has a non-empty reason and, when alternatives existed, a non-empty rejected list.
- With `link=None`, returns a conservative pipeline plan and says a measurement is missing.

## Traps

Do not hardcode the bandwidth. Do not port the "tensor parallel inside a node, pipeline parallel across nodes" rule without its condition; that rule assumes NVLink inside and InfiniBand across, and the Spark's single-hop link is a case it was not written for. Do not silently pool heterogeneous hardware. Do not return a plan without a reason, the reason is what the judge reads.
