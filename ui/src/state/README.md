# state

Everything the UI knows that is not a component. Four kinds of thing live here
and they are held four different ways: **the address bar** is the selection and
the question being asked, **polled resources** are the coordinator's structure,
**one SSE stream** is the live numbers, and a handful of pure modules are the
vocabulary every screen has to agree on -- what to call a machine, when a lamp
turns amber, which runtimes go through the launcher.

The rule underneath all of it is that nothing on screen may be confidently
wrong. A poll that fails keeps its last good answer rather than blanking; a
poll whose *question* changed drops its answer immediately, because another
machine's numbers under this machine's heading do not read as stale -- they are
real numbers, just about something else.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `routes.ts` | 273 | The URL scheme. `parse`, `href`, `Dest`, `SheetRef`, `DESTINATIONS`. Pure, no React |
| `router.tsx` | 99 | The React half: `RouterProvider`, `useRouter`, `plainClick` |
| `selection.tsx` | 230 | Node, link, deployment and sheet, read and written through the URL |
| `placement.ts` | 78 | `?on=` and `?tp=`/`?pp=` -- the shape a model is being planned at |
| `backend.tsx` | 145 | `BackendProvider`, `useBackend`, `useResource`, `useKeyedResource` |
| `resources.ts` | 192 | Every polled endpoint as a hook, with its interval argued once |
| `history.ts` | 393 | The `/api/history/*` client, and the adapters for its two row shapes |
| `useMetrics.ts` | 56 | The SSE subscription itself, and `SafeMetricsFrame` |
| `metrics.tsx` | 24 | The one place `useMetrics` is called |
| `useTelemetry.ts` | 147 | Nine series, sixty seconds, accumulated from the frame |
| `telemetry.tsx` | 45 | The one place `useTelemetry` is called |
| `names.ts` | 75 | What to call a machine. Label, else `node_id`, never hostname |
| `live.ts` | 131 | Whether a node's readings are live, and whether they are worrying |
| `launchPhase.ts` | 70 | The launch ladder's captions, and the forwards-only fold |
| `runtime.ts` | 88 | `vllm` / `sglang` / `tts` / `ollama`, and `servesOnCluster` |
| `policy.ts` | 9 | The two routing policies under which `weight` is a traffic share |
| `customServes.ts` | 106 | Remembered custom serve commands, in `localStorage` |
| `router.check.mjs` | 257 | Verifier: a route survives being written down. Hermetic |
| `history.check.mjs` | 241 | Verifier: the adapters against real captured payloads |

## `routes.ts`

The scheme, with no React in it so a node process can check it. `parse(url)`
gives a `Route`; `href(route)` gives the one canonical spelling of it. The
split it enforces: **the path names the screen and that screen's own subject**
(`/models/meta-llama/Llama-3.1-8B`), **the query names what is selected**
(`?node=`, `?link=`, `?dep=`, `?open=`, `?ctx=`/`?seq=`, `?on=`, `?tp=`/`?pp=`).
Selections are query parameters because they are not owned by a destination --
the same node is selectable on the cluster floor, in the dashboard's telemetry
strip and in the sidebar roster.

A model id keeps its slashes as real path separators and is the whole tail of
the path, because `/models/meta-llama/Llama-3.1-8B` is the URL somebody would
guess. Anything unrecognised -- a typo, a truncated paste, a link from a newer
build -- parses as the dashboard rather than as an error state, and
`RouterProvider` rewrites the address bar to say so.

`enc()` is hand-rolled rather than `URLSearchParams` for three characters:
`:`, `/` and `,` are legal unescaped in a query (RFC 3986 §3.4), so
`?open=model:meta-llama/Llama-3.1-8B` and `?on=spark-01,spark-02` stay readable
instead of arriving as `%3A`, `%2F` and `%2C`. Parsing accepts either form.

`DEFAULT_CONTEXT` is 8192 and `DEFAULT_CONCURRENCY` is 1, and neither is what a
missing `?ctx=`/`?seq=` means. Absence means the coordinator picks per model
from the fit arithmetic, which is what lets the models screen show real
verdicts on a fresh install with nothing typed anywhere. They are the number
the disclosure shows in its box before it has an answer.

