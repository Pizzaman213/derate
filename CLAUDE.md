# derate — working notes for Claude Code

Planning and orchestration for DGX Spark clusters: measures the interconnect,
derives the parallelism plan from it, refuses launches that will run out of
memory, and fronts every model behind one OpenAI endpoint.

**To look a contract up, read `CONTRACTS.md`.** It is generated from the code --
every type, enum, constant, derived fact, route and environment variable -- and
a test fails when it goes stale, so it cannot be wrong in a way the code is not.

**Read `00-architecture.md` before changing anything structural.** It holds the
scope, the reasoning and the file-ownership map. It is a journal: sections 1-8
are never edited and roughly twenty-five dated appendices amend them, so reading
it top-down gives you superseded answers. The index at the top lists every
appendix that reverses something above it. `agents/<X>-*.md` is the brief for one
workstream and several are now stale against those appendices; `README.md` is the
pitch and the install path. This file is the part that is only useful while you
are editing.

## Commands

```bash
python3 -m pytest -q -m "not slow"     # from the repo root. ~2 min, ~1300 tests.
cd ui && npm run typecheck             # tsc -b
cd ui && npm run build                 # tsc -b && vite build -> ui/dist
cd ui && npm run check                 # the whole UI suite. See below.
cd ui && npm run check -- --strict     # ...and a skip counts as a failure
cd ui && npm run screens               # capture ui/screens/<dest>.png, no assertions
python3 -m tests.model_sweep           # resolve every config in tests/resolver_data/
python3 -m tests.model_sweep --live    # ...or every local model the coordinator knows
python3 -m tests.model_sweep --arch    # VLLM_ARCHITECTURES against the image's registry
```

`tests/model_sweep.py` is the model tester and `tests/test_model_corpus.py`
gates the suite on it.

The corpus mode is hermetic and compares against
`tests/resolver_data/EXPECTED.json`. Drift fails in **both** directions but is
reported as two different events: a regression is a bug, an improvement is a
diff to review and `--regenerate`. It then runs the same corpus a second time
with the image's registry recorded, because a shape must not depend on which
runtime image is installed -- the mapper never asks a runtime anything, and
that pass is where it is proved rather than assumed. No image here prints
`unchecked`, never agreement.

`--arch` is the one that catches the failure this list keeps having: it diffs
`VLLM_ARCHITECTURES` against what the pinned image's registry actually holds.
A name the image loads and the table refuses is a 400 on a servable model
(`Gemma4ForConditionalGeneration`, for weeks); a name the table claims and the
image cannot load clears every gate and dies at load
(`MllamaForConditionalGeneration`). Both fail.

`--live` sweeps the coordinator's catalogue. Only a shape this build cannot
read is flagged. Provider-offered models are their own column rather than a
failure -- they are served over somebody's API and have no HuggingFace repo, so
"no local shape" is correct for them, and they are 427 of this box's 516 rows.
Gated and missing repos are counted apart too, because neither is a bug here.

The image-dependent tests are marked `slow`, so `-m "not slow"` deselects them
rather than starting a 24 GB container inside the suite. Deselected is not
skipped: the run says it did not check them.

When a contract, a route or an environment variable moves, regenerate:

```bash
python3 -m control_plane.contracts.manifest --write   # types, enums, constants, derived facts, env
python3 -m control_plane.contracts.routes --write     # every route both apps answer
python3 -m control_plane.contracts.document --write   # CONTRACTS.md, from the two above
```

`tests/test_contracts_manifest.py` fails when any of the three is stale, and
`tests/test_single_source.py` fails when a copy of a fact stops matching the one
`control_plane/contracts/derived.py` names as canonical. Add a row there rather
than adding an assertion by hand.

`pyproject.toml` sets `pythonpath = ["."]`, so pytest must run from the root or
nothing imports.

**Bare `npx tsc --noEmit` typechecks nothing here** and exits 0 on any tree:
`ui/tsconfig.json` is `{"files": [], "references": [...]}`, so it resolves no
sources. The real gates are `npm run typecheck` (`tsc -b`) and `npx tsc -b
--force`; `npx vite build` bundles *without* typechecking, which is how you tell
your own breakage from a peer's in a shared checkout.

There is no UI test runner: typecheck plus the `*.check.mjs` verifiers are the
whole gate, and each one exists because a specific class of bug is invisible to
types. Add to them rather than trusting a green `tsc`.

`ui/check.mjs` (`npm run check`) is how they are run. It **discovers** them --
never a list, because a list stops covering a verifier the moment somebody adds
or renames one -- and it reads what each needs off a `// requires:` line at the
top of the file: `coordinator` (a gateway on `$DERATE_CHECK_ORIGIN`, default
:8088), `python`, `browser`, `fixtures $NAME`, or no line at all for the ones
that need nothing but the checkout. Absent means hermetic, which is the safe
default: a verifier that forgets to declare something fails loudly on a machine
without it rather than being skipped everywhere forever.

