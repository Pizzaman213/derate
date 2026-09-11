# inspectors/node

The node page, rendered as sheet contents. Left column is what the machine
**is** — its live readouts, its four traces, its hardware, its links to the
other machines. Right column is what it is **doing** — every model it serves
with that model's own numbers, the requests that ran here, what is holding its
GPU, what happened to it, and, when none of those said enough, a prompt on the
machine itself.

It grew from a 440 px card that showed four instantaneous readouts and a list
of deployment names. Two things it still deliberately does not do: routing
(targets, shares, circuit state, cost) belongs to the deployment inspector and
each served name here is a button into it, and the throttle slider the original
mockup had stays gone because there is still nothing on the wire to throttle.

## Layout

Sections below are in render order — the header, then the left column top to
bottom, then the right.

| File | Lines | What it owns |
|---|---|---|
| `NodeInspector.tsx` | 222 | the frame: identity, lamp, four readouts, and the composition of everything else |
| `WindowChips.tsx` | 26 | the window selector, and the claim that `live` is a different kind of answer |
| `NodeCharts.tsx` | 113 | power, temperature, utilisation and memory over the selected window, on one shared cursor |
| `Provenance.tsx` | 72 | where the numbers above the line came from |
| `HardwareRows.tsx` | 117 | what the machine is, and what of it is actually available |
| `Interconnect.tsx` | 95 | this machine's links to every peer, and the button that measures one |
| `RenameNode.tsx` | 95 | the caption, and the id that does not move |
| `ServingBlock.tsx` | 144 | one model this machine is serving, as this machine experiences it |
| `RequestsTable.tsx` | 129 | the requests that actually ran here |
| `ResidentProcesses.tsx` | 108 | what is holding the GPU, whether or not derate launched it |
| `NodeRuntimeCard.tsx` | 296 | a runtime already listening here, and the one click that makes it a target |
| `EventsAndLogs.tsx` | 173 | what happened on this machine, in its own words, capped and saying so |
| `NodeLogFiles.tsx` | — | `node.log`/`proxy.log`, tailed over `GET /api/nodes/{id}/logs` -- not rendered by `NodeInspector` itself; Settings -> Instance is its only caller so far |
| `Terminal.tsx` | 300 | xterm over a WebSocket to the node, loaded on demand |

## `NodeInspector.tsx`

`NodeInspector({ node, deployments, routing, frame, stale, onClose })` holds
the one piece of page state — `useState<HistoryWindow>('live')` — and hands it
to every child that reads a window. `.nodegrid` is the two columns (1.15fr to
1fr, collapsing at 900 px); `.nodehead` is sticky against the top of the card,
because the window chips change what every chart below is showing and a control
you have to scroll back up to reach reads as a one-time choice.

Three rules are enforced here rather than in the children:

**The lamp takes the server's verdict, not a threshold of its own.**
`nodeSignal(node, live, severity)` is passed `memory_severity` from
`/api/memory` — the same field `HardwareRows` renders three inches below it.
The lamp disagreeing with the line under it was the whole defect.

**Every part of the hardware summary is dropped when there is nothing true to
put in it.** A Raspberry Pi read `· discrete · 0.0 GB/s`: a memory topology it
does not have, and a bandwidth of exactly zero, which `probe.bandwidth_for`
returns to mean "we do not know this part" and which reads on screen as a
measured bus. `topology` is now `null` outside `gb10` / `apple` / `discrete`,
and the bandwidth term is dropped at `<= 0`. The separate `unified` flag stays
GB10-only on purpose: the sentence it guards is about the static ceiling
overstating what `nvidia-smi` can account for, which is a GB10 fact rather than
a unified-memory one.

**The serving list is `runners(deployments)`, not `deployments`.**
`/api/deployments` is a ledger that keeps every attempt for a week, so nine
failed tries at four models are nine rows all naming this node — and each one
drew a full serving block, lamp and plan line and four readouts and three
charts, for a container that does not exist. `runners()` and
`runningOrPrevious()` (the `here`/`previous` split, including the fallback to
the last terminal attempt when nothing is running now) are both imported from
`tabs/cluster/layout`, the floor's and the dashboard strip's own filter,
deliberately not a fourth spelling of the same set: a machine cannot be
serving something here and idle there. `tabs/settings/InstanceCard.tsx` calls
the same `runningOrPrevious()` for the same node, so the two surfaces cannot
disagree about what "here" means.

