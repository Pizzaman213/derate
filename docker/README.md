# The container

One image, `ghcr.io/pizzaman213/derate/node`, on every machine. Role is resolved at runtime.
There is no coordinator image and no worker image, and there is no second
service for the UI.

## Run it

```bash
docker run --network host --gpus all --pid=host -v derate:/data ghcr.io/pizzaman213/derate/node
```

That is the whole install. The first node finds no coordinator, becomes one,
prints a cluster token and serves the UI on :8080. The second node, given the
same command with no flags and no IPs, finds the first over mDNS and joins.

To also launch inference backends, the container needs the host's sparkrun
config and its SSH identity, because sparkrun drives the cluster over SSH:

```bash
docker run -d --name derate \
  --network host \
  --gpus all --pid=host \
  --restart unless-stopped \
  -v derate:/data \
  -v "$HOME/.config/sparkrun:/root/.config/sparkrun:ro" \
  -v "$HOME/.ssh:/root/.ssh:ro" \
  ghcr.io/pizzaman213/derate/node
```

Without those two mounts everything else still works -- discovery, the
roster, link measurement, planning, the fit verdict, the rendered command --
and a launch is refused with a message saying so. Nothing fails silently.

`compose.yaml` in the repo root does the same thing for people who prefer
compose. It is never required.

## `--network host` is not optional

mDNS is multicast and does not cross a Docker bridge, and the node agent has
to see the real interfaces to report the ConnectX-7 topology. A bridged
container starts cleanly, reports itself healthy, and never discovers
anything -- silent, and indistinguishable from "there is no second node".

So the container checks at startup and refuses:

```
derate refuses to start: this container is on bridge networking.
...
Start it again with --network host:
    docker run --network host -v derate:/data ghcr.io/pizzaman213/derate/node
```

`DERATE_ALLOW_BRIDGE=1` downgrades this to a loud warning. It is
unsupported and discovery will not work.

## `--gpus all --pid=host` is not optional either

The probe is `nvidia-smi`. A container without the GPU has no `nvidia-smi`,
and the probe's answer for that container is not an error -- it is a
`NodeProfile` with no GPU name and zeroed memory, because
`registry/probe.py` never raises. That node joins, reports healthy, and sits
in the roster reporting real host memory, temperature and CPU utilisation
next to a GPU it will never be given work on. Nothing has failed, so nothing
says so.

What it is *called* there depends on whether there is a GPU on the machine
at all, and the probe does not decide that from the absence of `nvidia-smi`:
it also reads `/proc/driver/nvidia/version` and the PCI vendor ids, both of
which a container started *without* `--gpus` can still see -- the proc entry
belongs to the loaded driver and the bus is the host's.

- **A GPU machine missing these flags** keeps `device_class=UNKNOWN` and
  `ineligible_reason: "device class is not recognized"`. That is the whole
  point: the card is found, `nvidia-smi` is not, and the roster saying
  "unrecognised" is how anybody finds out this container was started wrong.
- **A machine with no NVIDIA hardware** -- a Raspberry Pi, a NAS -- gets
  `device_class=CPU` and is eligible. It cannot carry a rank, because
  `addressable_memory` is 0 and the fit gate refuses on that, but it is a
  cluster member in good standing rather than a machine we failed to
  identify.

`--pid=host` is part of the same requirement rather than a second one. On
GB10 every aggregate FB memory field reports `[N/A]` -- unified memory, no
discrete framebuffer to describe -- so `--query-compute-apps` is the only
thing that separates a resident model from the desktop, and that is the
difference the fit check turns on. `nvidia-smi` reports the processes it can
see, and inside its own PID namespace it can see none of the ones holding
the pool: `gpu_memory_used` reads 0 and every byte gets attributed to the
operating system.

`install.sh` passes both when the host has an NVIDIA driver. If Docker then
refuses them -- the driver is present but the NVIDIA container toolkit is
not -- it retries without, and says what was lost rather than leaving the
machine uninstalled. `--no-gpu` skips them deliberately.

The image also sets `NVIDIA_VISIBLE_DEVICES=all` and
`NVIDIA_DRIVER_CAPABILITIES=utility`, so a host that has made the NVIDIA
runtime its default needs no flag at all. `utility` is nvidia-smi and NVML
and nothing else, which is all this image ever wants a GPU for.

## Configuration

Every variable has a working default, so first run needs none.

