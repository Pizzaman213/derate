<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/brand/derate-lockup-dark.svg">
    <img src="docs/brand/derate-lockup.svg" width="280" alt="derate">
  </picture>
</p>

I started this while bringing up an agent swarm for [Jarvis](https://github.com/Pizzaman213/Jarvis), my self-hosted coding agent. The containers kept crashing and I had no way to watch them — which one died, why, or whether it ever had the memory to run at all. So I built derate.

Planning and orchestration for DGX Spark clusters. Measures the interconnect, derives the parallelism plan from it, refuses launches that will run out of memory, and fronts every model behind one endpoint.

## How it works

derate does not run inference. vLLM and SGLang do that, and NVIDIA's `sparkrun`
sets up the fabric and launches the processes. derate owns the four decisions in
front of them, and every one is made from a measurement rather than a default.

### It measures the link

`control_plane/links/` runs a ladder and labels each rung honestly for what it
is:

1. **nccl-tests** — `all_reduce_perf` and `sendrecv_perf` across the pair. The
   real answer, because it is the same code path an inference runtime takes.
   Both collectives are run: all-reduce governs whether tensor parallel is
   viable, sendrecv governs pipeline stage handoff and KV transfer, and the two
   differ enough that deriving one from the other would be guessing.
2. **ib_write_bw** — raw RDMA, scaled and flagged as an estimate. Reporting raw
   RDMA as if it were NCCL bandwidth is precisely the error that makes the
   ecosystem's defaults wrong here.
3. **TCP** — wire throughput on the data-plane interface. Coarse, flagged, and
   still better than nothing.

If all three fail it returns nothing at all. A missing measurement is a state
the planner handles; a fabricated one produces a plan that silently
underperforms. Measurement is serialised, because two collectives running over
the same fabric at once would each report the other's interference as the
link's speed. Reads never block on it.

### It resolves the model to a shape

`control_plane/resolver/` turns a HuggingFace id into a `ModelShape`: layer
count, hidden size, attention and KV head counts, total against active
parameters, quantization. Every field is either read from real metadata or
carries a warning saying it was not — the memory arithmetic and the parallelism
choice are both wrong if the KV head count or the active parameter count is
wrong here.

The mapper never asks a runtime anything, so a shape cannot depend on which
image happens to be installed. `python3 -m tests.model_sweep` proves that by
resolving the whole corpus a second time with the image's registry recorded.

### It plans the parallelism from those two facts

Legality first, because it is not a matter of taste: a tensor-parallel degree
that does not divide `num_attention_heads` and `num_kv_heads` fails at model
load, so the planner never emits one. Then arithmetic.
`control_plane/planner/comm.py` computes what each strategy actually moves per
decode step, and the plan quotes its own numbers back:

```
PP=2 across spark-01 and spark-02: measured all-reduce is 10.2 GB/s, below the
40 GB/s threshold where tensor parallel becomes competitive, and at concurrency
16 pipeline moves 262 KB per step over 1 exchange against tensor parallel's
41.9 MB over 160 exchanges.
```

Rejected strategies are kept, not discarded, each with the reason it lost:

```
TP=2: measured all-reduce 10.2 GB/s is below the 40 GB/s threshold; 2
all-reduces per layer across 80 layers is 160 cross-node exchanges and 41.9 MB
per step, which would dominate at concurrency 16
```

That is the whole disagreement with NVIDIA's own playbook, which specifies
tensor parallel for two Sparks. At the bandwidth the link actually delivers,
pipeline parallel wins batched serving by roughly 2.2x.

### It refuses launches that will not fit

`control_plane/fit/` is a blocking gate, not an estimate. Per rank:

```
weights + kv_cache + activations + comm_buffers + replicated
    + framework_overhead  <=  usable memory
```

Everything else in this space estimates and then lets you launch anyway. This
refuses, and a refusal names the term that blew the budget and the specific
change that would work:

```
Won't fit: weights alone are 131.4 GiB per rank against 107.7 GiB usable (90%
of 119.7 GiB addressable on NVIDIA GB10). Over budget by 65.1 GiB in total.
Context and concurrency cannot fix this at bf16 on 1 rank — it needs 2 nodes or
requantize to fp8 (65.7 GiB per rank).
```

It also refuses things that *would* load, when loading is not the same as
working:

```
Loads with 19.1 GiB to spare, but predicted decode is 1.0 tok/s, under the 10
tok/s usability threshold. Bandwidth bound: 143.8 GB moves per decoded token at
273 GB/s. Adding nodes will not fix this — a smaller quantization, a smaller
model, or an MoE with fewer active parameters will.
```

Both of those are this repo's own output against its fixtures, not
illustrations. They render through the UI verbatim: never truncated, re-cased
or summarised, because rewriting a refusal destroys the thing that made it
useful.

The budget prefers what a node can hand out *now* and falls back to the static
ceiling when there is no live reading — it never refuses for want of one. On
GB10 that distinction is the whole game: memory is unified, `nvidia-smi`
reports aggregate memory as N/A, and the addressable ceiling overstates what is
actually free by roughly 10x because the operating system spends from the same
pool.

### It launches, and says which of the slow things it is on

A launch goes `preparing → downloading → loading → starting → serving`, and
`control_plane/deploy/progress.py` classifies it from literal markers sparkrun
and the runtime print, showing their line verbatim. `serving` is not read off a
log line: the health probe decides it, because a runtime claiming to be ready
is not the same as a port that answers.

The slow parts are not slow in the order you would guess. Measured on a 0.5B
launch with image and weights already local: CUDA graph capture 40s+, the
python/vLLM import ~26s (twice — once for the API server, once for the engine
fork), sparkrun prep and `docker run` ~19s, cold `torch.compile` 7.5s, and
**weight load 5.6s**. The shard-counting progress bar everyone writes covers
the cheapest step.

The fit gate's arithmetic is also what sizes the launch.
`--gpu-memory-utilization` is not a limit — vLLM reads it as a claim on the
whole device and refuses to start unless that share is free right now — so a
constant asks for the same slice of the machine for a 0.5B model as for a 120B
one, and dies on any node with neighbours. `control_plane/deploy/utilization.py`
derives it from the fit breakdown instead, capped by what is free.

### It fronts everything behind one endpoint

`control_plane/gateway/` is the only HTTP surface: 77 routes on the
coordinator, of which `/v1/models`, `/v1/chat/completions`, `/v1/completions`,
`/v1/embeddings` and the audio routes are the OpenAI-compatible ones. Every
model in the cluster is listed there, whether it is a deployment this control
plane launched or a model on a remote provider.

Routing is per served name — `least_outstanding` by default, with
`weighted_capacity` for unequal nodes, `cache_affinity`, `failover`,
`cost_aware`, and `local_first` to spill to a remote provider when the local
one saturates. Weights are recomputed on a 60 second cadence so a thermally
throttling node sheds share on its own, while outstanding counts and the
admitting flag are re-read on every selection, so a critical memory event takes
effect on the next request rather than at the next refresh.

`control_plane/providers/` makes OpenRouter, OpenAI, Anthropic, Together,
Groq, Ollama, and any OpenAI-compatible URL as `custom` into ordinary route
targets. No API key is ever rendered:
responses are scrubbed on the way to the screen, and there is no inverse.

## The cluster

A node announces itself over mDNS and the coordinator holds the roster;
`control_plane/registry/` owns discovery, join, health and telemetry. One image
and one container serve both roles, decided at runtime — there is no separate
worker build, which is why **Install** below is nearly the same line twice.

Nodes are not assumed to be identical. The fit gate budgets against the
smallest one under whichever basis is in force, and a machine that cannot carry
a tensor- or pipeline-parallel rank — a Mac, a Windows box, anything not
launching through `sparkrun` — says so in the roster rather than implying
otherwise.

## The screen

`ui/` is a React app the gateway serves itself, at the same origin as the API —
`/dashboard`, `/models`, `/cluster`, `/chat`, `/spend`, `/settings`, and
`/setup`. The path names the screen and its subject
(`/models/meta-llama/Llama-3.1-8B`); the query names what is selected
(`?node=`, `?link=`, `?dep=`). Every screen has a real URL, so a link to one
node's page is a link somebody can send.

There is no UI test runner. The gate is `tsc -b` plus a set of `*.check.mjs`
verifiers, each of which exists because a specific class of bug is invisible to
types, and `npm run screens` drives a real browser over every destination and
writes a PNG — the only thing here that sees what shipped rather than inferring
it from source.

## Where things live

| Package | What it does |
|---|---|
| `control_plane/contracts/` | The frozen types every other package agrees on |
| `control_plane/registry/` | Node agent, mDNS discovery, join, health, telemetry |
| `control_plane/links/` | Measures real interconnect bandwidth. The number everything turns on. |
| `control_plane/resolver/` | HuggingFace id to `ModelShape`. Owns the quantization table. |
| `control_plane/fit/` | The blocking out-of-memory gate. Refuses, and says what to change. |
| `control_plane/planner/` | Chooses TP, PP, EP from measured facts |
| `control_plane/deploy/` | sparkrun adapter, launch lifecycle, progress classification |
| `control_plane/gateway/` | One OpenAI endpoint, routing, admission control, and the built UI |
| `control_plane/providers/` | OpenRouter and other remote upstreams as route targets |
| `control_plane/runtimes/` | The inference servers derate ships itself. Runs inside a model container. |
| `ui/` | The screen. Cluster graph, plan panel, refusal states. |

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

## macOS and Windows

`install.sh` installs a container, and the flags above are Linux-host features:
Docker Desktop's `--network host` does not reach the LAN, `--gpus` needs the
NVIDIA container toolkit, and `--pid=host` has no host to name. Those machines
run the package natively instead.

```bash
pipx install derate && derate                       # the first machine

DERATE_JOIN=http://<coordinator>:8080 \
DERATE_TOKEN=ej_... derate                          # every machine after
```

PowerShell:

```powershell
pipx install derate
$env:DERATE_JOIN='http://<coordinator>:8080'; $env:DERATE_TOKEN='ej_...'; derate
```

Settings -> Add a node composes the line for whichever platform you pick.

Such a machine is a full cluster member: discovered over mDNS, reporting its
real memory and CPU, and able to serve models through a local runtime like
Ollama added under Settings -> Providers. **It cannot carry a tensor- or
pipeline-parallel rank** — those launch through `sparkrun` onto Linux GPU nodes
— and the roster says so rather than implying otherwise. A link to one is
measured over TCP, which the planner labels as the coarse estimate it is.

State lives in `%LOCALAPPDATA%\derate` on Windows and `~/Library/Application
Support/derate` on macOS; `DERATE_DATA_DIR` overrides it. On Windows file modes
are not enforced, so provider API keys stored there are readable by anyone who
can log in to that machine — supply them through an environment variable and
store only the reference. Settings says so on screen rather than leaving it to
this paragraph.

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

Four ways to serve audio, cheapest first:

1. **A remote provider.** Add OpenAI or Groq and their audio models are routed
   like any other.
2. **An OpenAI-compatible server on the LAN** — Kokoro-FastAPI,
   openedai-speech, faster-whisper-server. Add it as a `custom` provider with
   its base URL; nothing needs to be launched by the control plane.
3. **A Whisper deployment the control plane launches**, on vLLM. Planned,
   fit-checked and launched like any other model; it is recorded as a
   transcription deployment, so it is offered on `/v1/audio/transcriptions` and
   kept off the chat routes.
4. **A TTS deployment the control plane launches**, on the `tts` runtime —
   derate's own server (`control_plane/runtimes/tts.py`), because neither vLLM
   nor SGLang serves `/v1/audio/speech` at all and neither will load a
   text-to-speech checkpoint's architecture. Pick the model, pick `tts`, press
   Serve: it is planned, fit-checked and launched like any other deployment,
   and it answers `POST /v1/audio/speech` behind the same gateway as
   everything else.

   ```bash
   curl localhost:8080/v1/audio/speech \
     -H 'content-type: application/json' \
     -d '{"model": "audio8-tts", "input": "The link is measured, not assumed.",
          "response_format": "mp3"}' --output line.mp3
   ```

   It writes `wav`, `mp3`, `flac`, `opus` and `pcm`; `aac` is refused by name
   rather than served as something else. `speed` is refused too — resampling
   moves the pitch, and a 1.5× that quietly returned a chipmunk would be worse
   than a sentence saying so. It generates one request at a time, so
   `--max-num-seqs` bounds how many callers may be waiting and the rest get a
   503 the router already knows how to hold.

   These checkpoints clone a voice zero-shot from a reference clip plus that
   clip's exact transcript, so a "voice" is a file pair. Point the runtime at a
   directory of them (`DERATE_TTS_VOICE_DIR`, or `--voice-dir`), one
   `<name>.wav` beside one `<name>.txt`; `GET /v1/audio/voices` lists what
   loaded and what was skipped. Omit `voice` entirely and the model speaks in
   its own. An unknown voice is refused, naming the ones installed — returning
   a different speaker under a 200 is the failure you cannot hear until
   somebody else does.

   Verified on a GB10 against `Audio8/Audio8-TTS-Preview-0.6b`, a 0.6B DualAR
   model with a 44.1 kHz codec: 4.4 seconds of speech in 4.3 seconds of wall
   clock, and 2451 MiB resident against the fit gate's predicted 2.3 GiB.

An audio deployment reports `—` rather than a token rate, in the strip and in
the inspector. There is no audio-side rate measured today, and a zero would
read as a stalled deployment.

## The demo

```bash
curl -fsSL .../install.sh | sh                                  # node 1
curl -fsSL http://node-1:8080/install.sh | sh -s -- --join http://node-1:8080 --token ej_...
```

Second node joins the first and appears as a member. Launch a model that will not fit on one. The plan panel says pipeline parallel, names the measured 10.2 GB/s link as the reason, and lists tensor parallel as rejected.

That is the sixty seconds: it measures the link, disagrees with the vendor playbook, and shows the arithmetic it disagreed on.

## Looking something up

- **`CONTRACTS.md`** — every type, enum, constant, derived fact, route and environment variable, generated from the code. A test fails when it goes stale, so it cannot be wrong in a way the code is not. This is where you look something up.
- **`00-architecture.md`** — why any of it is shaped this way. It is a journal: the early sections are never edited and the dated appendices amend them, so start from the index at the top rather than reading down.
- **`ui/README.md`** — the screen, the URL scheme, and the verifiers.
- **`CLAUDE.md`** — the part that is only useful while you are editing.

Target: NVIDIA GTC Berlin Golden Ticket, submission window closes September 10, 2026.