The header carries both identities the label may be standing in front of — the
`node_id` every deployment, link and routing target is keyed by, and the
hostname, which is *not* unique, because a worker in a `--network host`
container reports the host's. The four `Stat` readouts are always now, whichever
window is selected, and the page says so in words underneath them.

## `WindowChips.tsx`

`WindowChips({ value, onChange })` renders `HISTORY_WINDOWS` — `live`, `5m`,
`1h`, `24h` — as `aria-pressed` buttons. Twenty-six lines, and the argument is
in the docstring rather than the code: **`live` is a different kind of answer,
not just a shorter one.** It is the 1 Hz frame accumulated in this tab, so it
is always current and never survives a reload; the other three come off the
coordinator's archive, survive a restart, and can report that part of the
window is missing. The page says which it is showing rather than letting the
chips imply a smooth continuum, which is what `Provenance` exists to spell out.

## `NodeCharts.tsx`

`NodeCharts({ node, window })` draws power, temperature, `utilLabel(profile)`
and memory. Not new traces — the dashboard's telemetry sub-tab has drawn
exactly these four per Spark since it existed. What is new is that they are on
the machine's own page and can be read from the archive rather than only from
the sixty seconds this tab happens to have seen.

`ChartGrid`, not a plain div: one crosshair across all four, so a spike in
power can be read against the temperature, utilisation and memory of the same
instant instead of four separate guesses at where the eye was.

Two details are load-bearing. The percentage denominator is
`node.profile.total_memory`, used only as a fallback — a raw sample carries its
own `memory_total` but a rolled bucket does not, and this is the exact
denominator `serialize.node_payload` divides by, so the chart and the quad
above it cannot disagree about what "percent" means. And `envelope` is `null`
on `live`: only the archive knows where its holes are, so only the archive gets
to draw hatching over them. `bandLabel` is `'peak'` and `lowLabel` is `'avg'`,
because the axis line reads `min 6 avg … max 61 peak` and the two halves are
true of *different* series — the lowest bucket average, and the highest reading
inside any bucket. Calling the upper one "max" made it stutter to `max 61 max`.

## `Provenance.tsx`

`Provenance({ window, resource })` sits under the charts and answers where the
numbers came from. **A flat line has two causes** — the machine was idle, or the
rows were never collected — and this product already refuses to blur that
distinction for a link it has never measured, so it refuses to blur it here.

Four facts, in one sentence each: which resolution answered (the coordinator's
five-minute ring, one-minute buckets, one-hour buckets, or raw samples, off
`bucketSeconds(env.resolution)`); whether `env.durable` means it survives a
restart or "no archive is being kept on this coordinator"; whether
`env.truncated` dropped the oldest end; and every entry in `env.gaps` named
with its reason and its length in minutes, "known missing rather than quiet".

On `live` it says so plainly — accumulated in this browser since the page
loaded, starts empty, a reload loses it. On an error it renders the server's
own sentence through `Verbatim`: telemetry is off when its data root does not
exist, which is how the container records and a development machine does not,
and the 503 says exactly that better than anything written here.

## `HardwareRows.tsx`

Nine rows: GPU, compute capability, driver, addressable, allocatable now, in
use, host memory free, memory bandwidth, last heard from.

**`allocatable` is read from `/api/memory`, never recomputed.** The memory half
of this used to be `addressable - used`, guarded to unified memory only, which
meant every discrete node showed an em dash for the one number that decides
whether anything can be launched on it. `/api/memory` has carried a real
`allocatable` per node all along, from the fit gate itself, and
`useMemoryReport()` already polls it every two seconds for the Serve button.
A second copy of a figure is a second answer, and the one that disagrees with
the fit gate is the one that gets somebody an OOM.

`noGpu` (`gpu_count === 0`) is its own shape rather than a set of zeroes. A
machine with no GPU has no addressable memory to be a fraction of, and the four
GPU rows used to read as three blank cells and `0.0 GiB · 0.0 GiB usable at the
0.90 guardrail` — a card with an empty pool rather than a board with no card.
Now the device class answers the first row, `addressable` is an em dash, and the
"in use" row is relabelled "host memory in use" and divided by
`node.memory_total` (`inUseTotal = p.addressable_memory || node.memory_total`).

