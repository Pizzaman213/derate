# dashboard

Four sub-views of a cluster that is already running -- what is up, what it is
doing this second, what would still fit beside it, and who it is doing the work
for -- plus the chart layer the rest of the product borrows. The rule that
separates this folder from a monitoring page is that nothing here computes a
number: every figure on Headroom comes from the fit gate or the registry, every
figure on the other three comes off the metrics frame or the routing table, and
a browser that starts subtracting two of them is how a screen begins disagreeing
with the planner.

Planning is not here. The bar that used to sit above these tabs was a second
copy of `tabs/models/ServePanel.tsx` -- the same `POST /api/plan`, the same
`Verdict` box -- and it held the question in component state, so a plan asked
here could not be linked to; `tabs/DashboardTab.tsx` records the removal. That
is why the dashboard has no plan panel and why `Verdict.tsx` and
`DegreeFields.tsx` are under `tabs/models/`.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `Chart.tsx` | 437 | two renderings by two means: `Chart`/`ChartGrid` on uPlot, and `ChartCell`, a hand-drawn 24px SVG |
| `chart.check.mjs` | 270 | the hermetic verifier that pins "a hole stays a hole" at the renderer boundary |
| `HeadroomSub.tsx` | 247 | what can run here right now: per-node memory, and the fit gate's capacity table |
| `chart.ts` | 205 | the chart arithmetic, out of React and out of uPlot so it can be checked in node |
| `DeploymentsStrip.tsx` | 161 | one row per *running* deployment, with a spark cell, a stop and a `↗` to the detail sheet |
| `LoadSub.tsx` | 142 | requests by Spark, one column per local target |
| `TelemetrySub.tsx` | 119 | the 60-second live window: cluster, then the selected Spark, then the selected deployment |
| `AggregateRow.tsx` | 102 | the four overview tiles |
| `chartTheme.ts` | 85 | literal canvas colours, read live off the computed style |

## `Chart.tsx`

`Chart`, `ChartGrid` and `ChartCell`, and the two renderings are by different
means on purpose. The charts were hand-rolled SVG until the archive windows
made the hand-rolling the limiting factor: a 24-hour window is tens of thousands
of points, a rolled bucket carries an average *and* a maximum that one path
could not both draw, and four charts of one machine could not share a cursor.
uPlot is 45 KB of canvas that does all three, and -- the reason it is the right
library -- its `spanGaps` defaults to false, so a null lifts the pen without
being asked.

`ChartGrid` mints a sync key per grid, so hovering any chart puts the crosshair
at the same *instant* on its siblings and every readout answers for that
instant. That is the whole question a four-up of one machine is asked: what
were power, temperature, utilisation and memory doing at the same moment. Two
grids on one screen stay independent because the keys differ. The cursor is
x-only -- four charts of one machine are four different units, so a horizontal
line at "the same y" would mean nothing across them.

`Plot` is split out from `Chart` because the readout above it re-renders on
every mouse move and the uPlot instance must not. Data, size, y-range and gap
geometry all reach it through refs and imperative calls, so a hover costs one
canvas redraw rather than a teardown; the instance is rebuilt only on
`[width > 0, height, hasBand, syncKey, theme]` -- the first real width, the
shape, the sync group and the palette, and nothing else. Width is measured by a
`ResizeObserver` rather than assumed, because `DashboardTab` keeps its sub-tabs
mounted under `hidden` and a chart built while its tab is hidden is built at
zero width.

`ChartCell` is still SVG and should stay that way: a 24px trace in a table row
with no axis, no cursor and no band uses none of what uPlot brings, and the
deployments strip draws one per row, where a canvas, a DOM subtree and a
`ResizeObserver` each would be pure overhead. Both renderers take their nulls
from `chart.ts`, so the two cannot drift about what a hole is.

## `chart.check.mjs`

The verifier, and hermetic -- it carries no `// requires:` line, which
`ui/check.mjs` reads as needing nothing but the checkout. It bundles `chart.ts`
with the esbuild inside vite, for the same reason `router.check.mjs` does:
node's ESM resolver will not resolve an extensionless specifier.