## `router.tsx`

`RouterProvider` holds `window.location.pathname + search` in state, listens
for `popstate`, and settles every URL onto `href(parse(...))` with a
`replaceState` and no history entry -- so `/`, a hand-edited link and a stale
one all land on the canonical spelling and Back still goes wherever the person
came from.

`navigate(patch, {replace})` merges onto **the route the address bar holds
now**, by re-parsing `here()` rather than closing over `route`. A handler held
from an earlier render would otherwise write its patch onto a stale route and
silently revert whatever happened in between. `linkTo(patch)` is the same merge
as a string, so destinations render as real `<a href>` and the browser's own
"copy link address" and middle-click keep working; `plainClick(e)` is what tells
a click the app should handle from one the browser should keep.

The push/replace rule lives at the call sites and is one sentence: **selecting
replaces, opening pushes.**

## `selection.tsx`

`SelectionProvider` exposes `selDep`, `selNode`, `selLink`, `sheet` and the six
verbs that move them. None of it is component state -- all four live in the URL
query, so a selection is something you can send to somebody, and Back works on
all of it. Selecting a node clears the link and toggles; selecting a link clears
the node and toggles; `pickNode` sets without the toggle; the selected
deployment never goes empty on its own. `linkKey(a, b)` is exported beside
them and is the `"a~b"` spelling itself, sorted, so the same pair of machines
keys the same way whichever end the caller names first.

**`?dep=` carries a served name, never a deployment id**, and it is validated
against `topology.deployments` *and* `topology.remotes`. It used to be checked
against the local deployments alone, which was right until the chat picker
started offering provider models: clicking one of those rows navigated, failed
the test, and fell through to `defaultDep` -- quietly selecting a *different*
model with nothing on screen saying so. `defaultDep` stays local-only on
purpose: what is selectable and what is selected by default are different
questions.

`selNode` and `selLink` are read from refs inside the toggles rather than taken
as dependencies, because `ClusterGraph` keys its pointer and key listeners on
those callback identities and would rebind every listener on the machine floor
on each click.

## `placement.ts`

`usePlacement()` returns `nodeIds`, `degrees` and their two setters, reading and
writing `?on=` and `?tp=`/`?pp=`. `null` is "the planner picks" and is a
different request from any array -- including the empty one, which the gateway
answers with a 400, so unticking the last machine hands the choice back rather
than asking for a plan on nothing.

Its own module rather than four more members on `selection.tsx`: what that file
holds is a *selection*, toggled by clicking around, and this is an argument to a
request. They share a mechanism and nothing else.

The `nodeIds` array is rebuilt from a joined key so its identity is stable
across a poll that did not change the URL. `ServePanel`'s debounced plan effect
has both `nodeIds` and `degrees` in its dependency array; a new array every
render would refire it forever.

## `backend.tsx`

`BackendProvider` holds the one `Backend`, a `revision` counter that any
state-changing action bumps so every polled hook refetches at once instead of
waiting out its interval, and the coordinator `origin`. `useBackend()` is the
only way to reach any of the three, `invalidate()` included -- it is a field on
that context and not an export of its own. The backend object is
rebuilt per origin -- `httpBackend` is a module singleton whose methods read the
base at call time, but every effect downstream keys on the backend's *identity*,
and without a fresh one the metrics `EventSource` would go on streaming from the
coordinator you just navigated away from.

`useResource(read, intervalMs)` polls and **keeps the last good value through a
failed refresh**; `loading` is true only before the first success, because stale
structure beats a flash of nothing.

`useKeyedResource(key, read, intervalMs)` is the parameterised form and exists
because the plain one is wrong the moment a read takes an argument: it holds
`read` in a ref, so a hook called with a new node id keeps polling the old one
until the next tick and goes on returning the old node's `data` in the meantime.
Another machine's processes, under this machine's heading, for up to a full
interval -- and nothing about it reads as stale, because the numbers are real.
The coordinator origin is folded into the key whether the caller thought about
it or not.

## `resources.ts`

