# Research Prompt 3: Topology-Aware Parallelism Auto-Configuration

## Objective
Understand how serious inference stacks decide the parallelism plan for a given model and cluster, so a Spark control plane can pick tensor, pipeline, expert, and data parallel degrees automatically instead of making the user guess.

## Core Question
Given N nodes with known memory, compute, and interconnect bandwidth, how does each system choose TP, PP, EP, and DP degrees, and how much of that decision is automated versus hand-tuned?

## Systems to Analyze

- **vLLM**: `--tensor-parallel-size`, `--pipeline-parallel-size`, `--data-parallel-size`, `--enable-expert-parallel`. What defaults exist, what validation runs at startup, and what the docs say about when to prefer PP over TP.
- **NVIDIA Dynamo**: disaggregated prefill/decode, the planner component, KV-cache-aware routing, and any auto-sizing logic. This is the closest thing to a competitor for the control-plane concept, so cover it in depth.
- **SGLang**: its parallelism options and router, plus RadixAttention implications for cache-aware placement.
- **llm-d**: the Kubernetes-native approach, its inference scheduler, and how it models heterogeneous accelerators.
- **Ray Serve / Ray on vLLM**: placement groups, bundle scheduling, and how topology information reaches the scheduler.
- **TensorRT-LLM**: its build-time parallelism config and any auto-parallel features.

## Specific Things to Extract

### The decision rules
- The heuristic for TP degree: typically bounded by attention head count divisibility and intra-node bandwidth. Document the exact constraints each system enforces.
- When PP is chosen instead, and what the bubble/latency tradeoff looks like at small batch sizes
- How interconnect bandwidth enters the decision, if at all. Most systems assume NVLink intra-node and Ethernet/IB inter-node. Spark's ConnectX link between two nodes is an unusual middle case, so note what breaks in that assumption.
- Expert parallelism for MoE: when it is chosen over pure TP
- Whether any system measures the actual link and adapts, or whether all of them rely on static assumptions

### Communication patterns
- Per-token all-reduce volume under TP as a function of hidden size and layer count
- Point-to-point transfer volume under PP
- Which one degrades worse on a link that is fast but not NVLink-fast, which is the central question for Spark clustering

### Failure modes
- Known issues with multi-node vLLM at low TP or PP degrees
- What error messages users hit when a config is invalid, and whether the system suggests a fix

## Deliverable
A decision tree or scoring function that maps (model architecture, node count, per-node memory, link bandwidth, target latency vs throughput) to a recommended parallelism plan. Cite which system each rule came from, and flag rules that are folklore versus rules backed by published benchmarks.