| Variable | Default | Meaning |
|---|---|---|
| `DERATE_ROLE` | `auto` | `coordinator` or `worker` forces the role |
| `DERATE_PORT` | `8080` | gateway and UI, coordinator only |
| `DERATE_AGENT_PORT` | `8081` | node agent, both roles |
| `DERATE_TOKEN` | generated | cluster join token, or an enrollment token from the UI; the permanent one is generated and persisted on first run |
| `DERATE_JOIN` | mDNS | an address, to skip discovery on another subnet |
| `DERATE_DATA` | `/data` | persisted state |
| `DERATE_VLLM_IMAGE` | see `flags.py` | container image for vLLM backends |
| `DERATE_SGLANG_IMAGE` | see `flags.py` | container image for SGLang backends |
| `DERATE_ALLOW_BRIDGE` | unset | bypass the host-networking check, unsupported |
| `DERATE_LOG_LEVEL` | `INFO` | root log level |
| `DERATE_TELEMETRY` | `1` | `0` records nothing, anywhere |
| `DERATE_TELEMETRY_RETENTION_DAYS` | `30` | raw-sample horizon; rollups outlive it |
| `DERATE_TELEMETRY_LOG_LEVEL` | `INFO` | floor for log lines that reach the journal |
| `DERATE_TELEMETRY_MAX_BYTES` | 512 MiB | ceiling on a node's own journal |
| `DERATE_TELEMETRY_SHIP_INTERVAL_S` | `5` | how often the coordinator collects |
| `DERATE_TELEMETRY_QUIET_LOGGERS` | `uvicorn.access,httpx` | logger prefixes never journalled; `""` records everything |
| `DERATE_SHELL` | `0` | `1` adds an interactive root shell on this node — read below first |
| `DERATE_SHELL_KEY` | generated | the secret that opens a session; no route mints or returns it |
| `DERATE_SHELL_ORIGINS` | unset | browser origins allowed to open a shell socket; unset means no check |
| `DERATE_SHELL_IDLE_S` | `1800` | seconds of inactivity before a session closes itself |
| `DERATE_INSTALL_SH` | in-image | the script served at `GET /install.sh` |

### The node shell

`DERATE_SHELL=1` puts an interactive terminal on the node's page in the UI.
Understand what it is before turning it on.

The session is a real pty, and on any node started with `--pid=host` it
`nsenter`s into PID 1's namespaces — so it is a **root prompt on the machine**,
not in the container. This container also mounts the operator's `~/.ssh`
read-only and ships `openssh-client`, because sparkrun drives the fleet over
SSH. A shell on one node therefore reaches every machine those keys reach.

Nothing else on this product's HTTP surface is authenticated: `/api` has no
credential of any kind, the gateway binds `0.0.0.0`, and there is no TLS. Two
things stand between that and a prompt, and both are load-bearing:

- **This flag.** Unset, the route is never registered — an absent path cannot
  be probed for and cannot be switched on by a request.
- **The key.** Read from `DERATE_SHELL_KEY`, or generated into
  `/data/shell.key` at 0600 and printed once to the node's console on the boot
  that made it. No route mints it and no route returns it, which is the point:
  `POST /api/enroll` is unauthenticated and its token buys the permanent
  cluster token through `POST /api/nodes/join`, so anything gated on the
  *cluster* token is gated on a secret the network can mint for itself.

Set `DERATE_SHELL_ORIGINS` if you reach the UI from an origin this coordinator
does not serve. A WebSocket handshake is exempt from CORS and is never
preflighted, so that variable is the only thing stopping a page you happen to
visit from opening a socket to your LAN.

The key and everything typed cross the network in clear. One session per node,
and closing the tab kills the session and everything it started.

Host networking ignores published ports, so a port collision here is a
collision with something already on the machine. The container says which
variable moves it.

## `/data`

`node.json`, this machine's own id, and `cluster.json`, the id and token of the
cluster it belongs to. They are deliberately two files, because leaving a
cluster and forgetting the machine are different acts: `install.sh --leave`
drops the second and keeps the first, so a box moved to another cluster arrives
as itself rather than as a stranger, and stops presenting a credential the new
coordinator will reject. The node id used to be re-derived from the hostname on
every boot, which made renaming a machine equivalent to replacing it.

Also the cluster token, node registry, link measurements, deployment records, the
resolved-shape cache, the recipes we synthesize per launch, and a symlink
that puts sparkrun's job metadata inside the volume too -- so a restarted
container can still find, check and stop the workloads this node launched.

Also `telemetry/`. Every node writes `journal.db`, an append-only record of
its own samples, requests, events and logs; the coordinator additionally
keeps `archive.db`, where the whole cluster's history is assembled and rolled
into 1-minute and 1-hour buckets. Sizing, at the defaults: about 250 MB per
node for thirty days of 1 Hz samples, and rollups small enough not to matter.

A worker journals whether or not the coordinator is up. That is the point --
there is no coordinator failover, so a worker that recorded nothing would
lose everything the coordinator was down for. The coordinator collects the
backlog when it returns.

## Replacing the coordinator

There is still no failover and no election: a worker whose coordinator is gone
waits for one rather than promoting itself. But the coordinator is replaceable,
and the cluster token is what makes it so. Start one anywhere with

