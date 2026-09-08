# inspectors

The deployment detail pane, and the folder the node page hangs off. Two of the
three subjects that open a sheet are drawn from here, and both are drawn as a
*page* rather than a card: `shell/Sheet.tsx` gives a `node` and a `dep` sheet
the class `card full`, and `card` on its own sets no max-height, because every
long inspector is now one of the wide sizes.

`shell/Sheet.tsx` owns only the mechanics every use of the sheet needs — the
scrim, the body scroll lock, the focus trap that survives a content swap. Each
inspector here owns its own header row: the lamp, the label, and Close.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `DeploymentInspector.tsx` | 546 | what a launch is doing, and what a serving model is doing — the whole deployment sheet |
| `DeploymentLog.tsx` | 155 | the backend's own log, in its own sticky column of that sheet |
| `node/` | 13 files, 1890 lines | the node page, rendered as sheet contents. Its own README |

## `DeploymentInspector.tsx`

`DeploymentInspector({ dep, cfg, nodes, frame, stale, settings, onClose })` is
the sheet a deployment opens on. Two columns, laid out by `.depgrid`: the left
is the deployment, the right is `DeploymentLog`. Below the header line it draws
either the `Starting` block or the latency-and-throughput block — never both —
then targets, placement with the planner's verbatim reason and rejections, the
fit gate's warnings, and a memory bar per node it was placed on.

The header's node names come from `nameIndex(nodes.map(fromState))`, the same
naming rule the graph and the roster use, so this sheet's node list cannot
disagree with the plates it is describing.

### `Starting` replaces the readouts, it does not sit beside them

**A deployment that has not started cannot be serving at zero tokens a second.**
The latency block had nothing to say about an arriving launch, and said it five
different ways at once: `0 tok/s aggregate`, `— tok/s per stream`, `— ms to
first token`, `0 queued now` and "Not yet observed." — five ways of reporting
silence about a model that is in fact busy, for up to half an hour, doing four
different things in sequence. Nothing on the sheet distinguished "starting"
from "stalled". `Starting` takes the block's place whenever `dep.state` is
`planned` or `launching`, which is the same allowlist `/api/activity` uses:
`degraded` is up and serving badly, `stopping` is leaving, and neither is
arriving.

Everything in it is read, not inferred. The server classifies the launch in
`control_plane/deploy/progress.py` from sparkrun's output and the backend's own
log; this draws what it says, over the `LAUNCH_PHASES` ladder from
`state/launchPhase.ts` — `preparing`, `downloading`, `loading`, `starting`,
`serving` — which the first-run wizard's stepper walks too, from the same
module, so the two screens cannot drift into two vocabularies. When the server
has not named a phase, `phaseRank` returns -1 and every step draws as ahead,
which is true and better than ticking one on the assumption that a launch which
has said nothing must at least have started.

The `ProportionBar` is filled only while the runtime is counting its own
checkpoint shards. The pull, the download, the compile and the CUDA-graph
capture report no denominator, so the track is drawn open rather than filled at
a rate this screen would have had to invent. The same rule governs the ETA:
`remainingLabel(row.eta_s)` is rendered with whose count it is — the
downloader's or the runtime's, of the step it is on and not of the whole launch
— and the other two steps get "No estimate: nothing reports a total for this
step." The elapsed line says "Seen starting", not "launching for 4m", because
`row.since` is when *this coordinator* first saw it, and after a restart that
is when the coordinator came back.

### Arriving is amber

`Lamp` is `warn` for `degraded`, for either arriving state, and for `stopping`;
`live` only for `ready`; `fault` otherwise. `serving` is `ready || degraded`,
but the warn branch is tested first and takes `degraded` with it, so no
deployment ever reaches `live` while it is degraded. The expression used to
know only "serving" and "not serving", so a launch drew the fault lamp for the
whole of its startup — a model doing exactly what it was asked to do, reported
as broken, beside a Stop button. The rail has always drawn this state as warn.

### The numbers are wire values or a physics derivation of them

