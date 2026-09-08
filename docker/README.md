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

## Layout

Nothing in this folder builds the node image on its own. `Dockerfile` is at the
repo root; this folder holds the three scripts that run *inside* that image, the
script that builds it, and two Dockerfiles for images that are not it. Line
counts from `wc -l`; `README.md` (this file) and `__pycache__` are omitted.

| File | Lines | What it owns |
|---|---|---|
| `preflight.py` | 143 | the host-networking check, and nothing else |
| `placeholder_app.py` | 123 | the stand-in from before `control_plane.node` existed; effectively dead |
| `build.sh` | 110 | the multi-arch buildx wrapper, and the build id stamped into the image |
| `tts.Dockerfile` | 84 | the model image the `tts` runtime launches |
| `audio.Dockerfile` | 68 | the model image the `vllm` runtime launches, by default |
| `entrypoint.sh` | 54 | refuse a bridge, prepare `/data`, default everything, exec the node app |

## `preflight.py`

The host-networking check. `check(stream=sys.stderr)` returns True when it is
safe to start and prints the reason when it is not; `__main__` exits on it.
`in_container()` runs first — `/.dockerenv`, `/run/.containerenv`, `$container`,
then `docker`/`containerd`/`kubepods`/`podman` in `/proc/1/cgroup` — and outside
a container there is nothing to check. `on_host_network()` then classifies, in
order of confidence: a `/sys/class/net/<if>/device` symlink is a real hardware
interface and therefore the host's namespace; a visible `docker*`, `br-`,
`virbr`, `cni` or `flannel` bridge is the same conclusion, since only the host
sees those; every non-loopback interface being a veth (`ifindex != iflink`) is
exactly what a bridged container gets, and that is the only branch that refuses.
`BRIDGE_MESSAGE` prints the interfaces it saw, the `--network host` run line,
`network_mode: host` for compose, and names `DERATE_ALLOW_BRIDGE`. Five tests in
`tests/unit/test_deploy.py` import this file by path.

## `placeholder_app.py`

**Dead, and worth saying so rather than describing it as live.** It predates
`control_plane.node`, and `entrypoint.sh` prefers that module whenever
`importlib.util.find_spec` finds it — which it does in every shipped image, so
this file has not been the process since. It is left in place as a stdlib-only
server that boots, answers `/agent/health` and `/health` with
`{"placeholder": true, "detail": "control_plane.node not present in this image"}`,
and serves one page saying what is missing.

One line in it is still load-bearing as a record. `AGENT_PORT` defaults to
`"8081"` — matching `registry/config.py::DEFAULT_AGENT_PORT` and the Dockerfile
— and not to `PORT`, which is what it used to do. Defaulting it to `PORT` made
the two equal whenever only `DERATE_PORT` was named, which silently collapsed
the "serve on both ports" branch below into serving on one, with no error either
way. `tests/unit/test_single_source.py::test_no_two_readers_of_a_variable_disagree_about_its_default`
is that fault turned into a gate. `serve()` carries the other one: `EADDRINUSE`
becomes a sentence naming which variable moves the port, because under host
networking a collision is with something the operator already runs.

## `build.sh`

The multi-arch buildx wrapper described above under **Building**. Its knobs are
`IMAGE`, `TAG`, `PLATFORMS`, `BUILDER` (default `derate`), `SPARKRUN_VERSION`
(`0.2.40`, the version `deploy/flags.py` was verified against) and `SKIP_UI`; it
creates the buildx builder when `docker buildx inspect` fails, and with no
`--push` or `--load` it builds both architectures and discards them, which is a
verification run.

**`--load` is not a smaller `--push`.** It rewrites `PLATFORMS` to
`linux/$(docker version -f '{{.Server.Arch}}')`, because buildx cannot load a
multi-arch manifest into the local daemon — so a `--load` build has verified one
architecture, not both.

The other half of the script is the build id. `DERATE_BUILD` is resolved here
from `git rev-parse --short=12 HEAD`, suffixed `+dirty` when `git diff --quiet
HEAD` fails, and passed in as a build arg — resolved in the script rather than
in the Dockerfile because the build context does not carry `.git`. It is the
only source `control_plane/version.py` treats as exact for a shipped container,
and that module exists because of a Raspberry Pi whose roster line read *"device
class is not recognized"* for a fault that was entirely in the software, with
nothing anywhere able to say which build was running. No git available prints
*"no build id available; this image will report an unidentified build"*, and
`version.py::describe_build` renders the empty result as *"an unidentified
build"* rather than as a fabricated id.