"Last heard from" prints `last_seen` *and* `sample_ts`. The health check and the
telemetry sample are different clocks, and a node whose agent answers forever
while `nvidia-smi` is gone is the exact case that made a sample-staleness
threshold necessary — so both are shown and the difference between them is
visible.

## `Interconnect.tsx`

`Interconnect({ nodeId })` lists every peer in the roster and, per peer, either
the measurement or the words "never measured", and in both cases a button —
labelled Measure, or Re-measure once there is something to replace. **An
unmeasured pair carries no numbers at all** — not an estimate, not a zero, not
a greyed-out figure from a different pair. That is `SelectionRail`'s rule
unchanged: `edgeMeasured(edge)` requires `measured === true` *and* a non-null
`all_reduce_gbps`, and everything else prints "never measured".

Nothing new is fetched. `useCluster()` is already polled every five seconds for
`links` (latency and `measured_at`), and `/api/topology`'s edges carry the
`measured` and `stale` flags; `findEdge` and `findLink` match either
orientation, since a link is undirected and the archive may hold it either way.
Measuring is `POST /api/links/measure` with `{a, b}` followed by `invalidate()`,
and a failure renders the gateway's own message in one line under the peer list
rather than as a page-level banner. The state is `{ peer, message }`, but only
`message` is drawn — `peer` is carried and not yet shown, so with several peers
the sentence does not say which button produced it. A single-node cluster gets a
sentence, not an empty list: "The only machine in the cluster, so there is no
link to measure."

## `RenameNode.tsx`

**The name is a caption and nothing else.** `node_id` is the key every
deployment, link measurement, routing target and saved graph arrangement is
written against, and it does not move — which is why the id stays on screen in
the header above this field and in the helper line below it. A rename that hid
the id would leave an operator unable to match a plate against the link chips,
which is the confusion the feature exists to remove rather than relocate.

A name is needed because hostname is not unique: a worker in a `--network host`
container reports the host's, so a two-container Spark shows two machines
calling themselves the same thing.

The `seed` ref is the non-obvious part. This page stays mounted while the
five-second poll refreshes underneath it, so without comparing `{nodeId, label}`
against what the draft was seeded from, a rename would either be reverted
mid-edit by the next poll or, worse, carried across to the next machine the
operator opens. Empty saves as "no name" through the same `commit()` path as a
rename — a control that could set a name but never clear one is a trap, and
giving Clear its own request would give it its own, missing, error handling.
`maxLength` is `MAX_LABEL_LEN` (48), mirrored from
`control_plane/registry/labels.py`; the gateway's own sentence is what renders
on a refusal, because it is the thing that knows the limit.

## `ServingBlock.tsx`

One model this machine is serving: lamp, the served name as a button, the plan
line, four readouts, three charts, and — on an archived window — request totals
and real percentiles.

**Everything about routing stays in the deployment inspector.** Which targets
exist, their shares, their circuit state, what each costs: this block is about
the model as *this machine* experiences it, and the served name is a button
into the other surface (`selection.openSheet({ kind: 'dep', id: served_name })`)
rather than a duplicate of it.

The window switches the source, not just the length. On `live` the three charts
are `useTelemetrySeries()`'s per-deployment rings and the third is "Queued"; on
an archived window they are `depSeries(h, …)` and the third becomes "Failed",
with the TTFT chart relabelled "p50". The percentiles under them are merged
bucket-wise from the archive's histograms — an hourly p99 is that hour's p99
and not a mean of sixty of them — and taken from the busiest bucket in the
window rather than summed, because a percentile cannot be added. The four
readouts above are moving averages from the live frame, and the block says so.

## `RequestsTable.tsx`

**Queried without a served-name filter and narrowed here by `node_id`.**
`useRequestHistory('', window, 500)` asks for everything and
`h.requests.filter(r => r.node_id === nodeId)` picks out what ran here. The
route filters by served name and target, never by node, so filtering
server-side would mean one query per model and a cap on how many models a page
can cover — and it would still be the wrong set, because a request to a model
this machine serves may have been answered by a remote provider under the same
name. `node_id` is on every raw row and it means exactly "ran here".

