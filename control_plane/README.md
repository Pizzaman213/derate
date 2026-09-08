# control_plane

One Python package, one process, one entry point: `python3 -m control_plane.node`.
The twelve subpackages under this directory are what that process composes; the
ten modules sitting beside them are the leaves any of those twelve may import,
and which import none of them back. That constraint is the design rather than an
accident of layout -- a module at this level has to be importable by a worker
*and* by a coordinator, which is why the key scrubber lives here instead of
inside `providers/`, where it was written.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `node.py` | 253 | the composition root and the real entry point: one image, one process, role decided at runtime |
| `logfiles.py` | 282 | `node.log` and `proxy.log`, in one folder, redacted at the handler |
| `envspec.py` | 227 | every `DERATE_*` variable this project reads, its default, and the file that owns the read |
| `paths.py` | 161 | the single resolver for the data root and for the log folder |
| `redaction.py` | 160 | the key scrubber. Standard library only, so every node can import it |
| `version.py` | 145 | which build this is -- best effort, never a guess |
| `fsutil.py` | 113 | "restrict this file to this account", on a filesystem that may not do modes |
| `humanize.py` | 47 | one binary byte formatter, because there were three |
| `procmatch.py` | 39 | whether a running command line belongs to a deployment |
| `__init__.py` | 1 | the tagline, and nothing else |

## `node.py`

The process `docker/entrypoint.sh` execs into on every container, worker and
coordinator alike. It does not decide the role -- `registry.startup.start_node`
does, and hands back a `NodeRuntime`. Every node then serves
`runtime.agent_app()` on `config.agent_port`, and that is the entire worker.

**The worker path must stay light, and this file is what keeps it light.**
Nothing from `gateway`, `deploy`, `providers`, `fit`, `planner`, `links` or
`resolver` is imported at module scope; every one of those imports lives inside
`build_gateway_deps` or `_serve_coordinator`, so a worker process never pulls
httpx, uvicorn's gateway stack or a fit calculator into memory.
`tests/unit/test_node.py::test_worker_never_builds_gateway_deps` is the gate on that.

`_install_shutdown` is split out of `_run_with_signals` so the shutdown
behaviour can be exercised with plain fake objects, without a real OS signal or
a real socket. `main()` calls `logfiles.install()` *before* `asyncio.run`, so a
node that fails to start is in the file rather than only on a stderr that
systemd or `docker run -d` swallowed.

### What `build_gateway_deps` refuses to leave to a default

Four of its arguments exist because the default was observably wrong:

- **`strict=True`.** `GatewayDeps.__post_init__` raises when a port is missing
  rather than backfilling `gateway/stubs.py` fixtures -- a stub surface wearing
  a real gateway's clothes. It must not be reachable from a real composition
  root.
- **`cluster_id=runtime.identity.cluster_id`.** `GatewaySettings.cluster_id`
  defaults to the day-0 fixture `"c-local"`, so leaving it would have
  `/api/cluster` and `/api/topology` reporting a fixture id from a real cluster.
- **`coordinator_node_id=runtime.profile.node_id`.** This process knows which
  node is the coordinator, because it is one. Left `None`, the gateway lifespan
  has to work it out; `gateway/app.py::_resolve_coordinator_node` records what
  guessing produced -- a persisted roster that led with a worker named that
  worker as the coordinator.
- **`telemetry=runtime.telemetry`** on `create_app`. Given no bundle,
  `create_app` builds its own from the environment, which on a composed
  coordinator means two Journal writer threads on one `journal.db`, two Archives
  on one `archive.db`, and two Collectors racing cursors over the same journal.