On a failed build whose platform list is not the native one, it prints the
`docker run --privileged --rm tonistiigi/binfmt --install all` line: a missing
QEMU binfmt handler surfaces somewhere deep in apt and reads like a broken
Dockerfile. It is not checked up front, because buildx reports only the native
platform on some setups that cross-build perfectly well.

## `tts.Dockerfile`

The model image, covered above under **Inference backends are not in this
image**. What that section does not say is what is in the file.

`ARG BASE` is `ghcr.io/spark-arena/dgx-vllm-eugr-nightly:latest` — not what
`deploy/flags.py` pins for the `vllm` runtime, which has been
`ghcr.io/pizzaman213/derate/vllm-audio:latest` since `audio.Dockerfile` landed,
but the image *that* is one pip layer over. Same base either way, so the pip
layers below are the only new bytes a node that has served anything has to
fetch. `audio.Dockerfile` carries the same `ARG BASE` line; bump the two
Dockerfiles together.

The install is `transformers>=4.57,<6` (pinned to a major because these
checkpoints ship their architecture as remote code, written against one),
`soundfile>=0.12`, `scipy>=1.11`, fastapi and uvicorn, followed by a build-time
assertion that libsndfile in this base can write WAV, MP3, FLAC, OGG and RAW.
scipy is there for one job: 44.1 kHz to 48 kHz for opus, which libsndfile will
not write at the rate these codecs produce. Without it the server still serves
the other four and refuses opus by name.

It copies `control_plane/__init__.py` and `control_plane/runtimes/` and nothing
else, because copying the whole control plane would put the coordinator's code
and its dependency list inside a model container for no reason. `ENV
DERATE_TTS_VOICE_DIR=/voices` is for running the image by hand; a derate launch
overrides it through `flags.py`'s `cache_env` to `RUNTIME_CACHE_DIR/voices`,
beside `hub/` and never inside it. No `ENTRYPOINT`, and `CMD` is `--help`.

## `audio.Dockerfile`

The default vLLM image, argued above under **The vLLM image is ours too, by one
layer**. The file is that reasoning plus one `pip install` — `ARG AV="av>=13"`,
`ARG SOUNDFILE="soundfile>=0.12"` — and one build-time proof.

**The proof runs through vLLM's own entry point, not through an import.** The
failure this image exists to fix was two installed libraries that still could
not answer the call, so asserting that `av` and `soundfile` import would assert
the wrong thing. The `RUN` synthesizes a 44.1 kHz mono WAV in memory, hands it
to `vllm.multimodal.media.audio.load_audio(sr=16000, mono=True)`, and fails the
build unless the result is exactly 16000 samples at 16000 — which is what a
transcription request does with what `POST /v1/audio/speech` produced. It runs
under `PYTHONDONTWRITEBYTECODE=1`, because importing vLLM to run the check
writes 60 MB of `.pyc` across its tree and would bake it into the image; a
build-time assertion must not change what ships. No `ENTRYPOINT` and, unlike
`tts.Dockerfile`, no `CMD` either.
`tests/unit/test_deploy.py::test_the_audio_dockerfile_builds_on_the_image_it_is_a_layer_over`
asserts the `BASE` line, both package args, and `load_audio` with `44100`.

## `entrypoint.sh`

The image's `ENTRYPOINT`: thirty lines that are not a comment or blank, and
their order is the whole design. Export `DERATE_ROLE`, `DERATE_PORT`,
`DERATE_AGENT_PORT` and `DERATE_DATA` with defaults, and mirror `DERATE_DATA` into `DERATE_DATA_DIR`,
which is the name the Python side reads (`paths.py::data_dir()`). Run
`preflight.py` and `exit 1` on refusal — *before* the `mkdir`, so a container
started on a bridge has written nothing into the volume. Create
`$DERATE_DATA/{deployments,recipes,telemetry}`. Symlink
`$DERATE_DATA/sparkrun-cache` to `$HOME/.cache/sparkrun`, so a restarted
container can still find, check and stop the workloads this node launched.
Print `sparkrun --version`, or two warning lines saying that discovery,
planning and the UI still work and a backend launch will be refused.