**A skip is never a pass.** Unmet requirements put a verifier in its own column
with the reason printed, never folded into the passes; `--strict` turns each one
into a failure, which is the mode for this box, where the coordinator *is* up.
Two verifiers used to `process.exit(0)` when they could not reach a coordinator,
which made "checked nothing" and "checked everything" the same exit code.

Writing a new one starts at the first assertion -- `src/check/harness.mjs` has
`load()` (esbuild-bundles a TS module so node can import it) and `report()`
(PASS/FAIL, the counter, the exit code). Each verifier is still a program you
can run on its own, and that is still the inner loop.

**`npm run screens` is how you look at the result.** `src/shell/screens.check.mjs`
drives the Chromium already in the Playwright cache -- nothing is downloaded --
over every destination in `state/routes.ts` and writes `ui/screens/<dest>.png`.
Under `npm run check` it also asserts: no console error, no uncaught rejection,
nothing non-2xx from the coordinator, a `#root` with height, and, against the
server's own `Redactor` primed with the real stored values, that no key is on
screen. Open the PNGs -- that is the only thing here that sees what shipped
rather than inferring it from source. Off-site failures are recorded in
`screens/console.txt` and deliberately not gated: the publisher avatars come
from huggingface.co and 429 in bulk.

## Layout

```
control_plane/contracts/   frozen. Change these in one place, announced, never
                           to unblock yourself -- the README calls this out as
                           the project's one failure mode.
control_plane/registry/    node agent, discovery, join, health, telemetry
control_plane/links/       measured interconnect bandwidth
control_plane/resolver/    HuggingFace id -> ModelShape, quantization table
control_plane/fit/         the blocking out-of-memory gate
control_plane/planner/     TP/PP/EP from measured facts
control_plane/deploy/      sparkrun adapter, lifecycle
control_plane/gateway/     the one HTTP surface: /v1, /api, and the built UI
control_plane/providers/   remote upstreams as route targets
control_plane/runtimes/    the inference servers derate ships itself. Runs
                           INSIDE a model container (docker/tts.Dockerfile),
                           never in the node image: torch and transformers are
                           its dependencies and neither is in requirements.txt.
                           Nothing else in control_plane/ imports it.
control_plane/redaction.py the key scrubber, standard library only. It lives
                           outside providers/ on purpose: every node needs it
                           and the worker path may not import that package.
                           providers/secrets.py re-exports all of it.
control_plane/logfiles.py  node.log and proxy.log, in one folder
control_plane/node.py      the real entry point: one process, role at runtime
ui/                        the screen. See ui/README.md, which is thorough.
tests/                     pytest. tests/load/ is a harness, not a suite.
docs/brand/                the README's banner and bare lockup. The mark is a
                           COPY of the one Header.tsx draws and the colours are
                           copies of tokens.css, so change either and rerun
                           `python3 docs/brand/build.py`, which derives the
                           lockup's proportions from the header's own CSS and
                           outlines the wordmark so no font has to be installed.
```

## Rules that are not style

**Routers register above the `StaticFiles` mount.** A Starlette mount at `"/"`
catches every path not matched by an *earlier* route, so an `include_router`
below it never runs and `/api/settings` quietly answers `index.html`. Same rule
for `/install.sh`, which `curl | sh` would otherwise pipe an HTML document.

**The UI's deep paths are answered by `_UIStatics.get_response`.** Screens live
in the path (`/cluster`, `/models/meta-llama/Llama-3.1-8B`) and nothing exists
on disk under them. The fallback deliberately does not apply to `/api`, `/v1`
or a missing hashed asset -- an asset 404 must stay a 404 or a broken deploy
renders as a blank page with nothing in the network log to explain it.

**Planner and fit strings are the product.** They render through `Verbatim`,
exactly as received: never truncated, re-cased or summarised. A refusal names
what to change, and rewriting it destroys the thing that made it useful.

**No API key is ever rendered.** `ui/src/api/redact.ts` scrubs responses on the
way in. Do not add an inverse.

**Live memory is an optional kwarg that degrades.** The fit gate prefers what a
node can actually hand out now and falls back to the static ceiling when there
is no reading. It never refuses for want of a live number.

**The launched container's only writable mount is the HuggingFace cache.**
`docker inspect` on a running sparkrun container: `HOME=/tmp`, user
`1000:1000`, one bind mount at `/cache/huggingface`, and `--rm`. Anything a
runtime should keep between launches has to be pointed in there --
`RuntimeSpec.cache_env` -> the recipe's `env:` block, which is the only channel
that exists, because the recipe format has no `volumes:` key. This is why vLLM
recompiled from cold on every launch until `VLLM_CACHE_ROOT` was set. Put it
beside `hub/`, never inside: `modelcache.py` measures and deletes
`hub/models--*` and would count compiled kernels as weights.