Three states are said in words rather than shown as an empty table, because an
empty table reads as "this machine served nothing". On `live` the frame carries
rates rather than requests. Past raw resolution — `bucketSeconds(h.resolution)
!= null` — the archive keeps buckets and there is nothing to list, with a
pointer to the percentiles beside each model, which are computed from every
request in the window. And when the filter comes back empty there is no table
to foot, so the sentence itself carries `elsewhere`, the count that ran on other
machines — "The window holds N that ran elsewhere in the cluster" against "The
window holds none at all", so "none here" stays distinguishable from "none at
all". The footer says the same thing when there are rows.

`SHOWN` is 50, one row per attempt, newest first, with the cap disclosed in the
footer alongside `h.truncated`. A token count marked `est.` was counted from
stream frames rather than read from an upstream usage block. `clock()` is wall
time, not "3m ago", because these rows get read against the events and log lines
beside them and two different relative clocks do not line up.

## `ResidentProcesses.tsx`

What is actually holding the GPU, whether or not derate launched it.
`nvidia-smi --query-compute-apps` is the only memory number GB10 will give you,
the fit gate plans against it, and until this card nothing in the product could
say what was behind it: a leftover `llama-server` holding 72 GiB read as 72 GiB
of headroom gone with no deployment to explain it, and the only way to get it
back was a shell.

**A process derate launched is deliberately not killable from this list.**
`gateway/gpu_procs.py` sets `killable=False` and fills `not_killable_reason` for
any process it can match to a live deployment. Killing a backend the router is
still dispatching to would leave the deployment claiming READY while the process
is gone, and the operator who clicked would see 502s with nothing connecting
them to their own click. Those rows render a `↗` that opens the deployment
instead, whose Stop drains first.

The confirm names the consequence in full: what is being killed, its pid, the
GiB it is holding, "SIGTERM first, then SIGKILL if it has not exited in 10
seconds", and that anything it is serving stops immediately. The result renders
`result.detail` through `Verbatim` — the server is the only thing that knows
whether SIGTERM was enough and whether the driver actually released. `busy` is
keyed by pid so one row's kill does not grey out the others.

## `NodeRuntimeCard.tsx`

A model runtime already listening on this node, and the one click that turns it
into a route target. The gap it closes is a sentence the Models tab was already
showing — a machine with no GPU cannot carry a rank, but it can run a small
model and be routed to. That is correct and it is three steps, two of which are
facts the coordinator already holds: the node's address and the port the
runtime listens on. Only installing it needs a human on that machine, so the
other two happen here, over `GET`/`POST /api/nodes/{id}/runtime`.

**Deliberately silent when there is nothing.** `if (!detected) return null`.
Most machines are not running a runtime and a GPU node has no reason to, so a
card saying "no runtime found" on every node sheet would be noise on all of
them to be useful on one. `load()` swallows its own failure for the same
reason: a probe that could not run is not worth a red box, because this card is
an offer and the absence of an offer is a fine outcome.

**Nothing is adopted automatically.** A provider is a routing target, and one
that appeared without anybody choosing it is a request going somewhere nobody
meant — the same position the roster takes about a discovered node, where
discovery proposes and a human accepts. Adopted models arrive switched off, and
the card says so in both branches.

`pull()` goes through `POST /api/providers/{id}/pull`, the provider pull path,
deliberately rather than a second one of its own: it is the thing that weighs
the download against the machine's measured free memory, and a pull that
skipped that gate would be the one filling an SD card nobody is watching. It
accepts any name the runtime understands, which is a wider set than the Models
tab offers — that screen keeps one namespace and addresses everything as
`hf.co/<repo>:<quant>`, so a name from the runtime's own library cannot be said
there at all.

`freeAndRetry()` is scoped by `errorCode(e) === 'pull_over_memory'`, read off
`ApiError.body`, which keeps the untouched envelope for exactly this. It is the
second of the three ways out the refusal names, and the card is uniquely placed
to act on it because it already knows what is holding memory and already has
the control to release it. It **does not** promise the retry will succeed: what
is freed is known, whether it is enough is the gate's call, and claiming
otherwise here would be doing the gate's arithmetic in a place that cannot see
the budget fraction. `resident === null` is a third state, "residency unknown" —
loaded and downloaded are different, and on a machine this size the difference
is what decides whether anything else runs.

## `EventsAndLogs.tsx`

