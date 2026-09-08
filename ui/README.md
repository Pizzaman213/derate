# UI

Agent H. Owns `ui/**` and nothing else. Reads everything over Agent G's HTTP
surface as specified in `00-architecture.md` §4.8, and calls no other component
directly.

```bash
npm install
npm run dev        # http://localhost:5173, proxies /api and /v1 to :8080
npm run build      # static assets into dist/, served by the coordinator
npm run typecheck
npm run check      # the *.check.mjs suite -- this UI has no other test runner
npm run screens    # ui/screens/<dest>.png, one per destination, no assertions
```

`npm run check` discovers the verifiers rather than listing them, and reads what
each one needs off its `// requires:` line -- a live coordinator, python, a
browser, captured fixtures, or nothing. **An unmet requirement is a SKIP, named
in its own column, never a pass**; `npm run check -- --strict` makes each skip a
failure, which is what to use on a box where the coordinator is actually up.
`src/check/harness.mjs` is the prelude a new verifier starts from. Individual
files still run on their own -- `node src/state/router.check.mjs` -- and that is
still the inner loop.

`npm run screens` is the one that opens a browser. It drives the Chromium
already in the Playwright cache (nothing is downloaded) over every destination
in `state/routes.ts` and leaves a PNG per screen in `ui/screens/`, which is how
you see what a change actually did rather than inferring it from the source.

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
node src/tabs/cluster/particles.check.mjs # what gets drawn, in and out
```

The URL scheme has one too, for the reason under **URLs** below -- a URL is the
only part of this UI that leaves the machine, so both directions of it are
checked:

```bash
node src/state/router.check.mjs
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

The Ollama target has one because the string it builds is the whole feature —
`hf.co/<repo>:<publisher tag>` is what a remote daemon resolves against a
HuggingFace manifest, and a green typecheck says nothing about whether the tag
is spelled the way the repository spells it:

```bash
node src/tabs/models/ollamaTarget.check.mjs
```

It also pins the ladder partition, whose two branches are near enough inverses
that a mix-up would look plausible: under the cluster runtimes the servable
rows are what the gateway marked `launchable`, and under ollama they are the
GGUF rows, which are exactly the ones it did not.

The sidebar's Activity rows have one because every rule in them is a way of
declining to print a number, and every one of those compiles perfectly:

```bash
node src/sidebar/activity.check.mjs
```

A download with no reported total yet gets `value: null`, not `0` — the two are
different pictures (`ProportionBar` draws null as a dashed empty track and zero
as a solid one), and a solid 0% bar states that a transfer nothing has measured
is 0% done. A launching model gets `null` for almost all of a launch, and a
real fraction for one step of it: the server reads the backend's own log
(`/api/activity` carries `phase`, `status` and `fraction`), and the checkpoint
loader counts its own shards — "5/11" is a measurement somebody else made. The
image pull, the download, the compile and the graph capture all still report
`null`, because nothing reports a denominator for any of them, and a bar
filling at an invented rate is the thing this project refuses to ship.

The phase vocabulary is the server's (`control_plane/deploy/progress.py`); the
words on screen are `src/state/launchPhase.ts`, which the rail and the setup
wizard share so they cannot drift into two ladders again.

## The Runtime picker chooses between two verbs

`vllm` and `sglang` mean plan → fit gate → sparkrun, onto machines this cluster
owns and measures. `ollama (CPU)` means telling a provider — a box on the LAN
that derate does not orchestrate — to fetch a GGUF onto itself. The gateway
then routes to it like any other remote target.

Almost everything that differs on screen follows from that one distinction
rather than from the runtime's name, and `state/runtime.ts` states it once as
`servesOnCluster()` so no call site re-derives it:

- **No plan call.** `POST /api/plan` answers "does this fit on a machine in
  this cluster", and under a provider runtime no machine here carries it.
- **No node board and no degrees.** A provider picker takes their place;
  there is one box and nothing to split across.
- **Every fit field is suppressed, not reused.** The verdict lamp goes hollow
  and the Headroom and Decode columns are dropped. `_variant_verdicts` filters
  to profiles with addressable GPU memory *before* it walks, so those figures
  do not merely omit the provider's box — they describe a different machine.
  Predicted decode from a node's memory bandwidth would be wrong by orders of
  magnitude on a CPU, printed as a bare number with nothing qualifying it.