**The four slow things are not slow in the order you would guess.** Measured on
a 0.5B launch with the image and weights already local: CUDA graph capture 40s+,
python/vLLM import (twice -- APIServer, then the EngineCore fork) ~26s, sparkrun
prep and `docker run` ~19s, cold `torch.compile` 7.5s, **weight load 5.6s**. The
shard-counting progress bar covers the cheapest step. `VLLM_CACHE_ROOT` buys back
the 7.5s and not the 40s -- graph capture is not cached by it. Do not try to seed
the compile cache from `~/.cache/vllm` or `~/.cache/huggingface/vllm-cache`: the
key hashes the vLLM/torch build, the current nightly's key is in neither, and it
is 3.6 GB that will never be read. The only lever on graph capture is
`enforce_eager` or a trimmed `cudagraph_capture_sizes`, which trade decode
throughput for startup and so would have to be an option, not a default.

**A launch is four slow things, and it says which one it is on.**
`deploy/progress.py` classifies it from literal markers sparkrun and the
runtime print, and shows their line verbatim. Two rules there are load-bearing:
an unrecognised line changes nothing (the newest line is not necessarily the
current activity), and the only `fraction` that exists comes from the
checkpoint loader counting its own shards. When a marker stops matching after a
version bump, fix the marker -- do not fall back to inferring the phase.

**`gpu_memory_utilization` is a claim on the whole device, not a limit.** vLLM
refuses to start unless that share of the GPU is *free*, then fills it with KV
cache. A constant therefore asks for the same slice of the machine for a 0.5B
model as for a 120B one, and fails outright on any node with neighbours --
which is how every launch here died at startup while the fit gate's own record
said `fits`, basis `live`, 1.8 GiB into 24.0 GiB. `deploy/utilization.py`
derives it from `fit.breakdown.total`, capped by what is free and by
`DEFAULT_GUARDRAIL`. The denominator is the live sample, because that is what
the runtime divides by.

**A dead engine does not kill its container.** A solo launch execs the serve
command inside a container that sleeps forever, so `sparkrun cluster check-job`
still says "running" after the engine has exited -- which is how a launch that
died in thirty seconds was watched for the full 1800s. The runtime's own
`EngineCore failed to start.` is what catches it (`progress.py::_RUNTIME_FATAL`),
and `sparkrun logs` is the only way to see it: solo output goes to
`/tmp/sparkrun_serve.log` inside the container, not to `docker logs`. That
command *follows* -- it is `tail -f` and never returns -- so it is subscribed
to, never polled.

**`sparkrun` and "DGX Spark" are external names.** The project renamed
`sparkplane` -> `derate` on 2026-09-07 as a hard cut, but `sparkrun` is NVIDIA's
launcher binary (parsed with literal regexes) and "Spark" is the hardware.
Grep for `sparkplane`, never for bare `spark`.

**GB10 unified memory: `nvidia-smi` reports aggregate memory as N/A.** Only
per-process accounting works, which is why the container runs `--pid=host`. The
static ceiling overstates what is available by roughly 10x because the OS shares
the model's pool -- never present it as headroom.

**Platform questions are answered with evidence, not `platform.system()`.**
The probe runs `nvidia-smi` and reads what comes back; `detect_bridge_networking`
looks at the interfaces; the host readers fall through when `/proc/meminfo` is
not there, not when the OS has a particular name. A name test is wrong inside a
Linux container on a Mac, under WSL, and on a hardened `/proc` — all cases where
the Linux path still works and should still win. The two exceptions are
deliberate and commented: `fsutil` tests `hasattr(os, "fchmod")`, and `procs.py`
refuses the kill verb off POSIX because `os.kill` there terminates what it is
asked to probe.

**`/data` is not a default any more.** `control_plane/paths.py::data_dir()` is
the single resolver: `DERATE_DATA_DIR`, else `/data` when writable, else the
platform's application-state directory. Eleven modules used to re-type the
fallback, and off Linux every one of them failed into a `log.warning` — which
added up to a coordinator that regenerated its cluster token on every restart.