`Planner()` is deliberately constructed with no argument, and the argument that
looks obvious is the one that breaks it. The planner's `fit` parameter is its own
`FitHelpers` protocol -- `planner/fit_bridge.py`, two methods,
`min_nodes_required` and `kv_bytes_per_token` -- and `FitCalculator` implements
neither of them; its surface is `check` and `max_context`.
`Planner(fit=FitCalculator())` therefore constructs, starts, serves, and raises
`AttributeError` inside `Planner._facts` the first time anybody asks for a plan.
Left empty, the parameter resolves through `default_fit_helpers()`, which returns
the `control_plane.fit` module when it exports both helpers and otherwise logs a
warning naming the substitution before handing back the conservative fallback.
`ModelResolver` is handed `runtime_images` -- `container_image(spec)` for every
runtime in `deploy.flags.RUNTIMES` that `resolver.imageprobe.SCRIPTS` knows how
to interrogate -- because the composition root is the only thing that knows both
which images this build launches and which component needs to ask them what they
can load.

## `logfiles.py`

Two rotating files in one folder, attached to the root logger. `node.log` is
every record at `DERATE_LOG_LEVEL`; `proxy.log` is the same stream narrowed to
the nine prefixes in `PROXY_LOGGERS` -- `gateway.proxy`, `gateway.openai`,
`gateway.router`, `gateway.admission`, `gateway.parking`, `gateway.breaker`,
`gateway.budget`, `gateway.errors` and all of `control_plane.providers`.

**The narrow file is not a grep of the wide one.** A traceback's continuation
lines carry no logger name, so `grep gateway.proxy node.log` keeps `forward
failed` and drops the exception underneath it -- it splits exactly the records
worth reading. Filtering at the handler keeps whole records. Before this module
there were no files at all: `basicConfig` put records on stderr and
`telemetry/loghandler.py` put a redacted copy in SQLite, so the only readable
plain text was whatever the starting shell happened to redirect, which was a
different answer on every machine and nothing at all inside the container.

`install()` is idempotent, returns the folder or `None`, and never raises: an
unwritable folder is a `log.warning` and no handlers. Handlers are opened with
`delay=False` on purpose -- deferring the open moves the same failure into
`emit()`, which then prints a traceback to stderr for every line logged
thereafter. `LOG_MAX_BYTES` is 32 MiB and `LOG_BACKUPS` is 3, bounding the
folder at 256 MiB across both files, on a volume that also holds the model
cache. `DERATE_LOG_FILES=0` switches files off entirely; stderr and the journal
are unaffected either way. `paths()` reports where the two files are whether or
not anything is installed.

## `envspec.py`

The single declaration of all 63 `DERATE_*` variables, plus two `DYNAMIC`
families whose full name is only known at runtime (`DERATE_<PROVIDER>_API_KEY`
and `DERATE_DATAPLANE_<node id>`). Each `EnvVar` carries a `default` spelled *as
the reading site spells it* and an `owner` -- the file to open when the default
is wrong.

**Environment variables are the one contract with no type and no import graph.**
Nothing fails when a shell script exports one spelling and a Python module reads
another; the variable is simply absent and the default wins in silence. The pair
that proves it: `docker/entrypoint.sh` exports `DERATE_DATA` and every Python
component reads `DERATE_DATA_DIR`. Both default to `/data`, so the split is
invisible until somebody overrides one and the other keeps pointing at the old
estate. The entrypoint bridges them by hand
(`DERATE_DATA_DIR="${DERATE_DATA_DIR:-$DERATE_DATA}"`), which works, and is
exactly the kind of bridge nobody remembers on the fifth variable.

`is_declared(name)` is the predicate `tests/unit/test_single_source.py` greps the
whole tree with. `None` as a default means absence is itself the answer -- an
absent `DERATE_TOKEN` makes the coordinator generate one, which is not the same
as an empty string.

## `paths.py`

The single resolver for where persistent state goes: `DERATE_DATA_DIR`, else
`/data` when it is real and writable, else this platform's application-state
directory. `data_dir()` is the only correct way to ask, and `data_path(*parts)`
is the one-liner form.

