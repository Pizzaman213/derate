# tabs

The destination roots. One file per screen the URL can name, each one the whole
of what `/dashboard`, `/models`, `/cluster`, `/chat`, `/spend`, `/settings` and
`/setup` mean — `state/routes.ts` is the scheme, where `SEGMENT` is the
`Record<Dest, string>` giving each destination its path segment and
`DESTINATIONS` is derived from it. The union it keys is seven members wide and
`shell/AppShell.tsx` is what maps those seven onto the seven files here.

Six of them are mounted together and hidden with `hidden`, never unmounted, so a
destination keeps its in-flight polls and its scroll position across a visit
elsewhere. `SetupTab` is the exception and it is an early return:
`shell/AppShell.tsx` renders `if (dest === 'setup') return <SetupTab />` **above**
the header, the roster rail and the sidebar, because on a fresh install all three
are empty and a frame around four empty boxes is a worse first impression than no
frame.

None of these files computes a hardware, memory or fit fact. Verdicts come from
`GET /api/capacity` and `POST /api/plan`, bytes come from `GET /api/storage`,
phases come from `GET /api/activity`. A second opinion computed in the browser
would be a promise the launch path never agreed to.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `SetupTab.tsx` | 1269 | first run: five steps, a launch watcher, and the join command |
| `ModelsTab.tsx` | 711 | one list folded over five sources, banded by the fit gate's verdict |
| `ChatTab.tsx` | 305 | a client for `/v1/models` and the three endpoints `send` posts to; the transcript is React state and nothing else |
| `SpendTab.tsx` | 280 | where requests went and what they cost, with every unpriced figure declared |
| `ClusterTab.tsx` | 237 | the machine floor, the link rail beneath it, and the saved arrangement |
| `SettingsTab.tsx` | 156 | seven sub-tabs; the composition root for `settings/` and `storage/` |
| `DashboardTab.tsx` | 87 | four sub-tabs; it does the fetching and hands the data down as props |

Each destination's parts live in a folder beside it:

| Folder | Thesis |
|---|---|
| [`chat/`](./chat/README.md) | the picker, transcript, composer and voice fields, sectioned by the endpoint each row answers on |
| [`cluster/`](./cluster/README.md) | the floor drawing: layout, bands, particles, motion and the selection rail |
| [`dashboard/`](./dashboard/README.md) | the aggregate row, the deployments strip, and the three sub-tabs under it — a headroom table, a charted `TelemetrySub`, and `LoadSub`'s node-by-target matrix |
| [`models/`](./models/README.md) | rows, cards, the quantization ladder, and `ServePanel` — the one planning surface left |
| [`settings/`](./settings/README.md) | the cards the settings sub-tabs compose, and the key fields that never render a secret |
| [`setup/`](./setup/README.md) | the QR encoder the last setup step draws, and `setup.css` |
| [`spend/`](./spend/README.md) | `rows.ts`: metered against estimated, and every rule for when a figure is a lower bound |

## `SetupTab.tsx`

First run, as five steps: `machine`, `cloud`, `model`, `machines`, `done`. The
cloud step is **not rendered at all** when `status.provider_routing` is false —
not disabled, not explained — because a step nobody can take is noise on the one
screen where every word is being read.

`FIRST_RUN_MODELS` is five ids and every one of them is already 4-bit: AWQ, GPTQ
or compressed-tensors w4a16, never a bf16 checkpoint the ladder steps down from.
That is deliberate twice over. The weights are about a quarter of the download,
which is the wait a first run actually feels — Qwen3-30B-A3B ships 56.9 GiB of
safetensors against 15.8 GiB for its GPTQ-Int4 build; Mistral-7B-Instruct-v0.3
ships 27.0 GiB against 3.9 GiB for the w4a16 one. And starting at int4 keeps the
ladder off GGUF: on a tight live budget the bf16 list came back as q8_0 / q6_k /
q2_k rows, which `resolver/support.py` marks UNVERIFIED on vLLM and UNSUPPORTED
on SGLang. The ladder can still descend below int4; it can no longer climb back
to bf16.

`Ledger` is the running tally at the top, and `goTo` clears every choice after
the step it rewinds to. A tick that survived the answer it was about would be a
lie in the one place the reader is using to keep track.

### The first-run list is not the curated shortlist