- **The fit gate's sentence is withheld.** It renders through `Verbatim`
  everywhere else because it names the numbers the gate used; that promise is
  exactly what makes it wrong to show beside a button that starts something on
  a machine the gate never looked at.
- **`?on=`, `?tp=` and `?pp=` are left alone**, not cleared. They are not in
  this runtime's request, but discarding a machine selection because somebody
  glanced at another runtime would lose it on a reload with nothing launched.

The runtime itself is deliberately *not* in the URL. `?ctx=`/`?seq=`/`?on=` are
there because a verdict is only worth sending to somebody if the question it
answers travels with it — and under this runtime there is no verdict to send.

`?on=` reaches the fit gate now, not just the launch. `GET /api/models/variants`
and `GET /api/capacity` both take it, so the quantization ladder is sized on the
machines the board has ticked, at the widest tensor-parallel degree the *model*
legally admits over them. The pane used to apologise for that gap in prose —
"Each row is sized on a single machine, so it does not account for the N you
ticked above" — which was honest and useless: the numbers were still the wrong
ones, and the sentence only appeared when the operator had ticked the machines
rather than when the planner chose them. The response carries `sized_on`, and
the caption states it.

## URLs

Every screen has one, and so does most of what you can select on it. The rule
for which half of a URL a thing goes in:

```
the PATH names the screen         /dashboard  /models  /cluster
                                  /chat  /spend  /settings
...and the screen's own subject   /models/meta-llama/Llama-3.1-8B
the QUERY names what is selected  ?node=spark-01        a machine
                                  ?link=spark-01~spark-02   a link
                                  ?dep=qwen3-30b-a3b    the deployment in the sidebar
                                  ?open=node:spark-01   the sheet, over any screen
                                  ?ctx=32768&seq=4      an override of what the fit
                                                        verdicts are taken at; absent
                                                        means the coordinator picks
                                  ?on=spark-01,spark-02 the machines they are taken on
```

Selections are query parameters rather than path segments because they are not
owned by a destination: the same machine is selectable on the cluster floor, in
the dashboard's telemetry strip and in the sidebar roster, and the sheet is a
modal that sits over whichever screen is showing. A model id is a path, because
it is the subject of the screen and because `/models/meta-llama/Llama-3.1-8B`
is the URL somebody would guess.

**`?dep=` carries a served name, never a deployment id.** `state/selection.tsx`
matches it against `served_name`, so a `deployment_id` there does not fail
loudly -- it matches nothing, falls through to `defaultDep()`, and silently
selects somebody else's deployment. That is worse than a 404, because the
screen looks like it worked. `POST /api/deployments` returns a `DeploymentDTO`
carrying both, and `ServePanel.launch()` navigates with `dep.served_name` for
exactly this reason.

`state/routes.ts` is the scheme -- `parse` and `href`, pure, no React.
`state/router.tsx` is the provider, the `useRouter` hook, and the push/replace
policy. `state/selection.tsx` reads and writes the four selections through it;
every caller still just says `selectNode(id)` and does not know a URL was
involved.

**Selecting replaces, opening pushes.** Clicking across a machine floor is
scrubbing, not navigating, and a history entry per click would make Back a
hundred-press undo of something nobody thinks of as an action -- the URL still
updates, so it is still shareable. Opening the sheet or a model's detail pane
pushes, because those are screens and Back is how people close screens. Closing
either one replaces, so Back from a closed sheet goes where you were before you
opened it rather than reopening it.

**A URL has one spelling.** A model id on a destination that has no models on
it is never written: it would be a second URL for one screen, and two spellings
of a shared link is how you end up unable to tell whether two people are
looking at the same thing.

**`?ctx=` and `?seq=` are overrides, and absence is not the default.** This
reversed on 2026-09-07. `?ctx=8192` used to be normalised away, because absence
and 8192 were the same request. They are not any more: no `?ctx=` means the
coordinator picks a context *per model* — the largest that fits at the best
quantization that holds it, capped at the model's own `max_position_embeddings`
— and that is the default path for the whole models screen. `?ctx=8192` means
somebody overrode that and every verdict is taken at 8192.

Two questions, so two URLs; the rule above is intact, because there is still
one spelling per meaning. Dropping an explicit 8192 on the way out would
silently rewrite an override into "you choose" on a link somebody shared, which
is the same failure the old normalisation existed to prevent, pointed the other
way. `router.check.mjs` asserts both directions and says so.