`deriveLocalCost(powerW, decodeTps, rate)` prices local generation the only
honest way available: `energy per Mtok (kWh) = power_w * (1e6/tps) s / 3.6e6`,
times the rate a human entered in Settings. It is a unit conversion, not a
fitted constant, and it returns `null` — never a fabricated number — whenever
any input is missing. `TargetRow` sums power over *every* node behind the
target rather than `node_ids[0]`, because a pipeline target spans more than one
machine and pricing off the first silently under-counts the draw. A published
`cost_per_mtok` always wins; a derived figure carries its own provenance line
underneath, `from N W at N tok/s · $R/kWh`.

`isAudio(dep.modality)` — `speech` or `transcription` — suppresses every
token-denominated readout with a sentence saying why. They are absent rather
than zero: the control plane measures no audio-side rate, and a 0 would read as
a stalled deployment.

Stop confirms with the consequence, not just the question: "It stops accepting
new requests immediately; requests already in flight finish", which is what
`set_draining` followed by `stop` actually does. The disabled state is read off
the wire (`alreadyStopping`, which is `dep.state === 'stopping' ||
dep.state === 'stopped'`), not off local `stopping`, so a reload part-way
through a stop still shows the truth.

## `DeploymentLog.tsx`

`DeploymentLog({ deploymentId, autoOpen })` puts the launcher's output and then
the container's into one `<pre>`, which is one story to whoever is reading it.
It was a `<details>` at the bottom of a single column — the answer to "what is
it actually doing" placed below everything else on the sheet and behind a
click, during the one time anybody opens this sheet at all. It is now the right
column, sticky against the top of the card, so it holds its place while the
left column scrolls.

**The cost of the answer decides how it is asked for.** `GET
/api/deployments/{id}/logs` reports its own `source`. `buffer` is the
coordinator repeating lines it is already streaming — free, and polled here on
a 2000 ms `setTimeout`. `read` is one bounded `sparkrun logs` against a
deployment nothing is following any more; that command tails *and follows*, so
it has to be cut off rather than waited out, and putting it on a timer would
mean a subprocess every two seconds for as long as the sheet is open. It is
fetched once and refreshed by hand. `unavailable` is a third answer entirely —
`internal_api.py` returns it when the deployments port has no callable
`log_tail` — and it renders as "This control plane cannot read a backend log.",
which is not the same fact as "the backend printed nothing".

That cost is why `autoOpen` exists. The inspector passes `dep.state !==
'stopped' || dep.last_error != null`: free to show whenever the coordinator is
still following, and one click away with the reason said out loud when it is
not — unless the deployment left a reason behind, which is exactly when
somebody opened the sheet to read the log.

`followRef` samples whether the reader is at the bottom (within 24 px) on every
scroll event, and the tail-follow effect obeys it. A log that scrolls itself is
right until somebody scrolls up to read something, at which point following the
tail drags them away from it mid-sentence. `autoOpen` opens the pane and never
closes it: a log somebody asked for does not close itself under them.

## `node/`

Thirteen files and 1890 lines behind one export, `NodeInspector`, with its own
README beside this one. It is a folder rather than a file because the node
sheet is twelve panes composed into two columns — charts, provenance,
hardware, links, rename, a serving block per model, requests, GPU processes, a
detected runtime, events, log, terminal — and each one holds its own resource,
its own window and its own failure. The only thing that crosses back this way
is `openSheet({ kind: 'dep', id: served_name })`.

## The seam with the shell

`shell/Sheet.tsx` is the only importer of anything in this folder, and it takes
exactly two names:

```tsx
import { NodeInspector } from '../inspectors/node/NodeInspector'
import { DeploymentInspector } from '../inspectors/DeploymentInspector'
```

`SheetBody` resolves `sheet.kind`, finds the row in the polled `cluster` /
`routing` data, and hands it down. Nothing here fetches its own deployment or
node: `dep`, `cfg`, `nodes`, `frame`, `stale` and `settings` all arrive as
props from resources the shell already polls. The two things this folder does
fetch for itself are the ones no other screen needs — `useActivity()` for the
launch phase, and `backend.deploymentLogs()`.