It exists because `spanGaps: false` is a *default*. A default can change in a
minor version, and the promise it upholds is one this product makes in prose on
three different screens, so the promise is pinned at the boundary instead: what
`align` hands the renderer has a null wherever the data had one, and never a
value carried across. Six sections, each guarding one claim -- a hole stays a
hole (`null`, `NaN` and `Infinity` all arrive as nulls, and a timestamp one
series lacks becomes a null in that series rather than the other one's value);
uPlot's unvalidated precondition of a strictly ascending x, which it will
misdraw rather than complain about; an axis that is not fitted to noise;
"nothing measured" is not a measured zero; gap clipping; and the five changes
`seriesKey` must notice -- an appended sample, a trimmed head, the newest value
changing, the newest value going null, a wholesale replacement over a different
window -- plus the identical series that has to keep its key and the empty one
that has to return `'0'` rather than throw.

## `HeadroomSub.tsx`

What can run here right now, and every number on it is one the fit gate or the
registry produced. Nothing is recomputed in the browser -- that rule is why the
planner and this view cannot disagree, and it is the rule the old client-side
"Allocatable" row broke by subtracting two numbers itself. `DashboardTab` calls
it at 8192 context and 1 sequence.

The node card gives three distinct words to three distinct numbers --
Addressable, Ceiling (with the guardrail, and the note that this is what the fit
gate budgets against) and Allocatable now (with its binding limit, host memory
or the GPU ceiling) -- and carries a fourth row, Swap in use, which is not part
of that budget and is only there because swapping is how a node stops being able
to hand out what it says it can. The bug the three words replace was one word --
"Allocatable" -- over two figures. A node reporting no addressable memory gets
one sentence instead of four rows of `0.0 GB`, which read as a measurement
rather than an absence, and it is excluded from the capacity probe for the same
reason.

`cap.context` being null means nobody named a context and the gate chose one per
model; the caption then says so rather than printing whichever row's happened to
be first. A row that does not fit is dimmed to 0.62 opacity, never reddened: a
model that does not fit is excluded, not faulted, and red here would compete
with the real fault lamps. Unresolved models and excluded nodes render through
`Verbatim`, so the gate's own sentence survives intact.

## `chart.ts`

The arithmetic, kept out of React and out of uPlot so `chart.check.mjs` can run
it in node. `bounds`, `align`, `kindFor`, `yRange`, `gapSpans`, `gapAt` and
`seriesKey`.

`align` never interpolates. A timestamp one series has and another does not
yields a null in the second -- never a carried-forward value. It also produces
the ascent uPlot requires rather than assuming it of the caller, because the
archive returns rows in whatever order the query gave them, and it collapses a
duplicated timestamp last-write-wins, because a repeated x makes uPlot draw
backwards.

`yRange` refuses to auto-fit a bounded quantity. The hand-rolled chart scaled
every series to its own min..max, so a memory series sitting between 61% and 63%
filled the full height of the box and read as an event. A percentage is drawn
against 0..100 because that is what a percentage means; a rate is drawn from
zero; a `span` axis is widened symmetrically to `SPAN_FLOOR` (10) so a
thermally boring hour of 47.0-47.4 degrees is not stretched into a thermal
event. `PAD` is 0.08, so the peak is not welded to the frame. `kindFor` infers
the rule from the unit -- `%` is percent, `°C` is span (a chart of core
temperature drawn from 0 is 60% empty), and W, tok/s, ms and reqs are rates.

`gapSpans` turns the envelope's known holes into geometry, clipped to what is
being drawn. A gap wholly outside the window is dropped rather than clamped to
its edge: a marker hard against the frame reads as "the data stops here", which
is the claim `truncated` makes and this one does not.

## `DeploymentsStrip.tsx`

One row per *running* deployment -- name, plan, a 60-second spark of throughput,
the live number, and how many routing targets answer to it. Single click
selects, double-click or the hover button opens the detail sheet. "Running" is
`runners()`, which is everything not in `TERMINAL` -- exactly `failed` and
`stopped` -- so a `stopping` deployment is still on the strip, without its stop
button: it is winding down and still holding its machines.

`/api/deployments` is a ledger, not a roster: it keeps every attempt, so three
failed tries at one model are three rows carrying the same served name, the same
plan and a `0 tok/s` readout the frame never sent. The strip has no vocabulary
for "over" -- no band, no state column, and the stop button simply vanishes --
so a stopped deployment drew as an ordinary serving row. `runners()` is
`tabs/cluster/layout.ts`'s, which filters the same `TERMINAL` set
`models/rows.ts` uses, because the floor and this strip must not disagree about
whether a model is running. `stopping` is keyed by deployment id so one row's
stop never greys another's controls. A stop that throws still invalidates: the
reason belongs in the sheet, and a strip row has no room for a sentence but must
not swallow it into a silent no-op either. An audio deployment renders an em
dash rather than a confident 0 -- a speech or transcription server decodes no
tokens, so tok/s is not missing, it does not exist.

## `LoadSub.tsx`

Request load by Spark, with one column per **local target** and not per served
name. Two replicas of one model are separate deployments with separate counters
on separate nodes, and merging them stamped replica A's requests under replica
B's Sparks.

Attribution is by sharing rather than dividing. The gateway keys request stats
by `target_id`, not by physical node, and in pipeline parallel every request
traverses every node in the plan, so a deployment's completed count is stamped
under every node it spans, column totals legitimately exceed the request total,
and the note underneath says so in as many words rather than leaving the reader
to find the arithmetic wrong. `null` is never rendered as zero: a cell for a
target never dispatched to is an em dash, and a row total is null only when
every column that row spans is itself null. The footer says "since the gateway
started", because the counters do not reset and "today" would claim a reset that
never happens -- and it says "local Sparks", because this table has no rows for
what spilled to a remote provider.

## `TelemetrySub.tsx`

Nine series, because nine is exactly what the 1 Hz frame carries: two cluster
charts, four for the selected Spark, three for the selected deployment.
Drill-down rather than a wall -- the cluster charts are always on, the rest
render only for the current selection, and selection is shared with the
deployments strip and the cluster graph, so drilling in one place drills
everywhere. Picking a Spark from the chip row selects it everywhere and never
toggles off.

`NOTE` is the paragraph at the top and it has been corrected twice. "The control
plane stores no history" stopped being true when the telemetry package landed
durable journals on the coordinator. The second correction admits that
percentiles exist too -- a node's own page charts real TTFT and duration
percentiles from that archive -- because saying otherwise on a screen next to
one that shows them is worse than saying nothing. This screen is the live
window only: 60 seconds (`WINDOW_S` in `state/useTelemetry.ts`), accumulated in
the browser, empty on load, and its TTFT and mean duration are moving averages.

## `AggregateRow.tsx`

Four tiles -- tokens per second, deployments serving, watts drawn, GiB
addressable free -- and the interesting part of three of them is what they do
when they have nothing. The second is the exception and the one to read twice:
it counts `state === 'ready' || state === 'degraded'`, which is stricter than
the `runners()` the tile beside it uses, so a deployment still loading is
counted as running by `totalTps` and not by "deployments serving".

`totalTps` returns null rather than 0 when something is up and the stream has
not spoken about any of it -- an honest "no reading" beats a
confident zero the frame never sent -- but it counts over `runners()`, because a
cluster whose rows are all stopped or failed is not "no reading yet", it is
nought tokens per second, and before that filter the dead rows in the ledger
held the tile at an em dash for as long as the ledger kept them. `totalWatts`
applies the same rule per node: one that never reported power is skipped, not
treated as drawing zero, and only when every node is missing does the tile go to
an em dash. The fourth tile is `freeBytes` -- addressable memory less memory
used, summed across the roster -- because there is no slots model in this
product for a "slots free" tile to read.

## `chartTheme.ts`

uPlot draws to a canvas and so needs literal colours, but this product is themed
entirely with custom properties that move under both `prefers-color-scheme` and
the header's own theme switch. `readChartTheme()` reads `--ink`, `--ink-muted`,
`--rule`, `--panel` and `--warn` off the live computed style of the document
element, so nothing here invents a colour and every value keeps the contrast
that was measured in `tokens.css`. `--warn` is there for one thing only: the gap
hatch is `--warn` at 0.12 alpha, which is why a hole reads as a marked absence
rather than as another shade of the trace. The band fills are the *same* token at
reduced alpha -- 0.13 for the fill, 0.30 for its edge -- which is the "one hue,
two strengths" rule: an average and its bucket maximum are one quantity in two
aggregations, not two series. `alpha()` parses `#RRGGBB` and returns null for
anything else, so a token later changed to a `color-mix()` degrades to
`FALLBACK` instead of handing uPlot a string it will paint as black.
`subscribeChartTheme` watches the media query *and* `data-theme`, because the
switch's "system" setting removes the attribute and watching only the attribute
misses the case where system is itself in force.

## The seam with the inspectors

The chart layer is the only part of this folder imported from outside it.

- `inspectors/node/NodeCharts.tsx` takes `Chart` and `ChartGrid`.
- `inspectors/node/ServingBlock.tsx` takes `Chart`.
- `ChartCell` has exactly one caller, `DeploymentsStrip` in this folder.

`TelemetrySub` passes only `title`, `unit` and `points`. The props beyond those
exist for the two inspectors, which draw the archive rather than the live ring
-- `note`, `band`, `bandLabel`, `lowLabel` and `envelope`. `kind`, `height` and
`decimals` have no caller anywhere in `src/` today; they are the overrides the
unit inference, the 48px default and the zero-decimal readout would need if a
chart ever wanted something else.

```tsx
<Chart
  title="power" unit="W" points={avg}
  band={bands?.power}            // the bucket maximum, drawn over the average
  bandLabel="peak" lowLabel="avg"
  note={resolutionNote(h, power.length)}   // state/history.ts
  envelope={env}                 // its `gaps` become hatching
/>
```

`note` matters more than it looks: the default is `${n}s`, which is right only
at 1 Hz. A series read back from the archive at 1-minute or 1-hour buckets has
the same point count and a completely different span, and "60s" under an hourly
chart is simply false, so any caller drawing history passes `resolutionNote()`.

## Things that look like details and are not

**uPlot and xterm are the only libraries this UI depends on beyond React and the
fonts.** `ui/package.json` lists seven runtime dependencies: react, react-dom,
two `@fontsource` faces, `uplot`, and xterm's two packages. uPlot earns the slot
because the charts stopped being one line graph -- a rolled window carries both
a bucket average and a bucket maximum, four charts of one machine have to be
read against each other and so need one shared cursor, and a window can report
the stretches it knows it is missing. Recharts spans nulls by default, which is
the single thing this product refuses to do.

**`spanGaps: false` is written into the series options although it is also the
default.** It is the single property that made uPlot the right choice, so it is
written down rather than inherited silently -- and `chart.check.mjs` pins the
behaviour one level up, at `align`, so the guarantee does not rest on a library
default at all.

**The memo key is content, not array identity.** `nodeSeries` and `depSeries`
build a fresh array on every render, so identity dependencies would realign the
columns and push a full canvas redraw on every mouse move across the crosshair.
`seriesKey` is a heuristic and says so: it assumes a series either grows at the
end or is replaced wholesale, which holds because the archive is append-only and
the live ring is a queue.

**The gap hatching is painted in `drawClear`, before the series.** A hole is a
ground the trace is drawn on rather than a smear over it, and it is drawn even
where there are no samples on either side -- which is the case it exists for. A
flat line has two causes, the machine was idle or the rows were never collected,
and until the hatching existed the difference was a paragraph underneath rather
than a mark on the drawing.

**Hovering a hole reports the hole, not the nearest value.** `gapAt` decides,
and the readout becomes `missing · <the archive's own reason>`. The number the
old chart would have implied there came from the far side of the gap.

**Nothing on these screens renders `null` as `0`.** It is the same rule in five
places -- `bounds` returning null for an all-null series, `totalTps` and
`totalWatts` in the tiles, the `null` cells and row totals in `LoadSub`, and the
audio em dash in the strip -- and each one replaced a confident zero nobody had
measured.

## Failure behaviour

- **An empty or all-null series.** `Chart` draws "no samples yet" with an em
  dash in the value slot, not a flat line at zero. `ChartCell` needs at least
  two points and otherwise renders the same words.
- **A crosshair over a known hole.** `missing · <reason>` from the envelope.
  Over a null that is not inside a declared gap, `no sample`.
- **A chart mounted at zero width.** Construction waits; the `ResizeObserver`
  supplies a real width the moment the sub-tab is shown.
- **A theme token that is not `#RRGGBB`.** That derived colour falls back to
  `FALLBACK`'s value rather than being handed to uPlot.
- **`/api/memory` or the capacity probe erroring.** The message renders in
  `var(--fault)`; loading says "Reading memory…" or "Asking the fit gate…", and
  an empty answer says "No node reported memory." or "No answer."
- **Live capacity unavailable.** `Best` prints `largest right now: unavailable —
  <cap.unavailable_reason>` and the idle-hardware row still stands.
- **Nothing running.** "Nothing is being served." from the strip, from `LoadSub`
  when no row spans a column, and from the per-deployment section of
  `TelemetrySub`.
- **A stop that throws.** The strip invalidates and clears `stopping` in a
  `finally`, so the row's button goes back from a disabled `…` to `stop`; the
  reason surfaces in the detail sheet.

## Deliberately not built

**A plan panel.** Recorded in `tabs/DashboardTab.tsx`: the always-visible
planner bar was a second copy of `tabs/models/ServePanel.tsx` against the same
`POST /api/plan`, and it kept the question in component state, so a plan asked
here could not be linked to. The copy on the model's own URL carries `?ctx=`,
`?seq=` and the degrees in the path, which is why it is the one that survived.
`tabs/models/serve.check.mjs` names itself the former `planner.check.mjs` in its
own header.

**uPlot's axes and legend.** The box already carries a title, a value and a
min/note/max line; uPlot's own chrome would say all three twice inside a 48px
plot. The cursor is what the library was brought in for, not its axis renderer.

**A y cursor.** `cursor.y` is false and drag is off in every direction. Across a
synced grid of four different units a horizontal line at "the same y" is not a
claim about anything.

**A canvas per table row.** `ChartCell` stays hand-drawn SVG for the strip, and
the 24px trace it draws has no axis, no cursor and no band to justify anything
heavier.