Eleven modules used to re-type `os.environ.get("DERATE_DATA_DIR", "/data")`.
That is right in the container and quietly wrong everywhere else, because every
writer under `/data` -- `identity.py`, `roster.py`, `enrollment.py`,
`shell_config.py` -- swallows a failed write with a `log.warning` on purpose, so
a node can keep running on a read-only volume. Stacked, the effect was a
coordinator on a laptop that regenerated its cluster token on every start and
stopped recognising its own workers, having reported nothing worse than a
warning. So the fallback has to be somewhere writable *before* the write is
attempted: `_usable()` tests `is_dir()` and `os.access(W_OK)` rather than
`exists()`, because on Windows `Path("/data")` is drive-relative, resolves to
`C:\data`, and is creatable.

`logs_dir()` is the deliberate exception. `DERATE_LOG_DIR` wins, else `<project
root>/logs` -- next to the code, where somebody debugging is already standing --
and only falls through to `data_dir()/logs` when the checkout itself is not
writable. The tradeoff is stated rather than hidden: in the image the project
root is `/opt/derate`, which is a layer and not the `derate:/data` volume, so
logs there do not survive `docker rm`, and `DERATE_LOG_DIR=/data/logs` buys the
old behaviour back exactly.

`project_root()` derives from `Path(__file__)`, never from a cwd, because the
entrypoint's working directory is not the process's for long and a node agent
resolves this after uvicorn has been up for a week.

## `redaction.py`

`Redactor`, `SecretRedactingFilter`, `looks_like_secret`, `has_known_key_shape`,
`SecretsError` and `REDACTED` -- the standard library and nothing else.
`providers/secrets.py` re-exports every name, so `from .secrets import Redactor`
still works and nothing else moved when the code did.

**It is at this level because two rules could not both hold with it inside
`providers/`.** `logfiles.py` writes files on every node and cannot write a line
it has not scrubbed; `node.py` promises the worker path imports nothing from
`control_plane.providers`. The rule it enforces is that `api_key_ref` is the
*name* of an environment variable or of a key in `secrets.json` at 0600, never
the key itself, and the `Redactor` is the backstop for when something goes wrong
anyway -- an upstream answering `401 {"message": "Invalid API key sk-or-v1-..."}`
would otherwise put a live key into an error handed to a client.

`scrub()` replaces remembered values longest-first, so an overlapping prefix
cannot leave a tail behind, then applies nine vendor-prefix patterns.
`contains_secret()` is deliberately narrower -- it answers "did we leak a key we
hold" -- and `assert_clean()` raises rather than emit.

**`has_known_key_shape` exists because `looks_like_secret`'s entropy fallback is
also the shape of a model name.** That fallback -- 32+ contiguous
mixed-case-and-digit characters -- refuses every GGUF variant of
`mistralai/Mistral-Small-24B-Instruct-2501`, while `Qwen2.5` and `Llama-3.1`
pass because their dots break the run: a whole model family failing in a way
that reads as random. Use `has_known_key_shape` where the value is passed to an
upstream and never persisted or displayed.

## `version.py`

`build_id()` answers "which build is this", cached for the process because it is
read on every `/agent/profile` and every health round. Resolution order:
`DERATE_BUILD`, stamped into the image at build time; else `git rev-parse HEAD`
truncated to `SHA_LENGTH` (12) with `+dirty` appended when the tree has changes;
else `""`.

It exists because of an incident. A Raspberry Pi joined the cluster, its image
predated the CPU probe, and the roster read *"device class is not recognized;
cannot confirm this hardware is eligible to join the pool"* -- a sentence about
the hardware for a fault entirely in the software. Nothing could say which build
was running, so "three commits behind" and "a mystery box" rendered identically.

**Never raises, and never guesses.** `_git()` is shaped like the hardware probe:
no git binary, no repository, a 2 s timeout and a non-zero exit are all the same
answer. `same_build()` returns `True` for two unknowns -- not comparable is not a
reportable difference -- and `skew_note()` returns a sentence only when both
builds are known and differ. The package version from `pyproject.toml` is
deliberately not a fallback; it has been `0.1.0` for the life of the project.

## `fsutil.py`

