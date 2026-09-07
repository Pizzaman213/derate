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
and a node whose probe finds no `nvidia-smi` is not an error -- it is a
`NodeProfile` with `device_class=UNKNOWN`, no GPU name, and zeroed memory,
because `registry/probe.py` never raises. That node joins, reports healthy,
and sits in the roster reading

```
worker-docker   0W   0°C   0% GPU utilisation   —% memory used
```

with `ineligible_reason: "device class is not recognized"`. Nothing has
failed, so nothing says so, and the node is simply never planned onto.

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
| `DERATE_INSTALL_SH` | in-image | the script served at `GET /install.sh` |

Host networking ignores published ports, so a port collision here is a
collision with something already on the machine. The container says which
variable moves it.

## `/data`

Cluster token, node registry, link measurements, deployment records, the
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