Then dispatch, in order: arguments given exec verbatim; `DERATE_ENTRYPOINT`
execs `python3 -m` on it; `control_plane.node` execs when importable; otherwise
the placeholder.

## The seam with the root `Dockerfile` and with `deploy/`

The root `Dockerfile` is what consumes this folder. It does `COPY docker/
/opt/derate/docker/`, sets `ENTRYPOINT ["/opt/derate/docker/entrypoint.sh"]`,
and health-checks `/agent/health` on `DERATE_AGENT_PORT` and then `DERATE_PORT`.
Three build args cross from `build.sh`: `SKIP_UI`, `SPARKRUN_VERSION` and
`DERATE_BUILD`. The UI stage is `FROM --platform=$BUILDPLATFORM node:22-slim`,
so npm runs once, natively, and only apt and pip go through QEMU;
`SKIP_UI=1` replaces the built UI with a two-line `index.html` reading "UI not
built into this image", which is what makes it obvious in a browser rather than
in a build log.

The two model images are consumed by name, from
`control_plane/deploy/flags.py::RUNTIMES`:

```python
RUNTIMES["vllm"].default_image      # ghcr.io/pizzaman213/derate/vllm-audio:latest
RUNTIMES["vllm"].default_image_env  # DERATE_VLLM_IMAGE
RUNTIMES["tts"].default_image       # ghcr.io/pizzaman213/derate/tts:latest
RUNTIMES["tts"].default_image_env   # DERATE_TTS_IMAGE
```

`recipes.container_image(spec, env)` is what resolves the pair, so the env
variable always wins and the default is only a default.
`control_plane/runtimes/` is the code that runs inside the tts image, and
`control_plane/version.py` is the only reader of `DERATE_BUILD`.

## The tts image is not in the registry

Say it plainly, because five launches died on `manifest unknown` before anyone
noticed. `deploy/flags.py` defaults the `tts` runtime to
`ghcr.io/pizzaman213/derate/tts:latest`, and that name does not resolve at ghcr.
Nothing pushes it: the publish workflow below builds the node image only, and
the `docker buildx build -f docker/tts.Dockerfile ... --push` line above is run
by hand and has not been. So on each Spark the image is built from this
Dockerfile against that node's own local vLLM base — only the pip layers are new
bytes — and then **tagged with the ghcr name locally**, which is the same trick
the `vllm-audio` default relies on. `DERATE_TTS_IMAGE` also works and is one
restart away from being lost; it was lost that way once, and the next launch
went straight back to `manifest unknown`. Prefer the local tag.

`tests/unit/test_gateway.py::_IMAGE_MISSING` is the same fact written down as a skip:
`manifest unknown`, `image distribution failed`, `failed to ensure local image`
and `pull access denied` turn a live launch test into a skip rather than a
failure, on the reasoning that this is the one launch failure that is a fact
about the machines rather than about the code. Publishing both images is the
actual fix and has not been done.

## `.github/workflows/publish-image.yml`

The only thing that publishes anything here, and nothing else in the repo
documents it. On a push to `main`, a `v*` tag, or `workflow_dispatch`, two jobs
run: `verify` installs `requirements.txt` and runs `python -m pytest -q -m "not
slow"` from the repo root, then `npm ci`, `npm run typecheck`, `npm run build`
and `npm run check` inside `ui/`; `publish` `needs` it and buildx-pushes
`linux/amd64,linux/arm64` to `ghcr.io/pizzaman213/derate/node`, tagged `latest`
on the default branch plus the branch, the tag and the long SHA.

**The verify job is the whole point of the file.** Nothing used to run between
checkout and push, so a broken commit on the default branch became the `:latest`
image every node pulls, on machines nobody is watching, and the first sign of it
was an install failing in somebody else's house. `install.sh` defaults to
`:latest` from this registry, which puts a stranger's `curl ... | sh` downstream
of this workflow.