Portable "this file is mine alone". `harden_fd(fd, path)` sets 0600 on an open
descriptor before any content is written, which is the property the call sites
rely on and the reason it takes an fd rather than a name; `harden_path(path)`
does the same for a file already in place. Both return whether the mode was
applied. Eleven call sites in eight modules use them today -- the cluster token
(`registry/identity.py`, `registry/nodeident.py`), enrollment tokens, the
roster, provider keys (`providers/secrets.py`, `providers/store.py`), the
settings store and the setup state.

**The two failures are opposite, which is why neither is left to the platform.**
`os.fchmod` does not exist on Windows: called inside a `try` whose handler
re-raises, the API-key write is lost to an `AttributeError`. `os.chmod` *does*
exist there and does almost nothing -- it toggles the read-only attribute and
restricts nobody -- which is worse, because the write succeeds and every reader
of the code believes the mode took. `MODES_ARE_ENFORCED` is
`hasattr(os, "fchmod")`: a capability test, not a platform name, so it stays
correct on whatever asks next. `_warn_once` keys on the path, because a node
rewrites its settings on a timer and fifty warnings an hour is noise.
`confidentiality()` returns that fact as a `Confidentiality` record with an
operator-facing note; nothing imports it yet.

## `humanize.py`

`binary_bytes(n)`, and only that. `fit/calculator.py` had the careful version
and `gateway/internal_api.py` had a one-line divide that reproduced the bug the
careful version existed to avoid: "stopped after 0.0 GiB of 4.2 GiB" is what an
early download failure looked like.

**The rule is that a non-zero quantity always has a digit to show.** A refusal
whose overage is 40 MiB rendered as "Over budget by 0.0 GiB" says the thing does
not fit and that it is over by nothing, in one breath -- it reads as a broken
calculation rather than a near miss, in the one string a person most needs to
trust. So anything non-zero steps down through MiB and KiB, while a genuine zero
still prints `0.0 GiB`, because an empty comm buffer is a real zero and should
look like one. `contracts/derived.py` names this the canonical
`binary_byte_formatter` and declares `fit.calculator:_gib` a copy;
`tests/unit/test_single_source.py` fails when they stop matching.

## `procmatch.py`

`matches_deployment(command, cluster_id, port)` -- does this command line belong
to a deployment. Layer-free on purpose: the two callers sit on opposite sides of
`DeploymentPort` and neither may import the other. `gateway/gpu_procs.py`
annotates a node agent's process list before drawing a Kill button;
`deploy/manager.py` picks which PIDs to signal when `sparkrun stop` will not
confirm. The command line is the only evidence there is, because sparkrun
launches the backend across an SSH hop and hands back a cluster id, never a PID.

`port_in` matches the port as a *number*: `--port 81000` is not a match for
8100, and neither is `810`. Getting that wrong in the permissive direction
attributes a stray process to a deployment and hides the Kill button on the
thing actually holding the pool.

## `__init__.py`

One line: `"""Derate: measure the link, plan the parallelism, refuse the OOM."""`
No re-exports. Importing `control_plane` pulls in nothing, which is what lets
`from control_plane import fsutil` be cheap on the worker path.

## The twelve packages this one composes

| Package | The claim it is responsible for |
|---|---|
| [`contracts/`](contracts/README.md) | the frozen types, enums and constants. Nobody edits one to unblock themselves |
| [`registry/`](registry/) | the cluster's picture of itself: which machines exist, what they are, whether they are alive |
| [`links/`](links/) | what the interconnect actually delivers, not what the spec sheet claims. Nothing downstream may hold it as a constant |
| [`resolver/`](resolver/) | a HuggingFace id in, a complete `ModelShape` out, with provenance and real on-disk weight bytes |
| [`fit/`](fit/README.md) | the blocking out-of-memory gate. It refuses, and names the term that blew the budget |
| [`planner/`](planner/README.md) | TP/PP/EP/DP from measured facts, explained in a sentence the UI shows verbatim |
| [`deploy/`](deploy/README.md) | the sparkrun adapter and the deployment lifecycle |
| [`gateway/`](gateway/README.md) | the one HTTP surface: `/v1`, `/api`, and the built UI |
| [`providers/`](providers/README.md) | remote OpenAI-compatible upstreams as first-class route targets |
| [`inventory/`](inventory/README.md) | one materialized view of what models this cluster knows about, so fourteen screens stop each deriving their own |
| [`telemetry/`](telemetry/README.md) | per-node SQLite journals, drained by the coordinator into a typed archive and rolled into buckets |
| [`runtimes/`](runtimes/README.md) | the inference servers derate ships itself. Runs inside a model container, never in the node image |