Both directions of navigation are `selection.openSheet`. `node/ServingBlock`
and `node/ResidentProcesses` open `{ kind: 'dep', id: served_name }` into this
inspector; this inspector deliberately does not open a node sheet back.

The layout is `styles/derate.css`, not inline: `.depgrid` (two even columns,
collapsing at 900 px), `.deplogcol` (`position: sticky; top: 0`, which works
only because `.depgrid` sets `align-items: start`), and `.logbox pre.body`
(`max-height: min(62vh, 720px)`, taller than `.logbox .body`'s 320 px, because
the log has a whole column to itself).

## Things that look like details and are not

**`Starting` is mounted only while the deployment is arriving.** That is what
keeps `/api/activity` — polled at 2 s — off this sheet for the READY case,
which is most of them. Rendering it unconditionally and hiding it with CSS
would cost every open sheet a poll it has no use for.

**An unrecognised phase changes nothing.** `phaseRank` returns -1 for a name
this build has not heard of, and that is not an error: the server can name a
phase a deployed UI predates, and the honest response is to show its verbatim
sentence and tick nothing rather than fail to render the row.

**Only `source === 'buffer'` reschedules itself.** The `read` branch falls out
of `load()` without setting a timer, and the Refresh button bumps a `nonce` in
the effect's dependency list to run it again. Polling the `read` path would
spawn a `sparkrun logs` subprocess on the machine every two seconds.

**Planner and fit strings go through `Verbatim`.** `dep.plan.reason`,
`dep.plan.rejected` (via `VerbatimList`), `dep.fit.warnings`, `dep.last_error`
and `row.status` from the activity row. The two failures are the exception:
`stopError` and the log's own fetch error render in a fault-coloured
`<p className="label">` rather than through `Verbatim`. They are still the
sentence the server wrote, and nothing in this folder truncates, re-cases or
summarises any of them.

**The `stopError` is the server's sentence, not a category.** The catch stores
`e.message` and renders it under the header. The gateway is the only thing that
knows whether the stop failed in `set_draining` or in the launcher.

## Failure behaviour

- **No activity row for this deployment.** `Starting` renders the whole ladder
  with nothing ticked and says "Nothing has been read from this launch yet."
  The bar draws open.
- **`row.fraction` is not finite.** Treated as absent; the bar draws open.
  Finite values are clamped to `[0, 1]`.
- **The log request throws.** The error replaces the `<pre>` and the poll stops
  — `load()` sets `error` and does not reschedule, so a failing endpoint is not
  hammered every two seconds.
- **The sheet closes or the deployment id changes mid-fetch.** The effect's
  `live` flag drops the answer and `clearTimeout` cancels the pending poll,
  so a stale response cannot land in a pane that has moved on.
- **A target has no node behind it.** `powerW` is `null`, `deriveLocalCost`
  returns `null`, and the `$/Mtok` cell is an em dash. When the target is local
  and the electricity rate is unset, the cell says "set an electricity rate to
  price local generation" instead of a zero.
- **No `ttft_ms` or no `mean_duration_s`.** The prefill/decode split is
  suppressed with "Not yet observed." rather than drawn against a guessed
  boundary.
- **The node behind a memory bar is not in `nodes`.** That row renders nothing
  at all; a node with no live reading renders a `muted` bar and an em dash.

## Deliberately not built

**A dtype line.** It is not on the wire, so the sheet does not claim one.

**A Change-model swap.** The sheet stops a deployment; it does not mutate one
in place.

**Client-computed routing weights.** Share, Strength and `$/Mtok` are wire
values or the documented physics derivation of them. The pre-React mockup
computed weights in the browser off flat 0.55 and cost fixtures, and that
arithmetic is not reproduced here — the router is the only thing that knows
what it is actually dispatching.

**A per-tenant power share.** The wire carries one power reading per node, not
a share of it per deployment, so a node hosting two deployments is priced at
its full draw for each. There is no honest fix without a number nothing
measures, so it is disclosed in the provenance line under the figure rather
than hidden behind a division nobody can defend.