`fit/catalog.py`'s `CURATED_MODELS` exists to answer "what can this hardware
do", so it reaches for the ceiling on purpose — gpt-oss-120b and DeepSeek-V3 are
on it, and on one GB10 both come back as refusals. Correct answers, wrong
screen: a first run that opens with two paragraphs about being 617 GiB over
budget has taught somebody that the product mostly says no before they have run
anything at all. `capacityFor(FIRST_RUN_MODELS, null, null)` replaces the curated
walk rather than extending it, and the nulls ask the coordinator to pick a
context per model out of what actually fits.

That request is fired on mount, not when the model step appears: five models is
five hub round trips on a cold cache, and somebody spends a minute on the machine
and provider steps regardless. `CapacityAnswer` is a union rather than four loose
pieces of state, so "still loading" and "answered with nothing" cannot be
confused — an empty row list is a real answer about a machine nothing fits on,
and a spinner for it would wait forever for something that already arrived. Past
ten seconds the step says how long it has been waiting and offers to be skipped,
because an indefinite spinner cannot be told from a hung request and this one
depends on huggingface.co being up.

### `LaunchProgress` reads phases; it no longer infers them

`ModelStep.start` renders before `launch` resolves, on purpose. `launch` returns
as soon as the process is spawned, but "as soon as" still covers a resolve, the
fit gate and a process start, and awaiting it greyed every control out and said
"Starting…" in a footnote — which reads as frozen at the exact moment somebody
has committed to downloading tens of gigabytes. The launch goes out at
`row.context` and `row.max_seqs`, the numbers the verdict on screen was taken at,
never numbers this screen chose.

The watcher polls `deployments()`, `activity()` and — only while the phase is
`preparing` or `downloading` — `storage()`, every two seconds. This screen used
to infer the phases from bytes on disk: bytes appearing meant downloading, bytes
holding still for two polls meant loading. It worked, and it could not tell an
engine compiling from a download stalled, because both are a number that stopped
moving. `GET /api/activity` reports the phase off sparkrun's own output and the
backend's log (`control_plane/deploy/progress.py`), so this screen and the rail
agree and neither guesses. Four rules survive from that era:

- **Phases only go forward.** A poll landing between two writes can walk
  backwards, and a stepper that un-ticks reads as something going wrong. A phase
  this build has never heard of sorts to `-1` and is therefore treated as
  backwards, which is the safe way to be wrong about one.
- **A fatal runtime line is shown as the failure immediately**, a poll or two
  before the deployment record carries it, because it says more than the record
  will: `Free memory on device cuda:0 (49.56/121.69 GiB) on startup is less than
  desired GPU memory utilization` names what to change.
- **There is no percentage on the download.** Nothing reports the repo's final
  size before it arrives, and a bar creeping at a made-up rate gets read as an
  estimate and planned around. A rate is printed only while the figure is
  actually moving; zero beside a stalled number reads as a stall this cannot
  diagnose.
- **The byte figure at arrival is remembered.** If it never moves off it, the
  weights were already cached and no download happened — said out loud, rather
  than a download step that silently did nothing.

`MachinesStep` mints the join command server-side (`mintEnrollment({ auto_admit:
true })`) and `DoneStep` mints the `/v1` endpoint the same way
(`mintEnrollment({ ttl_s: 60 })`, falling back to `window.location.origin`). Both
use the coordinator's own probed address rather than `window.location.host`,
which is right in production and a lie in dev — and a wrong address in an install
command fails on a machine nobody is looking at. `DoneStep` calls
`completeSetup()` once on arrival, including when nothing was added: reaching
this screen is itself the answer.

## `ModelsTab.tsx`

One list. It used to be five mutually-exclusive source tabs — Recommended, On
device, Hub, Running, Providers — and the split cost the screen the question a
picker exists to answer: who can serve me this model, and can I run it myself?
You had to know which tab a model lived in before you could look for it, and the
search box only ever reached the hub, and only while the Hub tab happened to be
open.

The sources are facets of a row now. A model that is curated, on disk **and**
serving is one row saying all three. `GET /api/models` folds them server-side
over all five sources at one instant — which closed a skew this file used to
patch over, where `/api/providers` (15s) and `/api/providers/{id}/models` (300s)
disagreed for a tick after somebody switched a model on and the row claimed to be
both served and merely offered. `decorate` joins fit and cache afterwards;
`withHubHits` folds in `/api/models/search`, and only there, because a hub answer
is a fact about a query and not about this cluster.