Every link above lands on that package's own README, except `registry/`,
`resolver/` and `links/`, which had none when this was written.

## The seam with the packages below

Nothing at this level imports a subpackage except `node.py` -- and `node.py`
imports all of them, which is what makes it the composition root rather than a
module. Every other module here is a leaf: the traffic is one-way, upward.

- **`paths.py`** has the widest reach -- fifteen modules in nine packages:
  `registry/config.py`, `registry/agent.py`, `registry/enrollment.py`,
  `registry/shell_config.py`, `resolver/cache.py`, `resolver/avatars.py`,
  `links/service.py`, `providers/config.py`, `providers/logos.py`,
  `telemetry/config.py`, `deploy/manager.py`, `deploy/sparkrun.py`,
  `gateway/settings_store.py`, `gateway/setup_state.py` and `gateway/app.py`.
- **`fsutil.py`** is imported by the six modules that write a secret plus
  `gateway/settings_store.py` and `gateway/setup_state.py`.
- **`version.py`** goes to `registry/agent.py` and `registry/registry.py`
  (`build_id`), `gateway/internal_api.py` and `gateway/setup_api.py`
  (`build_id`), and `gateway/serialize.py` (`same_build`).
- **`redaction.py`** goes to `logfiles.py`, `gateway/proxy.py`,
  `providers/config.py` and `providers/secrets.py`.
- **`humanize.py`** goes to `fit/calculator.py` and `gateway/internal_api.py`.
- **`procmatch.py`** goes to `deploy/manager.py` and `gateway/gpu_procs.py`.
- **`envspec.py`** is read by `contracts/manifest.py::reflect_env` and by
  `tests/unit/test_single_source.py`, and by nothing at runtime.
- **`logfiles.py`** is installed by exactly two entrypoints: `node.py::main` and
  `gateway/main.py`, both immediately after `basicConfig`, both using
  `logfiles.LOG_FORMAT` so a line copied out of a file reads the same as one
  seen in a terminal.

```python
# the entire coordinator, as node.py composes it
runtime = await start_node(RegistryConfig.from_env())
deps = build_gateway_deps(runtime, config)          # strict=True
app = create_app(deps, settings=deps.settings, telemetry=runtime.telemetry)
```

## Things that look like details and are not

**A module at this level is a leaf, and that is a constraint on where code may
be written, not a description of where it happens to be.** `redaction.py` moved
out of `providers/secrets.py` and `humanize.py` out of `fit/calculator.py` for
the same reason: a second consumer appeared on the other side of a boundary the
first one could not cross. `providers/secrets.py` still re-exports all of
`redaction.py` so no call site had to change.

**`logfiles.install()` starts with a fresh `Redactor` that has never seen a
key.** A `Redactor` only scrubs values it has been told to remember, and the
entrypoints install logging long before a `ProviderService` exists. The fresh
instance still applies the vendor-prefix patterns; `adopt_redactor()`, called
from `build_gateway_deps` at the moment the service is built, swaps in the one
that remembers resolved values. `telemetry/service.py` shares the same instance
for the same reason.

**The redacting filter is on the handler, not on a logger.** A
`logging.Filter` attached to a logger is consulted only for records logged
through that object, never for a child's -- and most of what lands in these
files is `gateway.*`, outside the `control_plane.` hierarchy entirely, where a
filter on `control_plane` has never reached. A handler on the root logger sees
them all through propagation.

