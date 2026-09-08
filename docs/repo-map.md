# The repository root

Thirteen files sit above the packages, and they answer four questions: what
this is, how it gets onto a machine, what it is pinned against, and how hard
you can push it. Nothing at this level is imported by the running system except
`install.sh`, which the coordinator serves to the next node.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `README.md` | 246 | the pitch, the screenshots, and the two `curl` lines |
| `CLAUDE.md` | 344 | working notes for whoever is editing: commands, layout, and the rules that are not style |
| `TODO.md` | 80 | the flat checklist of what is not built |
| `LICENSE` | 34 | MIT, plus a note that changes nothing |
| `install.sh` | 629 | the POSIX-sh installer, also served at `GET /install.sh` |
| `Dockerfile` | 140 | the node image. One image, role at runtime |
| `compose.yaml` | 127 | the optional single-node equivalent of the `docker run` |
| `pyproject.toml` | 55 | the package, the console script, pytest and ruff config |
| `requirements.txt` | 23 | the runtime pins the image installs directly |
| `loadtest.py` | 2306 | a standalone load tester against the public API |
| `.dockerignore` | 12 | what never reaches the build context |
| `.gitignore` | 41 | what never reaches a commit |
| `URL-BUNDLE.models-node.txt` | — | a generated planning snapshot of one URL's code path. Deleted from the working tree; recoverable with `git show HEAD:URL-BUNDLE.models-node.txt` |

## `README.md`

The pitch and the install path, written for somebody who has not decided yet.
It states what derate does not do first -- it does not run inference; vLLM,
SGLang and NVIDIA's `sparkrun` do -- and then what is left: measure the
interconnect, resolve the model to a shape, plan parallelism from the two,
refuse launches that will not fit, and front everything behind one endpoint.
**Install** is two `curl` lines, the second of which the UI composes for you
with the coordinator's address and an enrollment token already in it. Six
screenshots from `docs/screenshots/` carry most of the argument. Touch it when
the install line, the port, the route surface or the screens change -- the
banner at the top is generated, so change `docs/screenshots/brand/build.py`'s inputs rather
than the SVG.

## `CLAUDE.md`

**Written for an agent editing this checkout, not for a user.** It is the file
that is only useful while your hands are on the code: the commands that
actually gate things (`pytest -q -m "not slow"`, `npm run typecheck`,
`npm run check`, `npm run screens`), a one-line-per-package layout map, and a
long section titled "Rules that are not style" -- each rule stated with the
failure it prevents. That routers must register above the `StaticFiles` mount
or `/api/settings` quietly answers `index.html`. That `gpu_memory_utilization`
is a claim on the whole device, which is how every launch died at startup while
the fit gate said `fits`. That a bare `npx tsc --noEmit` typechecks nothing here
and exits 0 on any tree. It also records this box's specifics: the live
coordinator on `:8088`, and that peer sessions edit the same checkout. Several
of the documents it points you at are no longer in the tree; where it does,
read the code it names instead. Touch it when a rule is learned, not when a
file moves.

## `TODO.md`

The flat checklist, in five groups: evidence, onboarding and reach, model
surface, cluster, and housekeeping. Each item carries the reason it is not done
rather than just a title -- inbound gateway authentication is "currently none;
biggest gap", manual placement is "the planner has no placement field",
coordinator-down "needs an architecture decision before any implementation".
The first item is the only one under **Evidence**, and it is the honest one:
the README's claim that pipeline parallel beats tensor parallel at ~10 GB/s is
still an assertion, `tests/load/` is the harness that would settle it, and it is
blocked because all three links currently read `measured: false`. Its header
credits two planning documents that are no longer in the checkout. Touch it
when something ships, or when a new gap is found with a reason attached.

## `LICENSE`

MIT, copyright 2026 Connor Secrist, and 20 of its 34 lines are the standard
text. The rest is a section that opens "A note, not a condition" and says so
twice: nothing below it changes the terms. It asks anyone putting derate in
front of real users to open an issue first, and names why -- sizing,
placement, and the gateway's current lack of inbound authentication.
`pyproject.toml` points `license-files` at this file, so it ships in the wheel.
You would touch it only to change the copyright line.

## `install.sh`

**The whole install, and the same 629 lines whichever way you get them.** POSIX
`sh` on purpose -- it runs through `curl | sh` on a box whose shell nobody
chose, so no bashisms and no arrays -- under `set -eu`. Its job is to put one
container on the machine with the right two environment variables and then say
what happened. `--join` and `--token` are what make the difference between a
coordinator and a node; `--dry-run` prints the `docker run` and stops,
`--uninstall` reverses it, `--purge` also takes the volume, and `--leave`
forgets a cluster while keeping the node's own identity. `--install-docker` and
`--install-ollama` are opt-in because the script does not install daemons
nobody asked for. `IMAGE_REPOS` searches `derate/node` as well as the current
GHCR name, so an upgrade still finds a container installed before that move.