Nothing on the models screen writes these any more except the Serve panel's
**Advanced** disclosure, which is also the only way back — a "Choose for me"
button, because once a number is in the URL there is no value you can type that
means "you pick", and clearing the box types 0.

**The coordinator has to answer a deep path with `index.html`.** Nothing exists
on disk under `/models/meta-llama/Llama-3.1-8B`; the router in the page reads
the path itself. `_UIStatics.get_response` in `control_plane/gateway/app.py`
does that, and deliberately does *not* do it for `/api`, `/v1` or a missing
hashed asset. Break that and the links work exactly once -- for the person who
never reloads. Vite's dev server does the same by default.

## Layout

```
src/api/        types mirrored from the frozen contracts, the HTTP client,
                and a defensive credential scrub
src/state/      polled resources, the 1 Hz metrics stream, the URL scheme
                (routes.ts, router.tsx) and selection
src/styles/     the token layer; dark mode redefines five variables
src/components/ Readout, Lamp, Bars, Panel, Verbatim
src/shell/      header, app shell, the one sheet
src/sidebar/    roster, plan, routing, cost
src/tabs/       dashboard, cluster, spend, settings
src/inspectors/ deployment detail — two columns, the backend log filling the
                right one — and node/ — the node page: charts, what it serves,
                requests that ran on it, its links, events and logs, and a
                terminal
```

**Two dependencies beyond React and the fonts: `@xterm/xterm` and `uplot`.**
xterm is not in the bundle at all — `Terminal.tsx` reaches it through a dynamic
import, so Vite emits it as its own chunk and its 84 kB is fetched the first
time somebody opens a shell and never otherwise. A VT100 emulator that fits in
a few hundred lines renders `top` and `vim` wrong, so there was nothing to
weigh.

uPlot is the harder call, because the rule it looks like it breaks — *"do not
add a chart library for one line graph"* — is a real rule and `Chart.tsx` did
build its SVG paths by hand for exactly that reason. What changed is that the
charts stopped being one line graph. The durable archive gave every window a
rolled form carrying a bucket average AND a bucket maximum, which one path
cannot draw; four charts of one machine are read against each other and so need
one shared cursor; and a window can now report the stretches it knows it is
missing, which want to be geometry rather than a paragraph. Forty-five
kilobytes of canvas for three features that were each a rewrite of the renderer
is the trade the rule is asking about, not a framework imported to avoid thirty
lines.

It is also the only charting library whose data model was already this one's:
`spanGaps` defaults to **false**, so a null lifts the pen without being asked.
Recharts interpolates across nulls by default, which is the single thing this
product refuses to do — see `chart.ts`, where that promise is pinned at the
boundary rather than left to a third party's default, and `chart.check.mjs`,
which fails if it ever stops holding. The 24px spark cell in the deployments
strip is still hand-drawn SVG: no axis, no cursor, no band, and one per table
row.

## Things that look like details and are not

**Tabular figures.** Every number updates once a second. `Readout` reserves its
width in `ch` and formats to a fixed decimal count, so a value crossing from two
digits to three moves the digits and never the unit. Verified: over twelve
samples the hero cycled through nine values with its left edge, box width and
the caption below it all fixed to the pixel.

**A chat turn is coloured by which model it went to.** The picker can move
between sends, so a transcript is not necessarily a conversation with one
model, and until `tabs/chat/tags.ts` the only thing that said so was the name
over each *answer* — a question carried no destination at all. Each block now
takes a rule, a 6% tint and a label colour from the model it went to, assigned
by **order of first appearance and not by hashing the name**: a hash is stable
across transcripts and can still hand two models in the *same* transcript one
colour, which is the only thing the mark must never do. Four hues, each also
used dashed, gives eight identities; a ninth model is drawn plainly rather than
repeating one, because an absent mark says "read the name" and a wrong mark
says "these are the same model".

The colour is never the only mark — every block spells its model out, and a
user turn now reads `you → <model>`. The hues are held apart by
`tags.check.mjs`: at least 40° between any two, and at least 40° from
`--live`/`--warn`/`--fault`, so a model can never be read as a verdict on
itself. That threshold is not decoration. The first version of this shipped
blue against teal, 29° apart, on the two commonest slots: every assertion
passed, both models were correctly coloured, and nobody could tell at a glance
— which was the entire feature. The screenshot caught it and the number is now
in the verifier.

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