Every polled endpoint in the UI, one hook each, with the interval argued at the
line it is set on rather than in a table somewhere. The spread is deliberate and
is a claim about how fast each fact moves: `useMemoryReport` and `useActivity`
at 2s (a two-second-old allocatable figure is a different decision from a
five-second-old one, and a download's bar has to move), `useCandidates` at 3s,
structure at 5s, `useModels` and `useModelRegistry` at 10s, `useProviders` at
15s, `useCapacity` at 20s, `useStorage` at 30s, the provider catalogues at 5
minutes, `useQuantTable` and `useCatalog` at an hour.

Two hooks take an "off" argument and resolve an empty payload rather than being
skipped, since hooks cannot be called conditionally: `useStorage(enabled)`,
because one call fans out to every node agent and walks a directory on each --
mounting it unconditionally would make every dashboard visitor trigger a
cluster-wide disk walk every 30s to populate a menu they may never open -- and
`useNodeProcesses(nodeId)`, where each poll costs an `nvidia-smi` on that node.

Model detail and the variant ladder are deliberately **not** here: `useResource`
refires on every `revision` bump, so an open model would re-run its hub calls
after every launch, admit and settings change.

## `history.ts`

The client for `/api/history/*`, and the only reader of the durable archive --
thirty days of raw node samples and four hundred of hourly rollups, surviving a
restart. Before it, the only history anywhere in the UI was sixty seconds
accumulated in a tab and lost on reload.

Five keyed hooks -- `useNodeHistory`, `useRequestHistory`, `useNodeEvents`,
`useNodeLogs`, `useShellStatus` -- and every one of them a no-op on `live`,
where the browser's own ring is the source. Everything under them is pure and
free of React: `windowLabel`, `bucketSeconds` and `resolutionNote` caption the
axis (`n × 1m`, `n × 1h`, or `n samples`, never a bucket count spelled as a
sample count), and the four adapters below do the reading.

`HISTORY_WINDOWS` is `live`, `5m`, `1h`, `24h`. `live` is not a fetch at all: it
is the ring `telemetry.tsx` accumulates. `5m` is offered first because
`TELEMETRY_RING_S` is 300, making it the one archive window a box with telemetry
switched off can still answer -- from the registry's in-RAM ring, labelled
`durable: false`. `WINDOW_SPEC` polls the three at 10s, 30s and 60s: a 24-hour
chart re-read every second is the same picture and a scan of the archive each
time.

**The awkward part is that one route answers with two row shapes.** Under six
hours it returns raw per-second columns (`power_w`); wider, bucket aggregates
(`power_w_avg`) and no `memory_total` at all. `nodeSeries` reads whichever is
present, with the node profile's total as the denominator for `mem` when the row
carries none -- the same figure `serialize.node_payload` divides by, so the
chart and the quad above it cannot disagree. A row carrying neither yields a
`null` **point** rather than being dropped, because a null lifts the pen in
`Chart` and a sampler that went quiet draws as the hole it is.

`nodeBand` recovers the bucket maximum, a column that was being thrown away: on
the 1h and 24h windows a node that held 140 W for forty seconds inside a
one-minute bucket drew as whatever the minute averaged to. The spike was not
smoothed, it was absent, and the chart said nothing about the fact. It returns
`null` -- not an empty array -- for a window with no maximum to give, so a band
never renders as a line pinned to the floor.

`depSeries` answers only for rolled buckets: a raw row is one request, not a
rate. `requestTotals` sums the counters and reports `null` for a total no row
carried, so "nothing was recorded" never renders as a measured 0. Percentiles
are never summed or averaged -- the busiest bucket's own summary is reported,
labelled with the count behind it.

## `useMetrics.ts`

The actual SSE subscription. `SafeMetricsFrame` is `MetricsFrame` with
`nodes`/`deployments` coalesced to arrays: the wire is honest about nulls on the
degraded path, and that honesty is resolved here, once, so no consumer needs a
guard around its own `.find`. `HISTORY_SECONDS` is 60 and the cluster
throughput trace is trimmed **by time, not by count**, so a stream gap leaves a
real gap rather than compressing the window when samples resume.

`stale` is true while the stream is down, and the contract for it is that live
values render greyed -- not frozen and not zeroed. A frozen number that looks
live is worse than an obviously stale one.

## `metrics.tsx`

Twenty-four lines, and the reason it exists is the whole of it: this is the one
place `useMetrics` is called. Before it, every consumer that wanted the frame
opened its own `EventSource`, so one tab's worth of components meant one tab's
worth of streams. The header's stream lamp, the roster and the telemetry charts
now read one frame through one context.

`useMetrics()` throws outside `MetricsProvider` rather than returning an empty
frame, so a missing provider fails at the call site instead of rendering a
cluster that looks idle.

## `useTelemetry.ts`

Exactly nine series -- cluster tokens/sec and power, four per node keyed by
`node_id`, three per deployment keyed by served name -- accumulated from the
metrics frame at its own 1 Hz over a 60-second window. The frame only knows
`deployment_id`, so the caller supplies the served-name lookup.

`push` trims by time rather than count, and `v: null` is a real point kept
rather than skipped: "this tick had nothing to say" is a gap a chart can draw
instead of interpolating across. `pushKeyed` does the same across every key ever
seen, so a node that drops out of a frame gets a null point and its chart shows
the gap instead of quietly stalling on the last value it had.

This is the live window and nothing more: this browser tab, gone on reload. It
is not interchangeable with `history.ts` -- this one has no gaps because it has
no memory of having missed anything, which is exactly what the archive's
envelope reports and this cannot.

## `telemetry.tsx`

The one place `useTelemetry` is called, for a sharper reason than `metrics.tsx`:
this accumulation is **stateful**, so calling it twice means two windows that
fill independently. It used to be called in `DashboardTab` and prop-drilled from
there, which made the accumulated history reachable only from that one
destination -- and the node sheet is not inside the Dashboard, so a chart in it
would have started an empty 60-second window every time somebody opened a
machine. A graph that is blank for a minute after every click is not a graph.

Mounted above `AppShell`, so the window keeps filling whichever destination is
showing and whether or not a sheet is open.

## `names.ts`

`nodeName`, `nodeSubtitle`, `nameIndex`, and the two unwrappers `fromState` /
`fromTopology`. Two rules, and the second is the one that matters.

A machine is called by its label if an operator gave it one, and by its
`node_id` otherwise -- **never by its hostname.** A worker in a `--network host`
container reports the host's hostname, so a two-node Spark can and does show two
machines both called `spark-4d38`, which is what this module exists to stop.

Whatever is shown, `node_id` stays the identity: deployments, link measurements,
routing targets and the saved graph arrangement are all keyed by it, so a
renamed plate carries its id in the line beneath. `nameIndex` falls back to the
id itself for anything not in the roster, because an id we cannot name is still
an id. `MAX_LABEL_LEN` is 48 and mirrors `control_plane/registry/labels.py`,
purely so the rejection arrives as a full input rather than as an error message.

## `live.ts`

Whether a node's readings are current, and whether they are worrying.
`nodeLive(node, frame, streamStale)` prefers the stream and falls back to the
last snapshot, returning `fresh: false` so the caller greys the numbers instead
of passing them off as live. `nodeSignal` turns that into `live` / `warn` /
`fault`, and `utilKind` / `utilLabel` decide whether the tile says GPU or CPU --
a node the probe found no GPU on reports CPU utilisation from `/proc/stat`, and
calling that "GPU utilisation" would label hardware the machine does not have.

`SAMPLE_STALE_S` is 30, and its canonical case is a container started without
`--gpus`: its agent answers `/agent/health` forever, so `last_seen` stays fresh
while power, temperature and utilisation are frozen at whatever they read when
`nvidia-smi` was last reachable. `HOT_C` is 80.

`MEMORY_PRESSURE_PCT` is 92 and is a **fallback on a different metric than the
server's.** `/api/memory` already answers this from `memory_warn_pct` (90) and
`memory_critical_pct` (95) measured against `memory_pressure_pct`, the only
figure that accounts for both foreign load on a unified-memory node and the host
reserve -- pass `severity` and 92 is never consulted. Do not tune it to 90: the
metrics are different, so the numbers should not be equal, and making them match
would hide the remaining gap.

## `launchPhase.ts`

`LAUNCH_PHASES` is `preparing`, `downloading`, `loading`, `starting`, `serving`,
and `LAUNCH_PHASE_LABEL` is what each is called on screen. The server owns the
vocabulary and its order (`control_plane/deploy/progress.py`, read off
sparkrun's output and the backend's own log); what lives here is the half a
person reads. Two screens draw this ladder -- the first-run wizard's stepper and
the rail's activity rows -- and before this file they drew two different ones,
because the wizard inferred its phases from bytes appearing on disk.

**Phases only ever move forward.** A log tail is a window, not a stream: a poll
can land after the interesting lines have scrolled out of it and come back with
an older marker, and a stepper that un-ticks a step reads as something going
wrong. `forward(held, next)` applies that rule on this side too, because the
wizard holds its own phase across polls.

`phaseRank` returns -1 for a phase this build has not heard of, and that is not
an error: a deployed UI can predate a phase the server names, and the honest
response is to show its verbatim sentence and tick nothing. `phaseLabel` is
the same rule as a caption -- `''` for an unknown phase rather than the raw
string, so a server-side identifier never reaches the screen as a step name.

## `runtime.ts`

`Runtime` is `vllm | sglang | tts | ollama`, and the one thing every screen
needs from that choice is whether it goes through the launcher. `vllm` and
`sglang` mean plan, fit gate, sparkrun, onto machines this cluster owns;
`ollama` means telling a provider on the LAN to fetch a GGUF onto itself. Almost
every difference downstream follows from that one fact rather than from the
runtime's name, so `servesOnCluster(runtime)` states it once instead of being
re-derived as `runtime === 'ollama'` at the plan call, the node board, the
degrees, the verdict and Serve. The union lived in five places as a bare
`'vllm' | 'sglang'` before this file.

`shardsAcrossNodes` is false for `tts` -- one process, one checkpoint, so TP and
PP are not degrees it has and `deploy/flags.py::sharding_refusal` refuses a plan
carrying them. The fields are hidden rather than shown and then rejected.
`runtimeFor(modality)` picks `tts` for speech; `asRuntime` narrows anything
arriving from outside the type system and falls back to `vllm`, because every
caller needs *a* runtime and a null would render an empty picker.

Deliberately not in the URL: `?on=`/`?tp=`/`?pp=` are there because a verdict is
only worth sending if the question it answers travels with it, and a runtime
does not change a verdict -- under `ollama` there is no verdict at all.

## `policy.ts`

Nine lines. `PROPORTIONAL` is the set of routing policies under which a target's
`weight` is semantically a configured traffic *share*: `weighted_capacity` and
`round_robin`. Drawing a filled proportional bar under `least_outstanding` would
claim a split the router does not use for selection. Shared by the routing
sidebar and the cluster graph's per-machine share bar so the two cannot
disagree.

## `customServes.ts`

One launch's custom command, remembered in `localStorage` under
`derate.models.customServes`, so a quantization flag or a memory knob can be
replayed onto a model without retyping it. `recordCustomServe` is called once,
from `ServePanel`, and only after `backend.launch` has resolved -- a launch the
backend rejected taught nothing worth replaying.

Client-side only, and safe to be: picking an entry seeds the Serve field, so the
plan and the fit gate run exactly as they would for anything typed by hand and
the token allowlist in `deploy/recipes.py` checks every token again on replay. A
corrupt value starts the list empty rather than diagnosing itself.

`useCustomServes()` is the list, newest first, and `removeCustomServe` is the
delete. Both re-read `localStorage` on every call, because nothing but this
tab's own launches and its own deletes ever changes the key.

`MAX` is 12 -- a screenful, not a database. The `derate:custom-serves-changed`
event exists because `storage` fires in every *other* window and never in the
one that wrote the key, and the box has to update the moment a launch in this
tab succeeds.

## `router.check.mjs`

Hermetic -- no `// requires:` line, so it needs nothing but the checkout. It
bundles `routes.ts` with the esbuild already inside vite (node's ESM resolver
will not take an extensionless specifier) and asserts two properties over
sixteen URLs:

```
parse(href(r)) === r        a route survives being written down
href(parse(u)) is stable    a URL has one canonical spelling
```

A URL is the one piece of this UI that leaves the machine, and both directions
are composed on purpose: comparing a route to itself would pass under any
encoding at all. It walks `DESTINATIONS` rather than a hand-kept list of paths,
because a `Dest` with no `SEGMENT` entry parses as the dashboard and `href`s to
`/undefined` -- silently, in both directions.

It also records a reversal. Three of its assertions used to say the opposite:
`?ctx=8192` was normalised away, because absence and 8192 were the same request
and one screen must not have two URLs. Absence now means the coordinator picks
per model, so dropping an explicit 8192 on the way out would silently rewrite
"judge everything at 8192" into "judge everything at whatever fits" on a link
somebody shared. Two questions, so two URLs, and the rule is intact: one
spelling per meaning.

## `history.check.mjs`

`// requires: fixtures HISTORY_FIXTURES`. It exists for the duality
`history.ts` describes: reading the wrong column does not throw, it yields an
empty chart, which is indistinguishable from a machine that was switched off --
and typecheck cannot catch it, because every column is optional precisely so
both shapes fit one type.

It asserts the shapes directly (`a rolled sample carries NO memory_total`)
before it asserts anything about the adapters, so if the archive ever starts
sending `memory_total` on a rollup the fallback is caught doing nothing. The
fixtures are **real payloads captured from a live coordinator's archive**
(spark-4d38 serving, worker-docker gone quiet about two hours before), not
hand-written objects: a fixture written from the same reading of the schema that
produced the bug agrees with the bug. Unset the variable and it exits 2 after
printing the four `curl` lines that capture them.

It bundles inside the ui root rather than `/tmp`, because `history.ts` reaches
React through `state/backend` and a bundle in `/tmp` has no `node_modules` above
it.

## The seam with the rest of the UI

Sixty-five files outside this folder import from it, second only to `api/` at
eighty-six. Nothing here talks to the coordinator directly -- every read goes
through the `Backend` interface in `api/client.ts`, and the origin it points at
comes from `api/origin.ts` through `useSyncExternalStore`.

Five providers, and their nesting is load-bearing. `main.tsx`:

```tsx
<RouterProvider>
  <BackendProvider>
    <MetricsProvider>
      <TelemetryProvider>
        <AppShell />
```

`SelectionProvider` is mounted lower, in `shell/AppShell.tsx`, because selection
is shell state: `Sheet` and every destination need it and nothing outside the
shell has a reason to reach it. `TelemetryProvider` must sit above `AppShell`
for the reason its own section gives.

The call sites never learn that a URL is involved:

```tsx
const { selNode, selectNode } = useSelection()   // writes ?node=, replace
const { nodeIds, setNodeIds } = usePlacement()   // writes ?on=,   replace
const cluster = useTopology()                    // polls, 5s, last-good on failure
const { frame, stale } = useMetrics()            // the one SSE subscription
```

## The two verifiers, and the `// requires:` line

`npm run check` (`ui/check.mjs`) **discovers** verifiers rather than listing
them, and reads what each one needs off a `// requires:` line in the first 4 KB
of the file: `coordinator`, `python`, `browser`, `fixtures $NAME`, or no line at
all. **Absent means hermetic**, which is the safe default -- a verifier that
forgets to declare a dependency fails loudly on a machine without it rather than
being skipped everywhere forever.

`router.check.mjs` has no line and runs anywhere. `history.check.mjs` declares
`fixtures HISTORY_FIXTURES` and is skipped when the variable is unset. **A skip
is never a pass**: it is counted in its own column with the reason printed, and
`npm run check -- --strict` turns each one into a failure. Both still run on
their own, and that is still the inner loop:

```bash
node src/state/router.check.mjs
HISTORY_FIXTURES=/tmp/derate-history-fx node src/state/history.check.mjs
```

## Things that look like details and are not

**A missing `?ctx=` is not 8192.** It means the coordinator chooses a context
per model from what actually fits, which is what lets a fresh install show real
verdicts with nothing typed anywhere. `DEFAULT_CONTEXT` is what the disclosure
box shows before it has an answer, and the client's fallback when a caller has
to name one. `href` therefore writes an explicit 8192 rather than dropping it.

**`positive()` and `degree()` are all but identical and must stay separate.**
`positive()` used to collapse a value equal to the default to null, which was
right while a missing context meant 8192 and was always wrong for a degree:
`tp=1` against a planner that wants `tp=2` is an override, and collapsing it
hands the axis back to the planner it was overruling. Merging them would make
the next change to one rule silently a change to both.

**`?tp=` and `?pp=` are written as a pair or not at all.** `parallelism` is
adopted as one object on the wire and an omitted key means 1, so "TP mine, PP
the planner's" cannot be expressed; half a pair is completed rather than
half-honoured, and `tp` alone in a URL would parse back as `{tp, 1}` and stop
being the route that was written down.

**`null` and `[]` are different requests for `?on=`.** Null is "the planner
picks" -- what every URL written before the field existed meant. The empty list
is a 400 with no honest answer, so unticking the last machine writes null.

**`useResource` does not deduplicate.** Two components calling the same hook
poll twice. `useActivity` is mounted in exactly one place for that reason, and
`useMetrics`/`useTelemetry` are hoisted into contexts for the same one.

**The coordinator origin is part of every resource key.** Two coordinators
answering the same path are not answering the same question, and without it,
pointing the UI at another coordinator keeps the previous cluster's names and
numbers on screen through the first poll.

## Failure behaviour

- **A poll fails.** `useResource` keeps the last good value and sets `error`;
  `loading` stays false, so nothing blanks. The screen shows structure that was
  true a few seconds ago rather than a spinner.
- **A poll's question changes.** `useKeyedResource` clears `data` in the same
  render the key changes, before the effect runs, so the caller gets its
  "Reading" state back rather than a confident answer to a question it is no
  longer asking.
- **The stream drops.** `StreamState` goes `stale`, `useMetrics` reports it, and
  live values render greyed. `nodeLive` falls back to the node's last snapshot
  and returns `fresh: false`.
- **A node's telemetry dies while its agent stays healthy.** Caught by
  `SAMPLE_STALE_S`, not by the stream state -- the sample carries its own age on
  the same clock as the frame's `ts`.
- **The URL is nonsense.** `parse` returns the dashboard and `RouterProvider`
  rewrites the address bar to the canonical spelling, with no history entry.
  An unknown `?open=` kind or an id-less one is no sheet, not a broken one.
- **A history window has nothing.** The `NO_NODES` / `NO_REQUESTS` /
  `NO_EVENTS` / `NO_LOGS` envelopes are `durable: false` with empty rows, and a
  chart fed from one draws nothing rather than a flat line at zero.
- **One provider cannot answer its catalogue.** `useProviderCatalogues` settles
  each fetch separately: that provider contributes no rows and is named in
  `failed`, rather than taking the others down with it.
- **A provider component is missing.** `useRouter`, `useSelection`, `useMetrics`
  and `useTelemetrySeries` all throw by name rather than returning an empty
  value, so the mistake surfaces at the call site instead of rendering a cluster
  that looks idle or charts that would never fill.
- **`localStorage` is unavailable or corrupt.** `customServes` starts empty; the
  coordinator base falls back to same origin. Neither is a source of truth for
  anything the launcher does.

## Deliberately not built

- **The runtime is not in the URL.** A verdict's question travels with it, and a
  runtime does not change a verdict -- under `ollama` there is no verdict at
  all. `?rt=ollama` would also parse back on screens where it means nothing. If
  that changes it goes in alongside the provider choice, as one announced change
  to `routes.ts` and its verifier.
- **Model detail and the variant ladder are not `resources.ts` hooks.**
  `useResource` refires on every `revision` bump, so an open model would re-run
  its hub calls after every launch, admit and settings change. They are
  user-driven, live in the tab's own state, and are debounced.
- **`useRequestMix` is not live and cannot be.** The 1 Hz metrics frame carries
  no streaming flag and `/api/routing` does not split `outstanding` by one, so
  the request archive is the only place that records how a request was called.
  30s, and a rolled window has no such column at all -- the ratio is null and
  the cluster graph paints the no-reading grey rather than a guess.
- **No second `EventSource`, ever.** `useMetrics.ts` and `useTelemetry.ts` are
  each paired with exactly one context that calls them, and both `use*` hooks
  throw outside it.