The coordinator serves these exact bytes at `GET /install.sh`
(`control_plane/gateway/enroll_api.py`), reading `/opt/derate/install.sh` in
the image and falling back to the repo copy beside the code. **The script never
contains a token** -- it serves the same bytes to everyone, and the token
arrives on the command line the UI composes:
`curl -fsSL {origin}/install.sh | sh -s -- --join {origin} --token {token}`.

## `Dockerfile`

Three stages, one image, role resolved at runtime -- there is no coordinator
build and no worker build. Stage `ui` is `node:22-slim` pinned to
`--platform=$BUILDPLATFORM`, because the bundle is static files identical for
every target arch and the emulated half of a multi-arch build should never run
npm. `ARG SKIP_UI=1` writes a one-line placeholder `index.html` instead of
building; a release build never sets it, because a UI that does not compile
should fail the image loudly. Stage `deps` builds a venv from
`requirements.txt` plus `sparkrun==${SPARKRUN_VERSION}` (0.2.40, pinned to the
version `control_plane/deploy/flags.py` was verified against). The runtime
stage copies that venv, `control_plane/`, `docker/`, `install.sh` and the built
UI, and sets `NVIDIA_DRIVER_CAPABILITIES=utility` -- nvidia-smi and NVML and
nothing more, since this image probes and samples and never runs a model. The
`HEALTHCHECK` hits `/agent/health`, which exists in both roles; checking
`/api/cluster` would mark every worker unhealthy. `ARG DERATE_BUILD` is empty
on a plain `docker build`, and `control_plane/version.py` renders that as an
unidentified build rather than inventing one.

## `compose.yaml`

The single-node equivalent, and its header comment is the point: `docker run`
is the documented path, and "a compose file you must have is a configuration
step, and the pitch is that there are none." Read it as annotated defaults
rather than as a thing to run. `network_mode: host` is marked not optional --
mDNS is multicast and does not cross a bridge, and the container refuses to
start without it. `gpus: all` and `pid: host` are marked not optional *and*
failing quietly: without them a node still joins and still reports healthy, it
is simply never planned onto and nothing says why. Four mounts, of which
`~/.cache/huggingface` is deliberately read-write because reclaiming weights is
the Storage tab's whole purpose. The `environment:` block is the most useful
part of the file -- every `DERATE_*` variable with its default and a paragraph
on the ones with teeth, including why `DERATE_SHELL` is off and why
`DERATE_SHELL_ORIGINS` exists at all.

## `pyproject.toml`

setuptools, `requires-python = ">=3.12"`, MIT with `license-files = ["LICENSE"]`.
Its `dependencies` list duplicates `requirements.txt` on purpose, and the
comment says why: the image pins from `requirements.txt` without building this
package, and a native `pip install .` on a laptop has no Dockerfile to read --
so both lists exist and must be changed together. `[project.scripts]` defines
`derate = "control_plane.node:main"`, the native install path, existing so a
machine with no Docker has something to type; the container entrypoint still
execs `python -m control_plane.node`. `[tool.pytest.ini_options]` sets
`pythonpath = ["."]` -- without it neither `control_plane` nor `tests.fixtures`
imports, and every run fails for reasons unrelated to anyone's code --
`testpaths = ["tests"]`, and the `slow` marker ("minutes, not seconds"). ruff is
line-length 100, target `py312`.

## `requirements.txt`

Twenty-three lines, nine pins, and the comments are the reason to open it.
Three of the nine are already transitive and are named anyway, each with the
`ImportError` it prevents. `websockets==15.0.1` comes with `uvicorn[standard]`,
but `/v1/realtime` imports it directly as a *client* to dial the upstream, so a
future uvicorn that drops the extra would break realtime at connect time.
`anyio==4.14.2` comes with starlette, but `gateway/proxy.py` needs
`CancelScope(shield=True)` so a streamed response still closes its upstream
connection after the client hangs up -- the scope it runs in is already
cancelled by then, and `asyncio.shield` does not cover an anyio cancellation.
`psutil==7.2.2` supplies host facts on the two platforms with no `/proc`; the
Linux readers do not go through it. `sparkrun` is not here -- the Dockerfile
installs it separately, pinned by build arg.

## `loadtest.py`

**A client, not a test.** It points at the public gateway API, asks
`/v1/models` what is being served, and hammers one model or all of them; it
imports nothing from `control_plane`, which is why it also works pointed at a
coordinator on another box. It routes by the modality the gateway reports --
text to `/v1/chat/completions`, embeddings to `/v1/embeddings`, speech to
`/v1/audio/speech`, transcription to `/v1/audio/transcriptions` -- because "all
the models" on a mixed cluster is not one endpoint. Two push modes measure
different things: closed-loop `--concurrency` slots can never overload anything
and answer "how does it behave at N users", while `--hammer` or an explicit
`--rps` issues on a schedule computed from the start of the run and answers
"where does it break", ramping until a bar trips and reporting the last rung
that held. Every request is measured against three clocks -- latency from when
it was *due*, service from when it was sent, and the send delay between them --
because a run that reports flat latency while the queue explodes is one that
started its clock at send time. Paid models are excluded unless
`--include-paid` is passed, read from `/api/providers` rather than from whether
a target is remote. `--list` prints what is served and exits; `--no-tui` runs
headless. `tests/test_loadtest.py` is its gate, pinning the handful of things
that go wrong silently.