**Positions are a pure function of the node set and the arrangement.**
Coordinator first, then sorted ids, dealt into a row or a grid and a ring past
twelve. No force simulation: at this node count physics produces drifting,
unrepeatable positions, and a machine that moves between refreshes is a machine
you cannot learn the position of. Hand-placement is the opposite of that —
somebody putting a machine somewhere on purpose — so a machine can be dragged
anywhere on the floor and stays exactly where it is dropped, saved the moment
the pointer comes up and restored on the next visit. Nothing snaps, and a plate
may be dropped over a link or over another plate: where you want a machine is
not a question the drawing gets a vote on.

What is stored is a *displacement* from the slot the machine is dealt, never an
absolute x/y — this is the one thing that makes free placement storable.
Absolute coordinates stop meaning anything the moment the window resizes or the
cluster crosses a density tier and the card size changes; an offset travels
with its slot through both, so a floor arranged to match the rack still matches
it on a narrower window. What it cannot survive is the machine set changing
shape underneath it, because a node joining re-deals the slots. `Reset layout`
puts every machine back, and appears only once there is something to put back.

Two things follow a moved plate, because they are drawings *of* it rather than
decoration near it: its links re-aim (a bracket where the drag left a clear
channel between two level plates, an arc where it did not) and its band's bar
re-spans, stretching to the machine's new position and dropping its
`contiguous` claim once the bar would run across a machine that is not one of
its own. Both are decided off the final geometry — `layout.ts` asks "are these
level, with nothing in between?" rather than comparing row and column indices,
which describe the slot a plate came from and stop describing the drawing the
moment anybody drags one. `alt` plus an arrow key is the other half: it steps a
machine through the *default order* rather than nudging it, and clears that
machine's hand-placement so it lands in the slot the announcement names.

**The unmeasured mesh is not all drawn at once.** `/api/topology` returns every
pair, so twelve machines is sixty-six edges of which one or two carry a figure.
Every measured link is always on the canvas; an unmeasured pair is drawn when a
deployment is relying on it, when the whole mesh is small enough to show, or
when its machine is selected. The rail below lists all of them either way.

**A model a provider serves gets the same band as one this cluster runs.**
`/api/topology` has carried a `remotes[]` row per provider model since the
target index grew one, and nothing drew it: a model routed to a provider
reached this screen as a substring inside the bus's sublabel and as nothing
else — no band, no tap, no mention on any machine. The floor now draws it with
the same bar, the same served name, the same 1 Hz throughput readout and the
same selection as a deployment, because it is the same served name in the same
request flow. The only honest differences are drawn and no others: no plan, no
machine members unless the provider is one of ours, and its own words — `via
openrouter`, `off cluster` — saying whose hardware it is. It used to be drawn
dashed as well; that went with the routing boundary, and words on the band said
it better than a stroke pattern ever did.

**Which provider models earn a place on the floor is a rule, not a filter.**
That payload is one row per model of every enabled provider, so an OpenRouter
key with no allowlist is several hundred rows and a band each would bury the
machines under a catalogue nobody deployed. `groupRemotes` in `layout.ts` draws
a name only when somebody here did something to it: a deployment already serves
it (`backup` — it goes ON that band, named in its sublabel, which is what the
routing actually does with it), the provider is a machine on this roster
(`hosted` — it sits with the local bands, machine leads and all, because the
request never leaves the building), an alias was set (`named`), or the
providers behind it serve few enough names between them to be a list somebody
chose rather than a catalogue we fetched. Everything else is counted on the bus
and not drawn. The rule reads the topology payload and nothing else, so it is
pure and `layout.check.mjs` holds it — including the case it exists for, that
forty catalogue models draw zero bands.

**The provider bus lists one line per provider.** It used to carry a single
aggregate sentence — "2 providers · third party · no telemetry", then every
routed name in one list — which could say THAT a provider was behind the bus
and never WHICH provider served WHAT, the question somebody looks at that box
to answer. Each line now names a provider, the models it serves here, and how
much of its catalogue it could still reach. The box stays outlined rather than
filled: it is not a machine you own, and no provider is ever drawn as one.

