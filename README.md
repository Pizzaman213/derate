# Derate

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

## Install

```bash
# the first machine. It becomes the coordinator and serves the UI on :8080.
curl -fsSL https://raw.githubusercontent.com/Pizzaman213/derate/integration/install.sh | sh

# every machine after. The UI composes this line for you, address and token
# already filled in: Settings -> Add a node.
curl -fsSL http://<coordinator>:8080/install.sh | sh -s -- \
    --join http://<coordinator>:8080 --token ej_...
```

One image, one container, role decided at runtime. The script checks Docker,
pulls `ghcr.io/pizzaman213/derate/node`, and runs it with host networking and a data volume;
`--dry-run` prints the `docker run` it would use and stops, `--uninstall`
reverses it. The `docker run` form below is still the whole of what it does and
remains supported.

The token in the second command is an **enrollment token**: minted on demand,
expiring in an hour, spent by the machine that uses it. A node holding one is
admitted on arrival, so there is nothing to click. The permanent cluster token
still only makes a candidate — it is not the thing you carry around any more.

```bash
docker run --network host --gpus all --pid=host \
    -v derate:/data ghcr.io/pizzaman213/derate/node    # equivalent, by hand
```

`--gpus all` because the hardware probe is `nvidia-smi`, and a container
without one probes as unidentified hardware the planner will not place work
on — a node that joins, reports healthy, and shows zeros. `--pid=host`
because on GB10 per-process accounting is the only memory number nvidia-smi
will still give you, and it only counts processes in its own namespace.
`install.sh` passes both for you.

## Voice and TTS

The OpenAI surface covers audio as well as text:

```
POST /v1/audio/speech            text in, audio bytes out
POST /v1/audio/transcriptions    audio file in, text out
GET  /v1/realtime                websocket, relayed to a provider
```

`GET /v1/models` reports a `modality` per model (`text`, `embedding`, `speech`,
`transcription`), and naming a model on the wrong endpoint is refused with the
one that would have worked. That matters even if you never serve audio
yourself: an OpenAI or OpenRouter catalog is ingested wholesale, so `tts-1` and
`whisper-1` arrive as ordinary models, and without the modality they show up in
the chat picker as if they were chat models.

Three ways to serve audio, cheapest first:

1. **A remote provider.** Add OpenAI or Groq and their audio models are routed
   like any other.
2. **An OpenAI-compatible server on the LAN** — Kokoro-FastAPI,
   openedai-speech, faster-whisper-server. Add it as a `custom` provider with
   its base URL; nothing needs to be launched by the control plane. This is the
   shortest path to local TTS, because neither vLLM nor SGLang serves
   `/v1/audio/speech` at all.
3. **A Whisper deployment the control plane launches**, on vLLM. Planned,
   fit-checked and launched like any other model; it is recorded as a
   transcription deployment, so it is offered on `/v1/audio/transcriptions` and
   kept off the chat routes.

An audio deployment reports `—` rather than a token rate, in the strip and in
the inspector. There is no audio-side rate measured today, and a zero would
read as a stalled deployment.

## The demo

```bash
curl -fsSL .../install.sh | sh                                  # node 1
curl -fsSL http://node-1:8080/install.sh | sh -s -- --join http://node-1:8080 --token ej_...
```

Second node joins the first and appears as a member. Launch a model that will not fit on one. The plan panel says pipeline parallel, names the measured 10.2 GB/s link as the reason, and lists tensor parallel as rejected.

NVIDIA's own playbook specifies tensor parallel for two Sparks. At the bandwidth the link actually delivers, pipeline parallel wins batched serving by roughly 2.2x. The tool measures, disagrees with the playbook, and is right. That is the sixty seconds.

## The one failure mode to watch

An agent quietly changing a contract to unblock themselves. Contracts change in one place, by one person, announced. Watch for it at the T+8h checkpoint.