**`tests/load/` is a different thing entirely.** That is an in-process harness:
the real `create_app()` with real `GatewaySettings` on a pinned core, driven by
up to eight driver processes against four fake vLLM runtimes on cores of their
own, producing the gateway's derating curve. `loadtest.py` measures a cluster
you are running; `tests/load/` measures the gateway itself under laboratory
conditions.

## `.dockerignore` and `.gitignore`

`.dockerignore` (12 lines) keeps the build context small and honest: VCS
metadata, bytecode, test and type caches, `ui/node_modules`, `ui/dist`, and
`*.md` with `!README.md` restoring the one file worth shipping. Excluding
`ui/dist` matters -- the UI stage runs its own `npm ci && npm run build`, so a
stale local bundle can never be what ends up in the image.

`.gitignore` (41 lines) covers the usual Python and Node artifacts and then two
runtime directories that appear at the root of any checkout you have actually
run. `data/` is grouped under "Secrets and runtime data" alongside `.env`,
`secrets.json`, `*.pem` and `*.key`; `!.env.example` is the one exception.
`logs/` carries its own comment: it holds `node.log`, `proxy.log` and their
rotations, because `paths.py::logs_dir()` puts them next to the code rather
than under the data root -- "where somebody debugging is already standing."
Neither is generated by the build; both appear the first time a node runs here.

## `URL-BUNDLE.models-node.txt`

860 KB and 21,773 lines of concatenated source: every file touched by one URL,
`/models/Qwen/Qwen2.5-0.5B-Instruct?node=connor-pi`, in the order it is
exercised -- the route parse, the screen it names, the machinery underneath,
then the coordinator handler and the JSON on it. It opens with the request
walked line by line through `gateway/app.py`, `state/routes.ts`,
`AppShell.tsx`, `ModelsTab.tsx` and `state/selection.tsx`, and it prunes the
six destinations that URL is not. **It self-describes as a snapshot, not a
source of truth**: the line numbers were grepped at generation time and are
right for the tree as it was and for no other. Edit the real files and
regenerate; never patch this one. It is a planning artifact, safe to delete --
and it has been: the file is no longer in the working tree. It is still in
git, so `git show HEAD:URL-BUNDLE.models-node.txt` recovers all 21,773 lines
of it if the walk is worth reading again. Nothing imports it and nothing
generates it on a schedule, which is why losing it costs nothing.

## The directories

| Directory | Thesis | Its own README |
|---|---|---|
| `control_plane/` | One package, one process, one entry point (`python3 -m control_plane.node`). Twelve subpackages composed at runtime; the ten modules beside them are the only things all twelve may import. | [`../control_plane/README.md`](../control_plane/README.md) |
| `ui/` | The screen the gateway serves itself, at the same origin as the API -- no CORS, no second service. Typecheck plus `*.check.mjs` verifiers are the whole gate. | [`../ui/README.md`](../ui/README.md) |
| `tests/` | pytest, roughly 1300 tests at `-m "not slow"`. Also holds the model sweep and `load/`, which is a harness rather than a suite. | [`../tests/load/README.md`](../tests/load/README.md) |
| `docker/` | One image on every machine, role resolved at runtime: the entrypoint, the preflight, the build script, and the two model-container Dockerfiles. | [`../docker/README.md`](../docker/README.md) |
| `docs/` | Everything the README points at: `screenshots/`, holding the eight captures and `brand/` (generated by `build.py`, never hand-edited). This map lives here too. | [`./index.md`](./index.md) |
| `.github/` | `workflows/publish-image.yml`, which builds and pushes the image every node pulls. A `verify` job gates the push, because a broken commit on the default branch used to become `:latest` on machines nobody is watching. | Covered in [`../docker/README.md`](../docker/README.md) |

`.github/` has no README of its own on purpose: the workflow it holds exists
to gate and publish the image, so it is documented beside the image, in
`docker/README.md`. Splitting it off would put the build in one document and
what the build is for in another.

## Where to start reading

1. **`README.md`.** Ten minutes, and it tells you what the system claims and
   what it explicitly does not do. Everything else is easier to read once you
   know that derate never runs inference itself.
2. **`control_plane/README.md`, then `control_plane/node.py`** (253 lines).
   That file is the composition root and the real entry point -- one process,
   role decided at runtime. Reading it tells you which subpackages exist,
   in what order they come up, and what a coordinator does that a worker does
   not.
3. **`Dockerfile` and `install.sh`, in that order.** How the thing reaches a
   machine, and the three flags (`--network host`, `--gpus all`, `--pid=host`)
   that are not preferences. `docker/README.md` argues each of them.
4. **`CLAUDE.md`, before you change anything.** The commands that actually gate
   the tree and the rules that are not style. Every one of those rules is there
   because something broke; skipping it means breaking it again.