**`data_dir()` obeys `DERATE_DATA_DIR` even when the path is wrong.** An
operator naming a path is entitled to be wrong about it, and a resolver that
second-guesses the name makes the variable untrustworthy. The writability check
governs the *fallback* only.

**Neither `data_dir()` nor `logs_dir()` creates its directory.** A read-only
caller asking where the estate is should not have the side effect of making one.
`logfiles.install()` is what calls `mkdir`, because it is the one caller that is
about to write.

**`envspec.default` is the literal as written at the reading site, not a
normalized value.** `tests/unit/test_single_source.py` compares them by string:
`test_declared_defaults_match_what_the_code_actually_falls_back_to` fails when a
declaration drifts from the `os.environ.get` beside it, and
`test_no_two_readers_of_a_variable_disagree_about_its_default` fails when two
files read one name with different fallbacks. That second one exists because
`docker/placeholder_app.py` defaulted `DERATE_AGENT_PORT` to whatever
`DERATE_PORT` was while the Dockerfile and `registry/config.py` both said 8081 --
an operator who named only the coordinator port got an agent on top of it, with
no error either way.

## Failure behaviour

- **The log folder is unwritable.** `install()` logs a warning, attaches no
  handlers and returns `None`. Never raises. stderr and the telemetry journal
  are untouched.
- **A log file cannot be opened.** Every handler made so far is closed, a
  warning names the folder, and the process runs without files.
- **`/data` is absent or read-only.** `default_data_dir()` returns the
  platform's application-state directory instead. The container is unaffected:
  `/data` exists and is writable there, so it is still chosen.
- **The checkout is on a read-only mount.** `logs_dir()` falls through to
  `data_dir()/logs`, which `default_data_dir()` has already guaranteed is
  writable.
- **This platform cannot enforce file modes.** `harden_fd` and `harden_path`
  return `False`, warn once per path, and let the write proceed;
  `confidentiality()` reports `enforced=False` with the note that anyone who can
  log in can read the stored keys.
- **A filesystem accepts the open but not the mode.** Same outcome, by the
  `OSError` branch -- some network and FAT mounts do this.
- **No git, no repository, or git is slow.** `build_id()` returns `""`, which
  renders as "an unidentified build" through `describe_build`. `skew_note()`
  returns `None` rather than reporting a difference between two unknowns.
- **A `LogRecord` cannot be formatted.** `SecretRedactingFilter.filter` returns
  `True` and lets it through unscrubbed rather than dropping it; a broken record
  is not the filter's problem.
- **A port is missing at composition.** `GatewayDeps(strict=True)` raises and
  the process does not start. A missing wire is a startup crash, never a silent
  stub.
- **`build_gateway_deps` is called with no registry.** `RuntimeError`, naming
  the fact that `runtime.role` must be coordinator.
- **A signal handler cannot be installed** -- Windows, or a loop that is not the
  main thread's. `_run_with_signals` passes; the process can still be stopped by
  cancellation.

## Deliberately not built

**A package-version fallback in `version.py`.** `pyproject.toml` would answer
"which build" with `0.1.0`, a string true of every build ever made -- the exact
failure this module exists to end, in a more confident voice.

**A third byte formatter, and a second one folded away.**
`planner/comm.py::human_bytes` stays where it is: it formats per-step transfer
volumes in the same sentence as a measured link bandwidth, and bandwidth is
quoted in decimal GB/s throughout the planner because that is the unit the
measurement returns. Binary volumes beside decimal rates would be a worse
sentence, not a more consistent one. Two formatters is right when there are two
units; the mistake was three formatters for two.

**An ACL implementation for platforms without modes.** `fsutil.py` hardens where
it can and says so where it cannot. A partial Windows implementation that
returned `True` would put the product back where `os.chmod` left it -- a write
that succeeds and a reader who believes the mode took.

**A role election.** `node.py` serves whichever role `start_node` resolved and
does not reconsider. There is no failover here and no promotion; a worker whose
coordinator is gone waits for one.