Rows band by the fit gate's verdict, from `GET /api/capacity`, polled at 20s.
`models/rows.ts` holds the whole of it: `Band` is seven members wide and
`BAND_TITLE` names them — "Running here", "Fits here", "Loads, but decode is
slow", "Not checked", "Needs more memory", "Runs on a provider, not here" and
"Not served", drawn in that order and only the last one collapsed. Nothing here
recomputes a fit.

**`AUTO_CHECK = 10` is a hub budget, not a render budget.** A searched model has
no verdict, because the capacity walk covers the curated shortlist and nothing
else, and a list whose every row says "not checked" is not answering the question
it exists for. So the first ten verdict-less rows are resolved in one batched
`capacityFor` call. The hard part is termination: the trigger is one batch per
*settled* question (`settled` is an empty box, or `hits.query === needle`), the
key is claimed in `asked.current` **before** the request and even when there is
nothing to ask, and `remoteOnly` and `unservedOnly` rows are excluded — those are
verdict-less by nature and there can be several hundred of them, so without the
exclusion every settle would spend the whole batch resolving a vendor's catalogue
against the hub.

**Every verdict is keyed on the question it was taken at.** `capKey` is
`context/concurrency/nodes`, and a batch whose key no longer matches is dropped
rather than shown under the current caption — a verdict taken at 8k is not an
answer about 128k. Both the capacity question and the hub search debounce 450ms
before becoming a request.

The context and sequences fields are gone from this screen. Two fields writing one
piece of state read as two settings that might disagree, and both asked for a
number in the units of a thing nobody had picked yet. The coordinator derives a
context per model from what fits, clamped to that model's own window; an override
lives behind `ServePanel`'s advanced disclosure. `?ctx=`/`?seq=` stay in the URL,
where null now means "the coordinator picked" — a different request from
`?ctx=8192`.

`machinesPhrase` names the machines a report was taken on rather than counting
them. "one machine" was the old apology's phrasing and it is exactly what nobody
can check; the complaint was that the screen would not say which.

## `ChatTab.tsx`

A client for endpoints that already existed. Until this tab there was no way,
from inside the product, to confirm that a deployment this cluster is running
actually answers — that meant leaving for curl. The readout under each answer is
the point of it: which model served, the request id the gateway minted, and what
it cost in time.

**No chat history, by construction rather than by promise.** The transcript is
`useState<Turn[]>` and nothing else: no `localStorage`, no fetch on mount, no
server-side surface, nothing added to the backend. Reload and it is gone, and the
head of the pane says so. `settings/ScopeCards.tsx` still carries the row —
`['Chat history', 'the Chat tab is a test console; the transcript is never
stored']` — so the product neither denies a feature it ships nor claims one it
does not.