Two `Box` panes — events and log — sharing one frame so they read as one
instrument. **`SHOWN` is 40, and the footer says so.** The archive is asked for
200; 40 is what fits in `.logbox .body`'s 320 px before scrolling stops being
reading and starts being scrubbing. A capped list that does not say it is
capped reads as a complete one, so the footer prints `40 of N shown, newest
first` and appends the archive's own truncation notice when `resource.data
.truncated` is set.

**Deliberately narrow.** One node's recent lines over the window already chosen
for the rest of the page — not a cluster-wide searchable log surface. There is
no query box and no logger control: the two things that turn a diagnostic strip
into a log browser are exactly the two left out.

The level chip defaults to `WARNING`, and that default used to be the only
thing keeping the section readable — it was not enough. "Everything" could only
ever show forty `uvicorn.access` lines, because the coordinator polls each node
at 1 Hz and, measured on a three-node cluster, those polls were **99.6% of the
log stream and 81% of every row in the archive**. They are no longer recorded
(`NOISY_LOGGERS` in `control_plane/telemetry/config.py`, overridable with
`DERATE_TELEMETRY_QUIET_LOGGERS`), so the chip now does what it says.

On `live` both boxes are replaced by one sentence: events and log lines are
recorded, not streamed to this page. Messages render through `Verbatim` — a log
line paraphrased is not a log line, and these are already redacted by the
handler that shipped them. `module:lineno` goes in the row's `title` attribute:
it is what you want once you have decided a line matters, and noise on every
line until then.

## `NodeLogFiles.tsx`

`NodeLogFiles({ nodeId })` tails `node.log` or `proxy.log` — the control
plane's own process log (`control_plane/logfiles.py`), not a served model's
stdout (`DeploymentLog`) and not the structured archive (`EventsAndLogs`
above). `GET /api/nodes/{id}/logs` proxies to the node agent's own
`GET /agent/logs`, the same shape `_agent_processes` already uses to reach a
node directly.

**Same rule as `EventsAndLogs`, extended to a new surface.** A file toggle and
a line-count choice are the only controls — no query box, no logger filter.
Read on demand and refreshed by hand, like `DeploymentLog`: a file tail has no
"is this actively streaming" signal to poll against.

Not yet wired into `NodeInspector` itself — its only caller today is
`tabs/settings/InstanceCard.tsx`, which renders it alongside this folder's
other panels for the node/deployment an operator picks there.

## `Terminal.tsx`

`NodeTerminal({ nodeId })` is xterm over a WebSocket to
`/api/nodes/{id}/shell`, and it is the last thing in the right column — the
escalation from everything above it, when the recorded lines did not say enough.

**The one dependency this UI takes that it could not have written, and it is
loaded on demand.** Rendering a pty means implementing VT100 — alternate screen
buffers, scroll regions, wide characters, mouse reporting — and the version of
that which fits in a few hundred lines renders `top` and `vim` wrong, which is
most of what a node shell is for. So `open()` `await`s three dynamic imports
(`@xterm/xterm`, `@xterm/addon-fit`, and the package's CSS), which makes Vite
emit them as their own chunks: `ui/dist/assets/xterm-*.js` is 332 kB on disk
beside a 5 kB `xterm-*.css`, fetched the first time somebody opens a shell and
never otherwise. The type-only `import type` lines at the top are erased at
compile time and pull in nothing.

**No terminal byte goes through React state.** xterm owns its own DOM; this
component owns the socket and gets out of the way. `socket.onmessage` writes
straight through with no decode, because an escape sequence can split across
two frames and anything trying to be helpful about encoding would corrupt the
stream. The chat transcript's approach — re-mapping a turn array per delta
frame — is fine at token rates and would melt at the throughput of a `find /`.

`keyIsCarriable()` tests the key against the RFC 6455 token grammar before
opening anything. The key travels as the second WebSocket subprotocol after
`derate-shell` (the same constant as `control_plane/registry/shell_route.py`
and `gateway/shell_api.py`), and a key containing a space, comma or quote
cannot be carried in one at all — checked here so the answer is a sentence
rather than a handshake that fails for no stated reason.

`readTheme()` reads `--panel`, `--ink`, `--panel-sunk`, `--fault`, `--live`,
`--warn` and `--flow` off the live computed style, because xterm needs literal
colours and this page is themed with custom properties that move under
`prefers-color-scheme` and the theme switch. Without it the terminal is a black
rectangle dropped into a cream panel.

The privilege sentence is deliberately hedged and that is the point: what you
get is whatever user the node agent runs as, and whether it reaches the host
depends on `--pid=host`. Asserting the worst case would be wrong on half the
fleet, and this product's rule about not rendering a figure it did not measure
applies to a claim about privilege at least as much as to a number. The prompt
says which once it opens. The warning about the connection being unencrypted,
and about SSH keys the node's container mounts, is not hedged.

## The seam with the rest of the UI

`NodeInspector` itself is imported by exactly one file, `shell/Sheet.tsx`,
which resolves `sheet.kind === 'node'` against the polled `cluster` roster and
hands the row down. Its children are not so exclusive any more:
`tabs/settings/InstanceCard.tsx` imports `ServingBlock`, `RequestsTable`,
`ResidentProcesses`, `NodeRuntimeCard`, `EventsAndLogs`, `DeploymentLog` and
`NodeLogFiles` directly, so an operator can see the same panels from Settings
without opening the sheet. Every one of them takes plain props and reads
shared app context (`useBackend`, `useSelection`, `state/history.ts`'s hooks)
rather than anything scoped to `NodeInspector`, which is what makes this
possible without change to any of the seven.

Everything else crosses outward:

- **`state/history.ts`** is the window vocabulary and every archived read.
  `HistoryWindow`, `HISTORY_WINDOWS`, `windowLabel`, `bucketSeconds`,
  `resolutionNote`, `nodeSeries`, `nodeBand`, `depSeries`, `requestTotals`, and
  the four hooks `useNodeHistory`, `useRequestHistory`, `useNodeEvents`,
  `useNodeLogs` — plus `useShellStatus`, polled lazily at 60 s purely to notice
  a coordinator restart, because the shell flag is read once at startup by
  design so that nothing arriving over the network can switch the route on.
- **`state/resources.ts`** supplies the shared polls this folder rides rather
  than duplicating: `useCluster` and `useTopology` at 5 s, `useMemoryReport` at
  2 s, `useNodeProcesses`.
- **`state/backend.tsx`** is every write this folder makes. Four of them
  invalidate the shared polls, because what they changed is on a screen other
  than this one: `measureLink`, `renameNode`, `killProcess`, `adoptNodeRuntime`.
  `setRuntimeModel` and `pullToProvider` re-read only the card that issued them,
  by calling its own `load()` — a resident model and an accepted pull are on the
  wire nowhere else. `nodeRuntime` is a read, not a write: the probe behind
  `load()`.
- **`tabs/cluster/layout.ts`** hands over `runners()`, `runningOrPrevious()`
  and `edgeMeasured()`, so the node page, the Settings instance picker and the
  cluster floor cannot disagree about what is running, what ran here last, or
  about what counts as a measured link.
- **`tabs/dashboard/Chart.tsx`** supplies `Chart` and `ChartGrid`; the four
  node traces and the three per-deployment traces are the same component the
  dashboard draws.
- **`state/selection.tsx`** is the only way out: `openSheet({ kind: 'dep', id })`
  from `ServingBlock`'s served name and from `ResidentProcesses`' `↗`.

The layout lives in `styles/derate.css`, not inline — `.nodegrid`, `.nodehead`,
`.logbox` and its `h4` / `.body` / `.foot` / `.logline`, and `.termbody`.
`.termbody` is a fixed height rather than a max-height on purpose: xterm
measures its container to decide how many rows to ask the pty for, and a
container that sizes to its content has no height to measure until there is
content, which is a chicken-and-egg that renders as one row. That file declares
`.termbody` twice — 320 px in the log-pane block, 340 px in a later block of its
own — so 340 px is what renders, and editing the first one changes nothing.

## Things that look like details and are not

**The four header readouts are always now, whichever window is selected.** What
the machine is doing at this second is a different question from what it did
over the last hour, and the page says that in words under the quad rather than
letting the chips appear to govern figures they do not.

**`resolutionNote` and `envelope` are suppressed on `live`.** The five children
that read a series take `window` — `NodeCharts` (and `Provenance` under it),
`ServingBlock`, `RequestsTable`, `EventsAndLogs` — and each branches on
`fromLive` before deciding what to draw and what to disclose. The other five
panes have no window to be wrong about. The live ring has no memory of having
missed anything; only the archive can honestly report a hole.

**`useRequestHistory` is keyed by served name, window *and* limit.**
`requests:${servedName}:${window}:${limit ?? ''}`. `ServingBlock` asks for one
model with no limit of its own and `RequestsTable` asks for every model at 500,
so on this page the two names already differ; the limit is in the key so that
two asks differing *only* by limit cannot share an entry and hand one caller the
other's cap.

**Two `busy` maps are keyed rather than boolean.** `ResidentProcesses` keys by
pid and `NodeRuntimeCard` keys by model name, so acting on one row does not
grey out the rest of the list.

**`clock()` is duplicated in `EventsAndLogs` and `RequestsTable`, deliberately
as wall time.** Both carry the same reasoning: the two lists get read against
each other, and two different relative clocks do not line up.

**Every server refusal is rendered as the server wrote it.** `Verbatim` carries
`node.ineligible_reason`, `Provenance`'s 503, the kill result's `detail`, the
runtime card's error, `EventsAndLogs`' log messages and the terminal's close
reason. Three others take the same sentence unchanged through a fault-coloured
element rather than through `Verbatim` — the rename error, in place of the
helper line under the field; the measure error; and the kill error. The gateway
is the thing that knows the limit, the threshold, or which of two states the
machine was in.

## Failure behaviour

- **The node is not in the roster.** Resolved one level up, in `Sheet.tsx`;
  nothing here renders against a missing node.
- **`/api/memory` has no row for this node.** `HardwareRows` falls back to a
  0.9 guardrail for the "usable" arithmetic and prints an em dash for
  `allocatable now`; `nodeSignal` receives `undefined` and falls back to its own
  local memory-pressure threshold rather than claiming the server said anything.
- **The node runtime probe fails.** `load()` catches, sets `state` to `null`,
  and the whole card disappears. An offer that cannot be made is not an error.
- **`/api/history/*` is unavailable.** `Provenance` renders the server's own
  503 sentence; `RequestsTable` and each `Box` render their own resource's
  error through `Verbatim`. One failing pane does not take the page down.
- **The window holds no rows.** Every list says which kind of nothing it is:
  "Nothing happened on this machine in this window", "Nothing at warning or
  worse. Switch to everything for the rest", "No request in this window ran on
  this machine" plus the count that ran elsewhere.
- **A link measurement fails.** The gateway's message renders in one line under
  the peer list and the other peers keep their buttons. `error.peer` is stored
  and not drawn, so with several peers the line does not yet say which button
  produced it.
- **The shell is switched off.** `useShellStatus()` reports `enabled: false`
  and the terminal renders the server's reason, or the fallback "Set
  DERATE_SHELL=1 on the coordinator and on the node to turn it on."
- **The xterm chunk cannot be fetched.** Phase returns to `closed` with a
  sentence saying the terminal is fetched separately the first time it is
  opened, so this is usually a network problem between here and the
  coordinator.
- **The shell socket closes without a reason.** Any code other than 1000 or
  1005 gets "The connection closed before a session started. The node may have
  the shell switched off, or the key may be wrong." A refusal happens before
  the socket is accepted, so `event.reason` is how the node's own sentence
  reaches the page, and it wins when there is one.
- **The sheet switches nodes.** The `useEffect` keyed on `nodeId` closes the
  socket and disposes the terminal. Without it, switching machines leaves the
  previous socket open — and on the far side that is a shell nobody is looking
  at any more.

## Deliberately not built

**Routing controls.** Targets, shares, circuit state and cost belong to the
deployment inspector. `ServingBlock` links there rather than growing a second
copy that could disagree.

**A throttle slider.** The original mockup had one; there is still nothing on
the wire to throttle, so there is still no control.

**A cluster-wide log browser.** `EventsAndLogs` is one node's recent lines over
the page's window. No query box, no logger picker — those are the two things
that would turn a diagnostic strip into a different product. `NodeLogFiles`
adds a second log surface, the raw files, under the identical rule: a file
toggle and a line-count choice, and nothing that searches.

**A kill for anything derate launched.** `gpu_procs.attribute()` sets
`killable=False` on any process it can match to a live deployment, and
`DELETE /api/nodes/{id}/processes/{pid}` refuses one with a 409
`process_is_managed` — so the card would not offer the button and the request
would fail if it did. The deployment's own Stop drains first, and that is the
only correct order.

**An automatic provider adoption.** The probe reports; a human clicks. A
routing target that appeared without anybody choosing it sends requests
somewhere nobody meant.
