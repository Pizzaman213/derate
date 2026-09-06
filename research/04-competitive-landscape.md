# Research Prompt 4: Competitive Landscape and Differentiating Features

## Objective
Map the products a Spark cluster control plane would be compared against, and identify which features users actually cite as reasons to pick one over another, so the roadmap targets real differentiators rather than assumed ones.

## Core Question
What does the market for local and on-prem cluster inference orchestration look like right now, and what unmet need is large enough to build around?

## Categories to Cover

### Distributed local inference
- **Exo**: peer-to-peer device clustering, automatic topology discovery, heterogeneous device support. This is arguably the closest philosophical competitor. Document its partitioning strategy and its known limitations.
- **Petals**: distributed inference over volunteer nodes, and why the model differs
- **llama.cpp RPC backend**: the low-effort multi-machine path, and how good it actually is
- **distributed-llama**, **Prima.cpp**, and similar smaller projects

### Prosumer single-node with cluster ambitions
- LM Studio, Ollama, Jan: what each has shipped or announced regarding multi-machine setups
- Any of these that have added Spark or GB10 support explicitly

### Enterprise and commercial
- NVIDIA AI Enterprise / NIM: pricing, what the license actually covers, and whether it applies to Spark
- Fireworks, Baseten, Together on-prem or BYOC offerings
- Prime Intellect and other decentralized compute plays
- Run:ai (now NVIDIA), and what its GPU orchestration layer does that is relevant at small scale

## For Each Product, Extract
- Pricing model and whether there is a free tier
- Target user: hobbyist, ML engineer, platform team, or enterprise IT
- Setup time from unbox to first token across multiple machines
- Whether it has a UI, and what that UI shows
- Observability: what metrics are exposed, and how
- Model catalog: curated, open Hugging Face passthrough, or both

## The Feature-Value Question
Go beyond marketing pages. Pull from GitHub issues, Reddit (r/LocalLLaMA in particular), Hacker News threads, and Discord logs where available to find:
- Which features people praise unprompted
- Which missing features cause people to switch away
- What the most common complaint is for each product
- Whether anyone is asking for exactly the thing being proposed: automatic parallelism config plus model-fit gating on a known hardware profile

## Deliverable
A positioning matrix placing each competitor on axes of (setup difficulty) versus (cluster capability), plus a ranked list of features by evidence of actual user demand. Explicitly call out any feature that is widely assumed to matter but that users never mention.

## Framing Note
The pitch context is NVIDIA GTC, so also note which competitors NVIDIA has acquired, partnered with, or built against. Anything that overlaps directly with a first-party NVIDIA product is a positioning risk worth flagging early.