```bash
docker run ... -e DERATE_TOKEN=<the cluster's token> ghcr.io/pizzaman213/derate/node
```

and every node presents itself again within a minute or so. Nodes keep serving
inference throughout -- sparkrun owns those processes, not this.

How much is automatic depends on what survived with it:

- **The volume survived** (an ordinary restart, or the volume moved to the new
  machine): the roster is on disk, so the nodes come straight back as healthy
  members. Nothing to click. Anything that changed while the coordinator was
  down -- a node that moved to a new address -- is corrected by the same
  re-announcement, which is the case the old coordinator-pull design could not
  recover from at all.
- **Only the token survived**: the new coordinator has never met these machines,
  so they arrive as candidates. One click each, exactly as on a first install.
  The permanent cluster token buys candidacy and never membership; that is the
  "discovery proposes, a human accepts" rule, and replacing a coordinator is not
  a reason to make an exception to it.

Keep the token. It is printed on the coordinator's first start and lives in
`cluster.json` on every machine that has been admitted, so a surviving worker
still has a copy. A coordinator that comes back without it mints a new one, and
no existing node can rejoin at all.

## Health

`/agent/health`, which exists in both roles. Never `/api/cluster`: that is
coordinator-only and would mark every worker unhealthy.

## Building

```bash
docker/build.sh              # verify both architectures
docker/build.sh --load       # this machine's architecture, into the local daemon
docker/build.sh --push       # push the multi-arch manifest
```

Both architectures, always: GB10 is arm64 and the workstation is usually
amd64. An image missing one means the heterogeneous case does not work at
all. Cross-building needs QEMU binfmt handlers on the host; the script says
so if the build fails for that reason.

`SKIP_UI=1` builds without the UI. For iterating on the container itself; a
release build never sets it, because a UI that does not compile should fail
the image.

## Inference backends are not in this image

sparkrun manages those on the host. This container plans, refuses, launches
through sparkrun, and tracks. It never runs a model.

That stays true even for the one backend this repository writes.
`docker/tts.Dockerfile` builds `ghcr.io/pizzaman213/derate/tts`, the image the
`tts` runtime launches -- derate's own text-to-speech server, which exists
because neither vLLM nor SGLang serves `/v1/audio/speech` at all. It is a
*model* image, in the same role as `dgx-vllm-eugr-nightly`: sparkrun pulls it
onto the node and runs the recipe's serve command inside it. Nothing in the
node image imports it and the node image ships no torch.

```bash
docker buildx build -f docker/tts.Dockerfile \
    --platform linux/arm64 \
    -t ghcr.io/pizzaman213/derate/tts:latest --push .
```

arm64 only by default -- a DGX Spark is a GB10 -- and the tag is overridable
per cluster with `DERATE_TTS_IMAGE`, exactly as `DERATE_VLLM_IMAGE` and
`DERATE_SGLANG_IMAGE` are. To serve cloned voices, mount a directory of
`<name>.wav` + `<name>.txt` pairs at `/voices`.

## The vLLM image is ours too, by one layer

`DERATE_VLLM_IMAGE` defaults to `ghcr.io/pizzaman213/derate/vllm-audio`, not
to `ghcr.io/spark-arena/dgx-vllm-eugr-nightly`. `docker/audio.Dockerfile` is
that upstream image and two pip packages -- about 100 MB on top of layers a
node that has served anything already has.

It exists because the upstream image cannot read an audio file: no torchcodec,
no soundfile, no PyAV, no system ffmpeg. That does not stop vLLM serving
`/v1/audio/transcriptions` from it. A Whisper deployment plans, fits,
launches, reaches READY, passes the health identity check and is offered on
the route -- and then answers every upload `Invalid or unsupported audio
file.` Every gate in this project passes it, because none of them is asking
about a decoder.

`soundfile` alone does not fix it, which is worth stating because it looks
like it should. It is what opens the file, but `load_audio_soundfile` hands
every rate conversion to PyAV unconditionally, so a 16 kHz clip decodes and a
44.1 kHz one -- the rate derate's *own* tts runtime writes -- raises
`ImportError: Please install vllm[audio]`. PyAV is what makes the resample
work, and it brings its own ffmpeg, so it covers the container formats
libsndfile will not open as well. The Dockerfile asserts that 44.1 kHz ->
16 kHz decode at build time, through vLLM's own `load_audio`, rather than
asserting that two packages import.

```bash
docker buildx build -f docker/audio.Dockerfile \
    --platform linux/arm64 \
    -t ghcr.io/pizzaman213/derate/vllm-audio:latest --push .
```

**Publish it before this default reaches a node.** Unlike the tts image, this
one is on the path of every vLLM launch, so an unpublished tag is not a
missing feature -- it is a pull failure on every deployment. A text
deployment cannot otherwise tell the two images apart, and
`DERATE_VLLM_IMAGE` points back at the upstream image exactly for anyone who
wants it.