**One entry plate per endpoint family, not one per floor.** The plate at the
left used to read `POST /v1/chat/completions` whatever was on the floor. With a
speech deployment beside a chat one that is not a simplification of the
routing, it is a drawing of a request the gateway refuses: naming a TTS model
on the chat endpoint comes back `wrong_modality` with the route that would have
worked. `layout.ts` now groups bands by modality — `endpointGroups` — and gives
each group its own entry plate and its own exit plate, the exit too because
what comes back off a speech band is one audio file and not a token stream. A
floor with nothing but chat deployments, which is nearly all of them, draws
exactly what it drew before: one family, one plate, at the same y. The plates
grow leftward from a fixed right edge so two labels of different lengths still
line up where the connectors leave, and `separate()` shuffles one down when two
families' bands interleave and both want the same middle — placed before
anything is connected to them, so a request block never flies out of thin air
beside a plate that moved.

**And `/chat` is the screen that makes that plate reachable.** The floor has
been able to draw a `POST /v1/audio/speech` entry since the audio work landed,
and for a while nothing in the product could send one: the chat picker filtered
audio models out, which left a runtime this repository writes audible only
from curl. A dedicated `/speech` screen closed that gap first; it was then
retired once the gap was closed a second way, in `ChatTab` itself — the chat
picker lists every modality (`buildRows` in `tabs/chat/ModelList.tsx` no
longer filters), and the composer posts to whichever endpoint the picked
row's `modality` advertises. Picking a speech deployment and typing a sentence
draws the same voice/format fields `/speech` used and returns a clip instead
of a `wrong_modality` 400; there is no separate screen left to keep in step
with the picker, because the picker and the send path now read the one field.
The clip is a Blob in React state behind an object URL that is revoked when it
is replaced; `pcm` gets a readout instead of a player, because raw samples
have no container and no browser will play them.

**The Chat picker is sectioned by endpoint, and filters nothing.** Every name
`/v1/models` reports gets a row, under a sticky heading naming the route it
answers on — `POST /v1/chat/completions`, `POST /v1/audio/speech`,
`POST /v1/audio/transcriptions`. What changes per row is the composer, not
whether the row exists: voice and format for a speech model, a file chooser
and a language field for a transcription one, a message box otherwise. Two
exclusions were removed to get here and both were the same mistake — a
statement about the composer wearing a statement about the model — so the rule
now is that a row you cannot act on is a bug in the composer. `ModelRow.modality`
is the single field the heading and the send branch both read, which is what
stops a row promising a route its request does not take; `rows.check.mjs`
asserts it. The `?dep=` fallback prefers a text model, and says so when the URL
named something this picker has no row for rather than silently substituting.

**A provider is connected on the side the request is already going.** The bus
used to be tapped from a rail in the left-hand gutter at x=156, between the
entry column at 140 and the floor at 172 — where every tap ran along the same
y as the entry connector that had just arrived at that band, under a line five
times its weight, with its junction dot reading as a bead threaded onto the
entry wire. The connection now leaves each band's bottom-right corner, drops
down a corridor of its own past the floor, and lands on the provider box's
right edge. That is also where the flight lands: the two used to disagree, the
rail stopping at the box's left edge while the block carried on to its centre,
so a request arrived at a provider by sliding out from under its own line and
across the label.

**Only a band a provider actually serves is connected to the bus.** Every band
used to get a 0.22-opacity hairline pointing at it, meaning "reachable through
the proxy" — true when a provider served its whole catalogue, and false now
that `enabled_models` means it serves nothing but its allowlist. A line from a
name no provider offers claimed routing that cannot happen, so there is no
longer a hairline tier at all: a band is connected or it is not, and the
predicate reads `band.providers` and the routing targets, both of which come
off payloads the coordinator has already filtered.

**A block in flight is a request in flight, on every band at once.** The
emitter used to open with `if (!selDep) return []`, so a graph with nothing
clicked — which is what it looks like almost all of the time — drew nothing,
and a cluster whose only traffic went to a provider had never animated. Drawing
all of it weakens no claim: a block is still one measured request off
`queue_depth` or `outstanding`, and `collapseFlows` still drops any flow whose
path this layout did not draw, so several hundred catalogue names cost one map
and no ink. A provider flight now crosses the band it is served by on its way
down, which the port had lost — it turned into the gutter sixteen units short
of the band, so a block for a name was never once seen on that name.

**The return legs run on every band too, but only the selected one is
coloured.** The rate is per band and measured, off the same 1 Hz frame as the
tok/s readout beside it. Whether a name was *called* with `stream: true` is not
on that frame at all — it lives in the request archive, which `useRequestMix`
queries for one served name — so asking per band would be a query per band for
a hue. The selected band paints its measured streamed/batched tone and every
other passes null, which is the same "no reading" grey an empty meter track
uses. Selecting a band is what answers that question for that band.

