# Spark Control Plane

Planning and orchestration for DGX Spark clusters. Measures the interconnect, derives the parallelism plan from it, refuses launches that will run out of memory, and fronts every model behind one endpoint.

Target: NVIDIA GTC Berlin Golden Ticket, submission window closes September 10, 2026.

## Read in this order

1. `00-architecture.md` — scope, deployment model, frozen contracts, file ownership. Everyone reads it. Nobody edits it.
2. Your agent brief in `agents/`. Self-contained. You do not need to read the others.

Two documents, and no third. Your brief is the only spec for your workstream; if something contradicts it, the architecture wins and someone announces the change.

## Workstreams

| Agent | Owns | One line |
|---|---|---|
| A | `registry/` | Node agent, mDNS discovery, join, health, telemetry |
| B | `links/` | Measures real interconnect bandwidth. The number everything turns on. |
| C | `resolver/` | HuggingFace ID to `ModelShape`. Owns the quantization table. |
| D | `fit/` | The blocking out-of-memory gate. Refuses, and says what to change. |
| E | `planner/` | Chooses TP, PP, EP from measured facts. The differentiator. |
| F | `deploy/` | sparkrun adapter, lifecycle, Docker image |
| G | `gateway/` | One OpenAI endpoint, routing policies, admission control |
| H | `ui/` | The screen. Cluster graph, plan panel, refusal states. |
| I | `providers/` | OpenRouter and other remote upstreams as route targets |

## Day 0, before any agent starts

One person writes and freezes:

- `control_plane/contracts/**` from architecture section 4
- `tests/fixtures/**`: four model shapes (`llama-3.3-70b`, `gpt-oss-120b`, `qwen3-30b-a3b`, `deepseek-v3`), two GB10 node profiles, one 3090 profile, one `LinkMeasurement` at 10.2 GB/s with GPUDirect RDMA disabled

Every agent then ships a stub of their port in the first hour. Agent G's fixture stub is the highest leverage one, since it unblocks the UI entirely.

## Integration checkpoints

| When | What must work |
|---|---|
| T+8h | Every module imports and its unit tests pass against fixtures |
| T+16h | C, D, E wired. `POST /api/plan` returns a real plan for a real model ID |
| T+24h | A, B wired. Plans use a measured link, not a default |
| T+32h | F wired. A deployment launches through sparkrun and reaches READY |
| T+40h | G and I wired. Tokens return locally and spill to a remote provider |
| T+48h | H wired against live data |

Anything not integrated by its checkpoint gets stubbed and cut, not rescued.

## The demo

```bash
docker run --network host -v sparkplane:/data sparkplane/node   # node 1
docker run --network host -v sparkplane:/data sparkplane/node   # node 2, same command
```

Second node finds the first. UI shows both. Launch a model that will not fit on one. The plan panel says pipeline parallel, names the measured 10.2 GB/s link as the reason, and lists tensor parallel as rejected.

NVIDIA's own playbook specifies tensor parallel for two Sparks. At the bandwidth the link actually delivers, pipeline parallel wins batched serving by roughly 2.2x. The tool measures, disagrees with the playbook, and is right. That is the sixty seconds.

## The one failure mode to watch

An agent quietly changing a contract to unblock themselves. Contracts change in one place, by one person, announced. Watch for it at the T+8h checkpoint.