`send` branches on the picked row's modality, and every turn records `model:
active` on both halves at the moment it is sent. The picker can move before the
answer lands and will move again over the transcript's life, so a turn that read
the current selection at render time would relabel and recolour itself every time
somebody switched.

- `transcription` → `backend.transcribe`. The answer is text, so it lands in
  `content` and every reader that understood a chat turn renders it unchanged.
- `speech` → `backend.speech`, and **no history is sent**: a speech request has
  an `input`, not a conversation, and the server would refuse an unknown field.
- everything else → `backend.chatStream`, with the request id patched in on
  `onOpen` so a turn that goes on to be refused still names the row that recorded
  the refusal.

Every token-denominated figure on the two audio paths is `null`, never 0. There
are no tokens over a binary body, the gateway deliberately counts none, and
inventing a count from the returned characters would be a figure nobody reported.

### `/speech` was retired once `/chat` could do it

`POST /v1/audio/speech` was reachable from exactly one screen for a few days,
because the chat picker filtered audio models out — which left a runtime this
repository writes audible only from curl. The filter was removed instead. The
picker lists every modality under a heading naming the route it answers on —
`.chatgrouphead` is `position: sticky`, because the question it answers stops
being visible the moment it scrolls off — and the composer grows voice/format or
a file chooser to match. Both audio branches of `send` read the same
`row.modality` the heading was drawn from, so neither audio heading can promise a
route the send does not take. `embedding` is the one row where they part: its
heading is `POST /v1/embeddings` out of `ENDPOINT_FOR_MODALITY`, `send` has no
embeddings branch, and the turn goes to `backend.chatStream` like any text row.
`/speech` was retired on 2026-09-08 with nothing left that `/chat` did not
already do.

The `?dep=` fallback prefers a text row, and it became load-bearing the moment
speech joined the picker: `buildRows` sorts local names alphabetically, and on
this cluster `Audio8-TTS-Preview-0.6b` sorts above `Qwen2.5-0.5B-Instruct`, so a
plain `rows[0]` made the default chat model a TTS deployment. An explicit `?dep=`
always wins. When it names a row the picker does not have, `substituted` says so
and names both models — on `/chat?dep=Audio8-TTS-Preview-0.6b` the rail once read
one model while the pane read another and Send went to the second.

## `SpendTab.tsx`

Where requests went and what they cost, deliberately two figures shorter than
the mockup it was ported from. The "managed remote"
tier is gone — a target is local or a provider, never a third kind — and so is
the `0.0009 Mtok/request × $0.60` "saved vs all-cloud" tile, because no such
constant exists anywhere on the real wire and a tile computed from one is exactly
the invented number this port exists to remove. "Tokens generated" replaces it
with a real sum.

**A zero is never printed where the figure is unknown.** The cloud half had the
mirror image of the same bug for longer: `spend_today_usd` is
`round(runtime.spend_today(now), 6)` server-side and is never `None`, so a
provider serving models it publishes no price for reported exactly the `$0.00` of
one serving nothing, under a tile labelled "spent today". `providers/runtime.py`
had counted `unpriced_requests` all along; `spend/rows.ts` is what reads it, and
`money(value, floor)` renders `≥` whenever real traffic sits under a total
unpriced. Local cost is re-gated the same way: `RouteTarget.cost_per_mtok` comes
back as a real `0.0` from `gateway/targets.py::local_cost_per_mtok` when the
electricity rate is unset, so every local figure here is forced to null (an em
dash) unless `settings.electricity_rate_usd_per_kwh > 0`.

`dedupeByTargetId` runs before any aggregation: a routing config can alias one
physical target under two served names, which would otherwise double-count its
requests, tokens and spend. The two halves keep different accounting windows on
purpose — providers report a figure that resets daily, the gateway keeps no
history at all — which is why the tile is labelled "requests" and not "requests
today", and why the caption says so.

Each row's Spent cell carries a `basis`, because a metered figure and an estimate
are not the same claim: metered is what the provider reported it charged,
estimated is arithmetic over a published rate that cannot see a cached prompt or
a long-context tier.

## `ClusterTab.tsx`

The machine floor and the reference desk under it. It fetches its own data
through the same `state/resources` hooks every other destination uses rather than
taking props, so it drops into AppShell's section on its own.

The arrangement — which slot each machine is dealt (`order`) and how far it has
been dragged off it (`offsets`) — is one piece of state because it is one
preference, saved through a single `save` callback so "auto-saves" is a property
of the state rather than something each caller remembers. It is stored per
cluster id, which is not known until the first poll answers. `arrangementRef`
exists so the two writers can read the current arrangement without putting it in
their dependency arrays: `ClusterGraph` keys its pointer and key listeners on
those callbacks, and a new identity per drop would tear the listeners down and
rebuild them on every move.

Measurement and reachability are separate state, deliberately: different
questions at different prices, and one running must not grey out the other's
button. Both are keyed `[a, b].sort().join('~')` and both surface their failure
where the button is. A rejected measurement that vanished into an unhandled
rejection left the rail reading "Never measured" with no explanation; a refused
check that only re-enabled its button read as "checked, and everything is fine",
which is the one thing it does not mean.

`nameIndex` is called once for the whole destination so a plate and the chip
naming the same machine cannot disagree. The legend states one meaning per
channel: width is measured bandwidth against the 40 GB/s tensor-parallel
threshold, a dash is never-measured and nothing else, colour means something is
wrong — which is why a healthy plate has none.

## `SettingsTab.tsx`

Seven sub-tabs — connection, nodes, storage, providers, policy, appearance, about
— composing cards from [`settings/`](./settings/README.md) and `storage/`.
This was one nine-card column, and the column
was not merely long: it ran four unrelated jobs together, and roughly half its
height was the About cards, which are documentation and never change.

Connection is first for the reason Coordinator used to be the first card: it is
the one section that still works when the connection does not, and every other
section is empty until the address in it is right. Its panel pairs
`CoordinatorCard` with `ClusterCard`, the second being the proof the first is
right — an address that resolves reports a cluster id back.

**Storage is why `tabs/storage/` has no tab file.** It is the same job as Nodes —
reading facts off the machines behind this coordinator — so the four cards moved
in unchanged rather than keeping a destination of their own. Appearance moved for
the mirror reason: the dark-mode select used to sit in the header, which put a
local-only preference in the bar every screen shares.

Every panel stays mounted and is hidden with `hidden`. `AddNodeCard` holds a
minted enrollment token, its countdown and the machines that have turned up
since — state a tab switch must not throw away — and so does a half-typed
coordinator address. The About panel is the one that takes `tabIndex={0}`:
nothing in it is focusable, so without it a keyboard lands on the tab with
nowhere to go. Every other panel holds a control and must not add a redundant
stop.

## `DashboardTab.tsx`

Four sub-tabs — overview (`AggregateRow` plus `DeploymentsStrip`), headroom,
telemetry, load — and this file is the one doing the fetching. `useCluster`,
`useTopology`, `useRouting`, `useMetrics` and `useTelemetrySeries` are read once
here and handed down as props, so `TelemetrySub` and `LoadSub` mount no interval
of their own and cannot disagree with the strip above them about what is
running. `HeadroomSub` is the exception and takes no data prop: it asks
`useMemoryReport` and `useCapacity` itself, at a fixed `context={8192}` and
`concurrency={1}`, because the question it puts to the fit gate is its own.
Headroom sits second because it answers the planner's question in reverse — not
"does this fit" but "what fits".

**Planning is not here.** The bar that used to sit above these tabs, always
visible, was a second copy of `models/ServePanel.tsx`: the same `POST /api/plan`,
the same `Verdict` box. It was removed on 2026-09-07. The copy on the model's own
URL is the better one, because it carries the question in the URL (`?ctx=`,
`?seq=`, the degrees) while this one held it in component state — so a plan asked
here could not be linked to. One plan surface, on the screen that names the
model.

The accumulated 60-second window used to be built here and drilled down from
here, which made it reachable only from this destination. It sits beside the SSE
subscription in `TelemetryProvider` now, because the node sheet draws it too and
the node sheet is not inside this tab.

## The seam with the shell

`shell/AppShell.tsx` is the only importer of all seven. Nothing here is exported
to anything else; the traffic runs the other way, through the state providers:

```tsx
if (dest === 'setup') return <SetupTab />   // above the chrome, not inside it

