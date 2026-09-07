# UI

Agent H. Owns `ui/**` and nothing else. Reads everything over Agent G's HTTP
surface as specified in `00-architecture.md` §4.8, and calls no other component
directly.

```bash
npm install
npm run dev        # http://localhost:5173, proxies /api and /v1 to :8080
npm run build      # static assets into dist/, served by the coordinator
npm run typecheck
```

## Running before there is a cluster

The client is **live only**. `src/api/fixtures.ts` and `VITE_API_MODE` are gone
as of the derate port: the coordinator is always there in the deployed shape,
and a second code path that only runs when it is not was a second thing to keep
honest.

The day-0 stub still exists, on the server side, and is the way to bring up a
richer cluster than the machine in front of you:

```bash
DERATE_GATEWAY=http://localhost:8088 npm run dev   # against a real coordinator
```

`vite.config.ts` proxies `/api` and `/v1` to `$DERATE_GATEWAY`, defaulting
to `:8080`. Point it wherever the coordinator actually is.

For the parts of the graph a two-node cluster cannot show — the compact and chip
density tiers, the ring, a wide unmeasured mesh — run the layout verifier, which
needs neither a browser nor a cluster:

```bash
node src/tabs/cluster/layout.check.mjs   # assertions, plus layout-preview.svg
```

The history adapters have their own verifier, for a different reason: one route
answers with raw per-second columns under six hours and bucket aggregates
beyond, and reading the wrong shape yields an empty chart rather than an error.
Typecheck cannot catch it — every column is optional precisely so both shapes
fit one type. It runs against real payloads captured from a live coordinator,
because a fixture written from the same reading of the schema that produced the
bug agrees with the bug:

```bash
# from the ui root, with a coordinator reachable and a node that has samples
C=http://localhost:8088 N=spark-01 FX=/tmp/derate-history-fx
mkdir -p $FX
curl -s "$C/api/history/nodes?node_id=$N&from=-5m"    > $FX/nodes-raw.json
curl -s "$C/api/history/nodes?node_id=$N&from=-24h"   > $FX/nodes-1m.json
curl -s "$C/api/history/requests?from=-6h&limit=500"  > $FX/requests-raw.json
curl -s "$C/api/history/requests?from=-7d&limit=500"  > $FX/requests-1m.json
HISTORY_FIXTURES=$FX node src/state/history.check.mjs
```

It asserts the fixtures really are the two shapes it thinks they are, so a
schema change fails the check rather than quietly passing it.

## Layout

```
src/api/        types mirrored from the frozen contracts, the HTTP client,
                and a defensive credential scrub
src/state/      polled resources, the 1 Hz metrics stream, selection
src/styles/     the token layer; dark mode redefines five variables
src/components/ Readout, Lamp, Bars, Panel, Verbatim
src/shell/      header, app shell, the one sheet
src/sidebar/    roster, plan, routing, cost
src/tabs/       dashboard, cluster, spend, settings
src/inspectors/ deployment detail, and node/ — the node page: charts, what
                it serves, requests that ran on it, its links, events and logs
```

## Things that look like details and are not

**Tabular figures.** Every number updates once a second. `Readout` reserves its
width in `ch` and formats to a fixed decimal count, so a value crossing from two
digits to three moves the digits and never the unit. Verified: over twelve
samples the hero cycled through nine values with its left edge, box width and
the caption below it all fixed to the pixel.

**`Verbatim` exists to make a rule structural.** Planner and fit strings are
more precise than any rewrite, and they are the product. They are rendered
exactly as received — never truncated, re-cased, or summarised. Rendering one
through anything else is the bug.

**The cluster graph is a machine floor, and it draws every machine.** One card
per node in `/api/topology`, whether or not it is running anything — an idle
Spark, a node that just joined and a node that has gone unreachable are all
things you need to see, and a graph built out of deployments cannot show any of
them. Cards shrink by tier as the cluster grows (full to 4, compact to 8, chip
to 12) rather than being sized by dividing the available width, which is how the
previous layout could compute a negative box.

**Positions are a pure function of the node set.** Coordinator first, then
sorted ids, arranged in a row or a grid and a ring past twelve. No force
simulation: at this node count physics produces drifting, unrepeatable
positions, and a machine that moves between refreshes is a machine you cannot
learn the position of. A machine can be dragged to another slot to match the
rack it is actually in; what gets stored is a permutation of node ids, never
coordinates, because coordinates stop meaning anything the moment the window
resizes or the card size changes. `Reset layout` puts the default back.

**The unmeasured mesh is not all drawn at once.** `/api/topology` returns every
pair, so twelve machines is sixty-six edges of which one or two carry a figure.
Every measured link is always on the canvas; an unmeasured pair is drawn when a
deployment is relying on it, when the whole mesh is small enough to show, or
when its machine is selected. The rail below lists all of them either way.

**Edge thickness is scaled against the 40 GB/s tensor-parallel threshold**, not
against the fastest link present, so a link drawn at full weight is a link where
TP is viable. An unmeasured link is drawn dashed, carries no figure, and offers
a measurement. A measurement saturates the link, so it is never started unasked.

**A stream gap greys values and keeps the last reading.** Not frozen as if live,
not zeroed. The lamp goes hollow so it stops claiming freshness, and the trace
breaks rather than drawing a straight line across the gap. Reconnect is
exponential backoff from 1 s to 30 s, with no reload.

**The planner is a recommendation, not a lock.** The machines and the
parallelism degrees are the operator's to set. When they overrule it, the
planner's own rejection line for the shape they chose stays on screen, whole --
not the abbreviation the mockup drew, because that line names the exchange count
and the bytes per step and it is the sentence that says why the choice is likely
to be wrong. What is *not* overridable is the arithmetic: an overruled shape
goes back through `POST /api/plan`, and the verdict, the breakdown and the Serve
button all describe the shape that will actually launch. Sending no machines and
no degrees is byte for byte the request this bar sent before the fields existed.

**A launch can need more than one permission.** `serve.overrides` is the list,
each entry with the server's own sentence; each renders as a checkbox whose
label is the claim, and one Serve button appears below the stack when every box
is ticked. One launch, one button, however many permissions it took. A gate this
client does not recognise still renders and still blocks, so a newer coordinator
can add one without this client launching past it.

**No API key is rendered and there is no reveal control.** `src/api/redact.ts`
scrubs every response on the way in as a backstop; `api_key_ref` is an
environment variable name and is kept, anything key-shaped is replaced. Do not
add an inverse.

## Deliberately not built

Per `00-architecture.md` §1: no WAN endpoint, no chat history, no log browser,
no deep-dive metrics page. The main view's readouts are the whole metrics
surface.

§1's "model catalog browser" was reversed deliberately and is recorded in that
file's section 1 amendment appendix. The Models tab browses the curated
catalog, running deployments and provider models, and opens each one on its
quantization ladder -- every variant with the repository that carries it, its
measured size, and a fit verdict from the same gate a launch goes through. It
is not the catalog §1 refused: a catalog lists what exists, this answers what
runs here. Settings carries the reversal on screen under "Scope changed".

§1's "chat interface" half was reversed deliberately and is recorded in that
file's integration appendix. The Chat tab is a client for `/v1/models` and
`/v1/chat/completions`, both of which already existed; it adds no backend
surface and stores nothing, in the browser or on the coordinator. The history
half of the non-goal stands.