**Remote throughput comes off the same 1 Hz frame as a deployment's.**
`MetricsHub` reports a `remotes[]` row per remote target the `StatsRegistry`
has actually seen — counters only, so an un-allowlisted key is not several
hundred rows a second. Joining `/api/topology`'s copy of the figure instead
would have been honest and arrived on a 5s poll, and a band ticking five times
slower than the one above it on the same drawing reads as a fault in the
drawing.

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
no degrees is byte for byte the request the Serve panel sent before the fields
existed. It asks on the model's own screen, and it is the only place that asks:
the dashboard carried a second copy of this bar until 2026-09-07, holding its
context and concurrency in component state, so the one plan you could not link
to was the one on the screen you landed on.

**A launch can need more than one permission.** `serve.overrides` is the list,
each entry with the server's own sentence; each renders as a checkbox whose
label is the claim, and one Serve button appears below the stack when every box
is ticked. One launch, one button, however many permissions it took. A gate this
client does not recognise still renders and still blocks, so a newer coordinator
can add one without this client launching past it.

**No API key is rendered and there is no reveal control.** `src/api/redact.ts`
scrubs every response on the way in as a backstop; `api_key_ref` is a reference
name and is kept, anything key-shaped is replaced. Do not add an inverse.

A key can be *sent* — Settings -> Providers offers "Paste a key" beside "Name a
reference", and the coordinator writes it to `secrets.json` and keeps only the
name — but nothing sends one back, so the rule above is unchanged. The two
modes exist because one masked field labelled "Key reference" was the whole
bug: it looked exactly like somewhere to paste a key, took the name of an
environment variable, and said so only in prose under the table.

The add form's field is labelled with the kind's own name and hinted with its
own key shape — "OpenRouter key" over `sk-or-v1-…` — rather than "API key" over
a generic `sk-…`, so somebody arriving with a key on the clipboard can see it
is the field for it without reading anything. The hints are `keyPlaceholder` in
`settings/keyfield.ts`, and every prefix in it is one `api/keyshape.ts` already
recognises; a kind with no prefix this build knows keeps the generic hint
rather than a guess. `keyfield.check.mjs` walks the kinds the *server* says
need a key, so a kind added there is checked here without this file changing.

That field is one component, `settings/KeyField.tsx`, and it appears twice.
**Set key** on a provider's own row is the same field PATCHed rather than
POSTed. Before it existed, a wrong or expired key was fixed by removing the
provider and adding it back — throwing away its priority, budget and aliases to
change one string, and there was no other way to do it from the product. A key
pasted there always lands under the reference derate mints
(`DERATE_<ID>_API_KEY`), because that is what the coordinator does with
`api_key` on a PATCH: it reuses a reference it minted and never overwrites one
the operator brought. Where the two differ the field says so before the save,
naming the reference the provider is about to move off.

**The Models cell is a disclosure, and it reads "2 of 312".** Adding OpenRouter
used to put every model it publishes into the Models tab and into `/v1/models`
at once — a list nobody chose, and on the Models tab a freeze point. A provider
now serves only what somebody switched on, and the cell opens
`settings/ProviderModelsPanel.tsx`: one flat list of what the provider
publishes, a search box, and a plain toggle per row carrying the model's name
and nothing else. No context window, no price, no sequence count, no facets or
tabs. Those facts are worth showing where a model is being *chosen for work*;
the only question here is whether this cluster serves it at all.

The filtering is entirely server-side and no component does any of it.
`/api/providers` already carries only the enabled models, so the Models tab's
`providerRows`, the inspector's "served elsewhere", the pull picker and Spend
inherit it without knowing it happened; `/v1/models` is filtered at the router's
target index, and the chat picker reads that endpoint's local half.
`GET /api/providers/{id}/models` is the one endpoint that still answers with the
whole catalogue, each row flagged `enabled`, and this panel is its only caller.
`allowlist.check.mjs` pins that split against a live coordinator — the two
endpoints agree, the counts mean what they say, and nothing switched off is a
routing target. A green `tsc` says nothing about any of it: `Provider.models`
and `ProviderCatalogueModel[]` are near-identical shapes, so serving the
unfiltered list from the filtered endpoint typechecks perfectly and puts three
hundred models back on screen.