Two mechanical details. `GITHUB_TOKEN` carries `packages: write` through the
job's own permissions block, so this needs no PAT and no repository secret. It
cannot set package *visibility*: after the first successful run somebody has to
flip the package to public once, or an anonymous `docker pull` returns a 401
that reads exactly like the "repository does not exist" failure this workflow
was meant to end. The verifier step runs `npm run check` and lets it report its
own skips — a runner has no coordinator, no captured fixtures and no browser, so
those verifiers are named in the output rather than counted as passes.

## Things that look like details and are not

**A case the preflight cannot classify starts the node.** No hardware
interface, no host bridge, and not every interface a veth returns
`(True, "could not classify ...; assuming host networking")`. The refusal is
reserved for the one shape that is positively identifiable as a bridge, because
blocking on an unrecognised network setup makes a machine uninstallable for a
fault nobody has diagnosed. The reason string is printed either way.

**`DERATE_ALLOW_BRIDGE` is read after detection, never instead of it.** The
warning it produces still carries the classification — *"bridge networking
detected (every interface is a veth pair: eth0)"* — so the unsupported mode is
distinguishable from the working one in the log. An override that skipped the
check would make them read identically.

**`SKIP_UI=1` is for iterating on the container and never for a release.** Both
`build.sh` and the Dockerfile say so at the point they honour it: a UI that does
not compile should fail the image, loudly.

**Both model images are pinned by tag, and by tag only.** `docker/build.sh`
does not know about them, the workflow does not build them, and there is no
`:sha` variant of either. A `latest` that moves under a running cluster is a
different image on the next launch and nothing records which one served.

## Failure behaviour

- **Bridge networking.** `preflight.py` prints `BRIDGE_MESSAGE` with the
  interfaces it saw and exits 1; `entrypoint.sh` exits with it, before the
  volume has been touched.
- **No network interfaces visible at all.** `"no network interfaces are visible
  at all"` — treated as not-host, so it refuses.
- **`DERATE_ALLOW_BRIDGE=1`, `true` or `yes`.** A warning naming the reason, and
  the node starts. Discovery does not work and the message says so.
- **`sparkrun` not on `PATH`.** Two warning lines and a normal start. Discovery,
  the roster, link measurement, planning, the fit verdict and the UI all work;
  a launch is refused.
- **The port is already bound.** On the placeholder path, `EADDRINUSE` prints
  which of `DERATE_PORT` / `DERATE_AGENT_PORT` moves it and exits 1. Any other
  `OSError` is re-raised unchanged.
- **`docker buildx` is missing.** `build.sh` names the single-architecture
  `docker build -t ${IMAGE}:${TAG} .` fallback and exits 1.
- **A cross-architecture build fails.** The QEMU binfmt hint is printed, but
  only when `PLATFORMS` is not this machine's architecture, and the script exits
  with the build's own status.
- **No git, or no `.git` in the context.** `DERATE_BUILD` is empty, the script
  says the image will report an unidentified build, and
  `version.py::describe_build` renders *"an unidentified build"* rather than
  guessing.
- **A build-time assertion fails.** No image is produced — the libsndfile format
  check in `tts.Dockerfile`, the 44.1 kHz to 16 kHz decode in
  `audio.Dockerfile`. Both are `RUN` steps, so the failure is the build's.

## Deliberately not built

**No `ENTRYPOINT` in either model image.** sparkrun runs the recipe's `command`
verbatim, rendered by `deploy/flags.py` with every knob the planner and the fit
gate decided on. An entrypoint here would be a second opinion about how to start
a server this file did not write. `audio.Dockerfile` goes further and declines a
`CMD` as well, on the grounds that the base image already has whatever it has.

**No amd64 for the model images.** arm64 only, because a DGX Spark is a GB10 —
the exact opposite of the rule for the node image, where a missing architecture
means the heterogeneous case does not work at all. Building amd64 would only
serve somebody serving speech off a workstation.

**No second vLLM image chosen per modality.** `vllm-audio` is the default for
every vLLM launch rather than an image picked when the model is audio: two
images for one runtime is two things to keep in step with the base bump, and the
delta is a few megabytes over layers every node has already pulled. A text
deployment cannot tell the two apart.

**No coordinator image and no worker image.** The Dockerfile has one
`ENTRYPOINT` and no `--target`, and
`tests/unit/test_deploy.py::test_dockerfile_ships_one_image_with_the_required_shape`
asserts both, along with the `/agent/health` health check and the absence of
`/api/cluster` from it.