<section aria-label="Models" hidden={dest !== 'models'}>
  <ModelsTab />                             // mounted, hidden, polls still running
</section>
```

Three of this folder's subfolders are imported from outside it, by six files,
and none of the three can be changed as if it were private to a screen here.
`cluster/layout.ts` is the most shared of them: `shell/Sheet.tsx`,
`sidebar/PlanSection.tsx` and `inspectors/node/NodeInspector.tsx` all pull
`runners` out of it and `inspectors/node/Interconnect.tsx` pulls `edgeMeasured`.
`dashboard/Chart.tsx` is next — `inspectors/node/NodeCharts.tsx` takes `Chart`
and `ChartGrid`, `inspectors/node/ServingBlock.tsx` takes `Chart`. `Sheet.tsx`
is the only importer of `models/ModelInspector`. So the model pane, the floor's
geometry and the chart primitive are shared surfaces: a change to any of them
shows up on a screen this folder does not own, and the node sheet is where it
shows up first.

Everything else arrives through `state/`: `useCluster`, `useTopology`,
`useRouting`, `useProviders`, `useModelRegistry`, `useCapacity`, `useStorage`,
`useSettings` and friends from `state/resources.ts`, `useBackend` for every
mutation, `useSelection` for `?node=`/`?dep=`, `useRouter` for the path, and
`usePlacement` for the machines the Serve panel has ticked.

## Things that look like details and are not

**Six tabs stay mounted; setup replaces the frame.** The `hidden` attribute, not
a conditional render. A destination unmounted on every visit elsewhere restarts
its polls, loses its scroll position and re-runs `ModelsTab`'s auto-check batch
from scratch. `SetupTab` is the deliberate exception and is still a real
destination with a real URL, so it can be linked, reloaded and re-run by typing
it.

**`ModelsTab` holds its selected model in the path, not in state.**
`/models/meta-llama/Llama-3.1-8B` plus `?ctx=`/`?seq=` is the whole of what
somebody means when they send you a model. It carries the id only: it used to
snapshot context and concurrency at the moment of opening, which meant editing
either field afterwards rebanded the list while the pane beside it went on
answering the old question — the caption saying 32,768 and the ladder under it
saying 8,192, both correct, on one screen.

**A feed failing greys nothing and empties nothing.** `ModelsTab` prints one line
naming the feed and the server's own sentence, and the other feeds still answer.
Only a total blackout — every feed failed *and* not one row to show — is handed
to the list as an error.

**The provider catalogue read on `ModelsTab` is the only unfiltered one in the
app.** `/api/providers` carries just the models the allowlist lets through, which
is right for every other screen and is exactly why this tab could not be the
place you choose from. `useProviderKinds` and `useProviders` are mounted once
here and handed down for the same reason: `useResource` does not deduplicate, so
a copy in the inspector and another in the pull card would be two intervals
asking for one static table, and neither may answer differently.

**Every refusal is printed exactly as it arrives.** `SetupTab`'s launch error,
`ChatTab`'s message, `ModelsTab`'s per-feed sentences, `ClusterTab`'s measurement
failures. `ApiError` has already unwrapped the gateway's `{error:{message}}`
envelope, so what reaches the screen is the sentence the gateway wrote; on the
chat path `Transcript` renders it through `Verbatim`, and everywhere else it is
printed with `white-space: pre-wrap` and no rewording. A refusal names what to
change; rewriting it destroys the thing that made it useful.

**`SetupTab` polls `/api/storage` only while bytes could still be arriving.**
That endpoint fans out to every node agent and does real syscalls there — the
rail polls it at thirty seconds for that reason — and this screen asks every two.
Once the phase says the weights have landed the figure cannot change, and the
request is pure cost on the machine that is busy loading a model.

## Failure behaviour

- **The coordinator does not answer `/api/setup`.** `SetupTab` renders its own
  page — "This coordinator did not answer." — with the error verbatim and a
  reload button. Nothing else on the screen is attempted.
- **The coordinator cannot identify its own hardware.** The machine step says so
  and offers "Carry on", naming how many machines are on the roster. It never
  introduces somebody else's GPU as the box under the reader's desk.
- **This coordinator has no local runtime.** `report.local_serving` false leaves
  the verdicts on screen — they are real — and disables every pick, with a line
  saying it can still route to a provider and to machines that join it.
- **A first-run model will not resolve.** Gated repo, hub down: it lands in
  `unresolved` and is printed by name with the reason. A list that quietly
  shrinks is indistinguishable from one that was always that short.
- **A launch fails.** The runtime's own fatal line becomes the failure
  immediately, ahead of the deployment record; the stepper stops rather than
  ticking on towards Serving.
- **One source of `/api/models` fails server-side.** It arrives in the payload's
  `sources` block as `{ok: false, reason}` and is printed with the others. One
  provider's catalogue failing costs that provider's un-served rows and nothing
  else.
- **The hub is rate-limited or slow.** `searchError` joins the same problem list;
  hub rows already on screen are kept whatever the box now says, because
  re-filtering would blank the hub's whole contribution for 450ms on every
  keystroke.
- **A picked chat model stops serving.** `activeRow` is derived, not held in an
  effect, so the selection falls back on its own instead of leaving Send pointed
  at a name the gateway will now refuse.
- **A speech or transcription request is aborted.** `chatStream` swallows its own
  abort and returns a stopped meta; `speech()` cannot — there is no partial audio
  to keep — so the turn is marked stopped rather than failed.
- **A link measurement or reach check is refused.** The message appears against
  that pair's button. Neither disables the other.
- **No capacity answer yet.** `ModelsTab` says "No capacity answer yet, so
  nothing here is banded by fit" rather than banding rows against nothing.

## Deliberately not built

**A planner bar on the dashboard.** Removed 2026-09-07. Serving happens on a
model's own URL, in `models/ServePanel.tsx`, because that is where the question
travels in the link.

**Context and sequences fields above the model list.** They asked for a number in
the units of a thing nobody had picked yet, and duplicated the pair inside the
Serve panel. The coordinator derives a context per model; the override is behind
one disclosure, on one screen.

**A `/speech` destination.** It existed for the length of one gap and was retired
on 2026-09-08. Two screens reading `row.modality` is two places to keep in step
with what the server accepts.

**Stored chat transcripts.** Not a setting, not a flag, not a server route. The
absence of a persistence path is the guarantee.

**A "large enough for roughly N billion parameters" line on the machine step,**
and a "saved vs all-cloud" tile on Spend. Both would be a screen's own arithmetic
over an assumed constant, and both questions are answered properly elsewhere —
per model, by the gate a launch actually goes through.