A toggle PATCHes the complete set rather than a delta, so clicking faster than
the network resolves to the last state the operator actually saw.

**"Backs up" is the other half of that row, and it is one field.** A provider
model whose served name equals a deployment's is merged into that name's routing
entry (`gateway/targets.py`), and `auto_policy_explained` picks `local_first`
for a name served both ways ahead of `weighted_capacity` — "a mixed fleet is a
spill decision before it is a balance decision" — so the provider takes traffic
only once every local replica stops admitting, and stops the moment one frees.
All of that was complete on the server and reachable only as
`PATCH /api/providers/<id> {"aliases": {...}}` typed by hand, which meant the
overflow valve the README leads with could not be opened from the screen.

`settings/ProviderBackupPanel.tsx` asks the one question that opens it: which
name should this provider's model answer to here. The served-name field
suggests what this cluster runs, because aliasing onto one of those is the
backup case; anything else typed is a rename, and the row says which of the two
it just did rather than leaving it to be inferred from whether traffic ever
arrives.

It composes the next alias map from `GET /api/providers/{id}/models`, not from
`/api/providers`. PATCH replaces the map wholesale and that endpoint carries
only the models the allowlist admits, so building the map from the filtered
list would silently drop the alias of every model somebody had switched off —
and switching one back on would restore it under its upstream id.

**Each row says whether its key resolves and from where**, from `key_state` and
`key_source` on `/api/providers` through `keyStateNote`. "resolves from
environment" and "resolves from secrets.json" are different facts, and the
second is the one this coordinator can replace in place. A null state is a
provider port that cannot answer and reads as "state unknown", never as a
missing key: putting a warning beside a working provider on nothing but the
absence of a method is worse than admitting the gap. The vocabulary is not
restated here — `keyfield.check.mjs` reads it out of the server's own
`key_status` and fails when a state grows there with no sentence for it.

The screen deciding what is a name and what is key material is
`src/api/keyshape.ts`, a port of the server's `looks_like_secret`, shared by
`redact.ts` and the add form. `src/tabs/settings/keyfield.check.mjs` asserts the
two sides still agree by running the Python — a drift is invisible to `tsc` on
both sides, and one already shipped: `redact.ts` tested a reference against
`/^[A-Z][A-Z0-9_]{0,63}$/` while the server accepted `my-openrouter-key`, so a
correctly-configured name rendered on screen as `***`.

## Deliberately not built

Per `00-architecture.md` §1: no WAN endpoint, no chat history, no log browser,
no deep-dive metrics page. The main view's readouts are the whole metrics
surface.

§1's "model catalog browser" was reversed deliberately and is recorded in that
file's section 1 amendment appendix. The Models tab is ONE list over five
sources at once -- the curated shortlist, the weights already on disk, what is
running, what each provider publishes, and the hub -- folded so that a model
which is several of those at once is one row that says so. Opening a row gives
its quantization ladder: every variant with the repository that carries it, its
measured size, and a fit verdict from the same gate a launch goes through. It
is not the catalog §1 refused: a catalog lists what exists, this answers what
runs here. Settings carries the reversal on screen under "Scope changed".

§1's "chat interface" half was reversed deliberately and is recorded in that
file's integration appendix. The Chat tab is a client for `/v1/models` and
`/v1/chat/completions`, both of which already existed; it adds no backend
surface and stores nothing, in the browser or on the coordinator. The history
half of the non-goal stands.

It lists the *local* half of `/v1/models` -- the names carrying `local` in
`target_kinds`, which is the gateway saying a deployment in `ready` or
`degraded` is answering for them. The whole endpoint is the router's target
index, so on a coordinator pointed at OpenRouter it is four hundred-odd rows of
somebody else's hardware with the one thing this cluster runs somewhere inside
it; and a browser for a provider's catalogue is the "model catalog browser" §1
also rules out. Deployments that are not serving yet are not listed either --
they used to be, disabled and captioned with their state, and that was
reversed: every row here can answer, and a launch in flight is reported by the
empty panel's own sentence and by the Dashboard. `chat/rows.check.mjs` pins all
of it against a live coordinator, because `ServedModel[]` is the same type
filtered or not and deleting the filter typechecks perfectly.

The picked model is `?dep=`, the app's selected deployment, rather than a
second private notion of the same thing -- the row set and `selDep` have the
same domain, so `/chat?dep=<served name>` opens on that model the way
`/cluster?node=` opens on that machine.