**The logs are one folder, and `proxy.log` is not a grep of `node.log`.**
`control_plane/logfiles.py` writes both into `paths.py::logs_dir()` --
`DERATE_LOG_DIR`, else `<project root>/logs`, next to the code rather than
under `data_dir()` like everything else in `paths.py`. In the image that root
is `/opt/derate`, which is a layer and *not* the `derate:/data` volume, so a
deployment that wants logs to survive `docker rm` sets
`DERATE_LOG_DIR=/data/logs`. The narrow file is filtered at the *handler* because a
traceback's continuation lines carry no logger name: `grep gateway.proxy
node.log` keeps `forward failed` and drops the exception under it. Add a logger
to `PROXY_LOGGERS`, never a second file. An unwritable folder is a warning and
no handlers -- this never fails a startup -- and the redacting filter is on the
handler, not on a logger, because the loggers it covers are `gateway.*` and a
`logging.Filter` on a logger is never consulted for another one's records.

**Unsloth Studio is AGPL-3.0-only.** Read it as a spec, reimplement, never
vendor.

**`tts` is a runtime like the other two, and its architecture list is a claim
about `control_plane/runtimes/tts.py`.** A name belongs in
`TTS_ARCHITECTURES` once somebody has run that checkpoint through that server,
never because the model card says TTS -- the server calls exactly
`processor(...)`, `generate() -> codes`, `decode_audio(codes)`, and a
checkpoint whose remote code answers something else is a launch that clears
every gate and dies at load. Its recipe says `runtime: vllm` on purpose:
sparkrun's runtime field picks its orchestration plugin, and every plugin runs
an explicit `command:` verbatim.

## The UI's URL scheme

Added 2026-09-07. The path names the screen (`/dashboard`, `/models`,
`/cluster`, `/chat`, `/spend`, `/settings`, plus `/setup`, which is
a real destination and off the nav) and that screen's own subject
(`/models/<model id>`); the query names what is selected (`?node=`, `?link=`,
`?dep=`, `?open=node:spark-01`, `?ctx=`/`?seq=`). `ui/src/state/
routes.ts` is the scheme -- pure, verified by `router.check.mjs` --
`router.tsx` is the provider, and `selection.tsx` reads and writes it so no
call site knows a URL is involved. Selecting replaces, opening pushes. Details
and the reasoning are in `ui/README.md` under **URLs**.

This list said `/storage` and did not say `/setup`. Storage has never been a
destination -- `tabs/storage/*` is four cards composed inside `SettingsTab`
-- and a `Dest` that is not in `SEGMENT` parses as the dashboard and renders
`/undefined`, silently, which is why `router.check.mjs` now walks the whole
`Dest` union rather than a hand-kept list of paths.

`/speech` existed briefly as the only way in the product to send
`POST /v1/audio/speech`, on the argument that the chat picker filtered audio
models out and so a TTS deployment was visible everywhere and reachable from
nowhere. That filter was removed instead: the chat picker now lists every
modality, `ChatTab` posts to the endpoint the picked row advertises (text and
embedding to `/v1/chat/completions`, a speech row to `/v1/audio/speech`), and
the composer grows a voice/format pair or a file chooser to match. `/speech`
was retired on 2026-09-08 once it had nothing left that `/chat` did not
already do.

## This box

- The live coordinator is on **:8088** (`DERATE_UI_DIR=/home/connor/derate/ui/dist`,
  data in `/tmp/derate-live`). `:8080` is somebody else's. Boot test instances
  on **18xxx**.
- `npx vite build` is live on :8088 with no restart, *if* `DERATE_UI_DIR` still
  points at `ui/dist` -- read it off `/proc/<pid>/environ` and confirm with
  `curl -s localhost:8088/ | grep -o 'assets/index-[^"]*\.js'`. Equal hashes is
  the only proof.
- Server-side changes do need a restart, and the process **does not exit on
  SIGTERM**: `kill PID`, wait for the port to free, then `kill -9`. Never
  `pkill -f control_plane.node` -- the pattern matches the agent's own shell.
- **The tts image is not published, and is faked by a local tag.**
  `deploy/flags.py` defaults the `tts` runtime to
  `ghcr.io/pizzaman213/derate/tts:latest`, which does not exist in the
  registry -- five launches died on `manifest unknown` before anyone noticed.
  It is built from `docker/tts.Dockerfile` on each Spark against that node's
  local `sparkrun-eugr-vllm:latest` base (only the pip layers are new bytes)
  and then **tagged with the ghcr name locally**, which is the same trick the
  vllm-audio default relies on. `DERATE_TTS_IMAGE` also works and is one
  restart away from being lost -- it was, at 22:58 on 2026-09-07, and the next
  launch went straight back to `manifest unknown`. Prefer the tag. Publishing
  the real image is the actual fix and has not been done.
- **Peer Claude sessions edit this checkout at the same time.** Files change and
  are deleted under you; `npm run build` will fail on somebody else's in-flight
  type error in a file you never touched (`npx vite build` bundles without
  typechecking, so you can still tell your breakage from theirs). Before
  declaring UI work done, grep for a marker unique to your edit rather than
  trusting that typecheck passed.
- **Commit with explicit paths only.** A directory-scoped `git add` sweeps a
  peer's uncommitted work into your commit; it has happened more than once.




Dont commit to git or ask unless I ask`s