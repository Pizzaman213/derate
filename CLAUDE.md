# derate — working notes for Claude Code

Planning and orchestration for DGX Spark clusters: measures the interconnect,
derives the parallelism plan from it, refuses launches that will run out of
memory, and fronts every model behind one OpenAI endpoint.

**Read `00-architecture.md` before changing anything structural.** It holds the
scope, the frozen contracts and the file-ownership map. `agents/<X>-*.md` is the
brief for one workstream; `README.md` is the pitch and the install path. This
file is the part that is only useful while you are editing.

## Commands

```bash
python3 -m pytest -q -m "not slow"     # from the repo root. ~2 min, ~1300 tests.
cd ui && npm run typecheck             # tsc -b
cd ui && npm run build                 # tsc -b && vite build -> ui/dist
node ui/src/state/router.check.mjs         # the URL scheme, both directions
node ui/src/tabs/cluster/layout.check.mjs  # graph layout + layout-preview.svg
node ui/src/tabs/models/rows.check.mjs     # model rows, against the live API
node ui/src/tabs/models/ollamaTarget.check.mjs  # gguf -> hf.co ref, ladder partition
node ui/src/tabs/settings/keyfield.check.mjs  # key-vs-reference, against the Python
node ui/src/sidebar/activity.check.mjs      # download/launch rows: null is not zero
node ui/src/tabs/settings/allowlist.check.mjs # only enabled provider models are servable
node ui/src/tabs/chat/rows.check.mjs        # the chat picker: local half of /v1/models only
node ui/src/tabs/spend/rows.check.mjs      # spend: a $0.00 that is not a zero
```

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
control_plane/node.py      the real entry point: one process, role at runtime
ui/                        the screen. See ui/README.md, which is thorough.
tests/                     pytest. tests/load/ is a harness, not a suite.
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

**Unsloth Studio is AGPL-3.0-only.** Read it as a spec, reimplement, never
vendor.

## The UI's URL scheme

Added 2026-09-07. The path names the screen (`/dashboard`, `/models`,
`/cluster`, `/storage`, `/chat`, `/spend`, `/settings`) plus that screen's own
subject (`/models/<model id>`); the query names what is selected (`?node=`,
`?link=`, `?dep=`, `?open=node:spark-01`, `?ctx=`/`?seq=`). `ui/src/state/
routes.ts` is the scheme -- pure, verified by `router.check.mjs` --
`router.tsx` is the provider, and `selection.tsx` reads and writes it so no
call site knows a URL is involved. Selecting replaces, opening pushes. Details
and the reasoning are in `ui/README.md` under **URLs**.

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
- **Peer Claude sessions edit this checkout at the same time.** Files change and
  are deleted under you; `npm run build` will fail on somebody else's in-flight
  type error in a file you never touched (`npx vite build` bundles without
  typechecking, so you can still tell your breakage from theirs). Before
  declaring UI work done, grep for a marker unique to your edit rather than
  trusting that typecheck passed.
- **Commit with explicit paths only.** A directory-scoped `git add` sweeps a
  peer's uncommitted work into your commit; it has happened more than once.
