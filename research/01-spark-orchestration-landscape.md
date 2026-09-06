# Research Prompt 1: DGX Spark / GB10 Orchestration Landscape

## Objective
Establish what already exists for orchestrating and clustering NVIDIA DGX Spark (GB10) nodes, and identify exactly where the gaps are that a custom control plane could fill.

## Core Question
What tooling exists today for discovering, interconnecting, and serving models across multiple DGX Spark units, and what does each tool automate versus leave to the user?

## Areas to Cover

### Official NVIDIA tooling
- NVIDIA Sync and what it actually manages (provisioning, monitoring, model launch, or just setup)
- DGX Spark Playbooks: which ones cover multi-node, and how much is scripted versus documented steps
- NVIDIA's own guidance on the ConnectX / QSFP interconnect between two Sparks, including supported topologies and whether more than two nodes are officially supported
- Whether NGC containers ship with any Spark-specific cluster awareness
- DGX OS / Base Command overlap: does anything from the DGX server line apply here

### Community and third-party tooling
- GitHub projects targeting DGX Spark clustering, GB10, or Grace Blackwell desktop nodes specifically
- Ray, Slurm, and Kubernetes setups people have actually gotten working on Spark clusters, including reported pain points
- Blog posts, forum threads, and videos documenting real two-node or larger Spark deployments

### For each tool found, document
- Does it auto-discover peer nodes, or require manual IP/host config?
- Does it know the hardware profile (unified memory size, interconnect bandwidth, NVLink-C2C characteristics)?
- Does it choose parallelism settings, or does the user pass them in?
- Does it validate that a given model will fit before launch?
- What is the interface: CLI, web UI, API, or config file?

## Deliverable
A table of every tool found, scored on the five questions above, plus a written summary of the specific automation gaps nobody has filled.

## Notes
Pay particular attention to anything published after the Spark's general availability, since early coverage was mostly unboxing and single-node benchmarks. Prioritize primary sources: NVIDIA docs, repo READMEs, and issue threads over secondhand articles.
