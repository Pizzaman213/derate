# tabs/models

The model surface: what this cluster could run, what each quantization of it
costs, and the only place in the product where anything is launched. Every
verdict on these screens is read off the wire and rendered as the fit gate
wrote it — nothing here computes a fit, sizes a cache, or ranks a machine,
because a second copy of that arithmetic in the browser is a second answer, and
the one that disagrees with the gate is the one that costs somebody an
out-of-memory kill.

One flat list, not five source tabs. Provenance is a *set* of facets and the
band is the verdict, so `openai/gpt-oss-120b` is one row that says it is
curated, cached and serving rather than three rows each saying a third of it.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `rows.ts` | 933 | the row model: facets, bands, the joins, sorting, search |
| `ModelInspector.tsx` | 648 | one model — where it would run, what you can get it as |
| `ServePanel.tsx` | 503 | "run it here": plan, fit gate, machines, sparkrun |
| `Verdict.tsx` | 536 | the dry run and the refusal in one box, both budgets shown |
| `QuantLadder.tsx` | 1048 | one quantization already chosen, the rest a click away |
| `ladder.ts` | 62 | the ladder's two pure derivations, out where a verifier can reach them |
| `board.ts` | 287 | the machine board's arithmetic, with no DOM in it |
| `NodeBoard.tsx` | 230 | the board itself: the facts you need to overrule the planner |
| `CatalogList.tsx` | 275 | the fit-banded row list; rows are real `<button>`s |
| `CardGrid.tsx` | 96 | the card view, with one avatar subscription for the whole grid |
| `ModelCard.tsx` | 171 | one model as a card, written from Unsloth Studio's behaviour |
| `BandSection.tsx` | 84 | the band heading, and the one band that draws collapsed |
| `owner.ts` | 221 | publisher accent, initials, and the avatar lookup through the coordinator |
| `dominant.ts` | 101 | the accent taken from the avatar's own pixels |
| `support.ts` | 131 | the red dot: whether any runtime here loads the format at all |
| `RouteCard.tsx` | 211 | serving by routing to somebody else's hardware |
| `BackendPicker.tsx` | 131 | pinning OpenRouter's backend host for one model |
| `PullCard.tsx` | 205 | putting a model on a server derate does not launch |
| `pullTargets.ts` | 42 | which providers can be told to fetch weights, from the server's kind table |
| `ProviderPicker.tsx` | 94 | which machine a pull lands on |
| `ollamaTarget.ts` | 102 | `hf.co/<repo>:<tag>`, and the ladder partition that goes with it |
| `UnchosenBanner.tsx` | 104 | a provider serving its whole catalogue, and the one click that ends it |
| `CapabilityChips.tsx` | 104 | MoE, MLA, GQA, sliding window, vision, MTP, context — as tags |
| `ParamBreakdownTable.tsx` | 82 | where the parameters are, and why two totals differ |
| `QuantTableCard.tsx` | 85 | the quantization table exactly as the gateway reports it |
| `DegreeFields.tsx` | 102 | TP and PP, owned as a set; `null` means the planner picks |
| `SpeculativeField.tsx` | 95 | which speculative method to serve with; `null` means one token per step |
| `CustomServes.tsx` | 58 | replaying a hand-written launch command |
| `InstalledModelsCard.tsx` | 49 | what is already on the cluster's disks, read-only |
| `rows.check.mjs` | 726 | the browser-side fold against the live coordinator's real payloads |
| `registry.check.mjs` | 213 | `GET /api/models`, the fold that moved to the server, and the boundary |
| `serve.check.mjs` | 122 | launch permissions, including branches a live cluster cannot reach |
| `ollamaTarget.check.mjs` | 118 | the target string and the ladder partition |

## `rows.ts`

The row model, and the file every other one here reads a type from. `ModelRow`
keeps its facts apart instead of pre-joined into a string — the list it feeds
used to receive `{label, detail, group}` with `detail` already concatenated,
which is why it could never badge fit, sort by size, or say what was on disk.

`Facet` is where a model was found and `FACET_ORDER` is the order the six are
canonicalised into: `running`, `ondisk`, `catalog`, `provider`, `offered`,
`hub`. `Band` is the verdict — `running`, `fits`, `degraded`, `unchecked`,
`wont`, `elsewhere`, `unserved` — and `band()` reads reality before prediction:
on this cluster `openai/gpt-oss-120b` is serving right now and the capacity walk
at 8192/1 refuses it, because the live budget already has that deployment's own
memory taken out of it. Filing a model that is answering requests under "Needs
more memory" would be flatly false. `TERMINAL` (`failed`, `stopped`) is exported
so `board.ts` shares one definition of "over": a stopped deployment holds no
machine.

`capacityIndex(...reports)` is variadic because verdicts arrive from two places
— the 20s cluster-wide poll and the targeted batches the auto-check fires — and
a later report wins for a model both name. It prefers each report's live side
and records `basisOf(modelId)` per model, so a row never claims a basis its own
answer was not taken at.

`cacheIndex(report)` joins `/api/storage` by repository id. Two rules in it are
incident-shaped. Bytes are the **largest** figure any node reports, never the
sum: this cluster registers `spark-4d38` and `probe-worker` on the same host,
both reporting the same 52 repositories, and summing there claimed 364 GiB for a
182 GiB download. And `blob_count === 0` is skipped exactly — four of this
cluster's 52 cache entries are directories a resolve touched and wrote nothing
into, and counting them as cached made the card promise a download that had not
happened. `complete()` needs a measured expected size and uses
`COMPLETE_FRACTION` (0.99), because a cache holds symlinked snapshots whose
reported sizes jitter and an exact test calls a finished download partial.

`registryRows` copies `GET /api/models` — `where` arrives canonical,
deduplicated and non-empty, because the five-source fold now happens once,
server side, over one instant instead of over five polls on different intervals.
`remoteOnly` and `unservedOnly` are still derived here, from `where`, because
`band()` reads them. `withHubHits` is the one merge left in the browser and it
only ever *fills gaps*: `/api/models/search` resolves nothing, so a hit is a
query's answer rather than a fact about this cluster. Keyed on `model_id`
exactly, never case-folded — OpenRouter spells things `qwen/qwen3-30b-a3b` where
the hub spells them `Qwen/Qwen3-30B-A3B`, and folding would hand an un-served
remote row a local verdict for weights it is not the same thing as.

`mergeRows` and the six builders (`catalogRows`, `onDeviceRows`, `hubRows`,
`runningRows`, `providerRows`, `offeredRows`) survive as the browser-side fold
the server's now mirrors, and `rows.check.mjs` is the only caller left for five
of them — `hubRows` is the exception, because `withHubHits` still runs it on the
live path. `providerRowsFor` is exported for the detail pane, which needs the
answer for a model the list never produced a row for. `deploymentSignal` is
exported for the same reason `TERMINAL` is — `CatalogList` must not disagree with
the ladder about what `degraded` looks like — and `BAND_TITLE` is the one place a
band's heading text is written.

`isCollapsibleBand` is pure and lives here rather than in a component so the
verifier can hold it to the rule: exactly one band hides itself, and it is
`unserved`, the one with no local verdict in it. `bandSubtitle` names the
publishers rather than counting them — "428 from OpenRouter" is checkable and
"428 from your providers" is not. `compare()` sorts inside a band and never
across one; on `size` it never compares a parameter count against a byte count,
because falling back to `bytesOnDisk` put a 120e9 parameter count and a 195e9
byte count in one comparison as though they were one quantity. `bytesOnDisk`
survives only as the tie-break among rows that have no measured parameter count
at all.

## `ModelInspector.tsx`

One model, in two panes (`Serve`, `About`) held in component state rather than
the URL — the URL names the screen and its subject, and which of two views of
one subject you are reading is neither. It fetches `modelDetail` and
`modelVariants` itself rather than taking them from `state/resources.ts`, because
`useResource` refires on every `revision` bump and an open model would re-run its
hub calls after every launch, admit and settings change. The two loads are shown
separately: detail is one resolve and usually cached, the ladder is a search plus
a repository read per GGUF repo and takes seconds.

`target`, `runtime` and `providerId` are owned here, not in the ladder, because
the plan call above and the launch below have to agree about them — two copies
would let the verdict describe one shape while Serve started another. Storage and
cluster are polled once here and handed to both halves for the same reason a
second `useStorage` would be a second cluster-wide disk walk every 30s.

`routeTargets` assembles both provider surfaces — the filtered listing knows
health and admission for what is served, the catalogue knows what is merely
published — and counts a provider in both once, on the served side. A `seq` ref
is bumped on every model change so a response for a model the pane no longer
shows cannot land. The runtime follows the model once on arrival, not at render:
a speech checkpoint is *refused* on vllm rather than merely slow, so the default
must move, and picking vllm afterwards to read its refusal has to keep working.

## `ServePanel.tsx`

"Run it here", and since 2026-09-07 the only plan surface in the product. The
dashboard's planner bar answered the same question from a screen that had not
chosen a model; this one carries the question in the URL, and the machine board
stands where that bar had a checkbox popover, because choosing between machines
needs the figures a popover hides.

The plan effect debounces at 450 ms and short-circuits under a provider runtime:
`POST /api/plan` asks whether a model fits on a machine in *this* cluster, and a
verdict about the wrong memory pool would arrive looking exactly like one that
meant something. `context`, `concurrency`, `node_ids` and `parallelism` are all
**spread, never sent as null** — absence is the contract for "the coordinator
picks", and an explicit null is a different request. `launch()` then sends
`result.context` and `result.concurrency`, the numbers the verdict was actually
taken at, not what the boxes show.

`granted` is cleared on every replan so an override cannot outlive the
measurement that justified it, and the launch spreads one key per gate the
backend itself published rather than a fixed set of names. On success it
navigates to `{dest: 'cluster', dep: dep.served_name}` — `served_name`, never
`deployment_id`, because `?dep=` is matched against the served name and an id
there does not fail, it falls through to `defaultDep()` and silently selects
somebody else's deployment.

The stale-node effect drops a ticked machine that has left the cluster, and its
`every` short-circuit is not an optimisation: without it a fresh array is written
on every 5s cluster poll, which refires the plan effect forever.

## `Verdict.tsx`

The dry run and the refusal in one card. It renders **both** fit results — one
against the static ceiling, one against what the machine can hand out right now —
and lets the backend's `serve` decision say which governs the button. A gateway
that predates `serve` omits the field, and treating that absence as a refusal
would red-flag every model on an older coordinator, so `legacy` falls back to the
static verdict.

`gates` are the permissions, and `canServe` appears only once every one of them
is ticked. `claimFor` writes the checkbox label in the operator's first person —
"Serve anyway. I am overriding the live fit gate, which measured N GB allocatable
and refused" — because it is a claim they are making, not a message they are
dismissing; the server's own sentence renders verbatim directly above it either
way. Its final fallback return matters as much as the two named `param` branches
(`allow_over_live_memory`, `allow_mixed_hardware`): a gateway that adds a third
gate tomorrow still gets a blocking control here — "Launch anyway. I have read
the reason above and am overriding it." — instead of this client launching past a
permission it did not recognise.

`NoVerdict` covers an unwired fit port: the plan is real and shown, the absence
of a verdict is stated, and Serve is withheld. The six-term `SegmentBar` marks
`shown.limiting_term`, and the breakdown is taken from the live result because it
is identical under both budgets.

## `QuantLadder.tsx`

One quantization already chosen, with the rest a click away. A repository can
publish forty variants and a forty-row table asks a question most people cannot
answer, so the card opens on `ladder.recommended` — the gateway's largest variant
that both fits and can be served — falling back to the best servable row, then to
the top of the ranking, because "here is the closest thing, and here is why you
cannot run it" is an answer and an empty card is not.

The rest split in two, and the split is the point. Most of what a hub search
turns up for a popular model is GGUF and there is no llama.cpp runtime here: for
Qwen3-30B-A3B, thirty-seven of forty-two. The gateway ranks on fit before
servability — `capacity_api.py::_rank_key` sorts on fit tier then size and never
consults `launchable` — so a 30 GB GGUF that fits outranks the smaller
safetensors row that is the only thing vLLM can start.
`partitionForRuntime` fixes what is seen without touching the ordering — rank
order is preserved exactly inside each group — and re-sorting in the browser
would be the second answer that misleads.

**Where the launchable rows land is a snapshot, not a property.** This passage
used to record them at 29, 30, 31 and 39; today they are at 1, 38, 39, 40 and
41. It moves with the hub listing and with every change to the fit arithmetic,
because the fit tier is the outer sort key — the KV-margin commits of
2026-09-10 were enough to shift one launchable row to the top. `rows.check.mjs`
therefore prints the positions as evidence and asserts only what does not move:
that the partition preserves the gateway's order and never becomes a second
ordering. An assertion on the magnitude was there until 2026-09-11 and was
inverted — fixing `_rank_key` would have broken it. Serve is **absent** on a row that
cannot be served rather than present and dead: a disabled button reads as "not
right now" when the truth is "not by this route at all".

Per-row state is keyed on `variantKey`, never `repo_id`, because one GGUF
repository publishes many quantizations and a repo-keyed state lights "Serving…"
on every sibling of the row that was clicked. A launch sends the variant's own
`repo_id` and *that row's* `context`/`max_seqs`, since on the default path the
gate chose one per variant. With nothing ticked it falls back to `sized_on.nodes`
— the machines the verdict was taken on — rather than to "the planner picks".

`refusalCode` maps a 409 to the field that reopens it, and it has to: the cluster
launch refuses with `live_memory_insufficient` / `allow_over_live_memory`, the
pull refuses with `pull_over_memory` / `allow_over_memory`, and matching only the
first left a real pull refusal rendering as a bare red line with the way past it
unreachable. An unrecognised code returns null on purpose — a checkbox that sends
a field the gateway ignores is a button that does nothing twice. 409 gets the
override path and 400 does not, because an unchanged retry succeeds once memory
frees.

`Served` reads the deployment record rather than printing one word, and reads
`pulled` instead when the runtime pulls — a pull never becomes a Deployment, so
looking for one left the chip reading "starting…" forever with nothing to explain
it. `OnDisk` distinguishes partial from present, because here there *is* a
measured size to check against: `Qwen/Qwen3-30B-A3B` on this cluster is 2 MB of
metadata and no weights.

## `ladder.ts`

`fitLamp` and `decodeLabel`, out of the `.tsx` so `rows.check.mjs` can bundle
plain TS and assert on them — what a verdict *looks* like is exactly the
judgement that wants a verifier, and a green `tsc` says nothing about either.

`fitLamp(variant, onCluster)` returns `idle` / "not judged here" under a provider
runtime, because `_variant_verdicts` filters to profiles with addressable memory
before it walks: the provider's box was excluded, not merely unaccounted for.
A `wont_fit` row whose `static_fits` is true gets its own label — "will not fit
right now — fits on an idle machine" — since that case wants somebody to go and
look at the machine rather than give up on the model. `decodeLabel` gates on
`variant.fits`, not on `verdict === 'fits'`: `fits` is true for `fits_degraded`
too, and that is the one row whose whole reason for existing is its decode
figure.

## `board.ts`

The machine board's arithmetic with no DOM and no React in it, split for the same
reason `tabs/cluster/layout.ts` is: it is the part that can be wrong in a way
types cannot catch, so it is the part the verifier bundles.

Nothing here scores a machine or ranks a set of them, and nothing server-side
does either — `Planner._nodes_for` takes the alphabetical prefix of the strongest
homogeneous group — so a ranking invented in the browser would be a second answer
with no arithmetic behind it. `buildBoard` joins the roster, `/api/memory`,
`/api/topology`, `/api/deployments` and the cache index into `BoardRow`s.

`allocatable` is `null`, never `0`, when nothing has polled the machine: an
unpolled node must not draw as a full one. Two rules withhold the tick rather
than offering a refusal — `NO_MEMORY` for a machine the gateway would 400 with
`node_has_no_memory`, and `RUNNING_IT(servedName)` for the one-copy-per-node rule
that answers 409. `unmeasuredPairs` exists because `LinkService.worst_all_reduce`
answers for the whole set or not at all: one unprobed pair makes the planner treat
every link as unknown and fall back to a conservative pipeline, which is invisible
otherwise and changes the plan. `linkMeasured` requires the wire's flag **and** a
figure, matching `tabs/cluster/layout.ts`. `toggle` sorts before handback so two
tick orders produce one request body, one answer and one server-side memo key.

## `NodeBoard.tsx`

The board itself: which machines this model would be served on, and the facts you
need in order to disagree with the planner about it. It exists because the
planner does not choose machines — live memory, whether the weights are already
on disk here, and what is already running here never enter `_nodes_for`, and
every one of those is knowledge a person has and the planner does not.

Rows are in cluster order, not an order this file invented. Columns are
allocatable-now against the ceiling, weights, link and what is running.
Unticking the last machine hands the choice back to the planner rather than
meaning "plan on nothing", which the gateway can only answer with a 400. An
ineligible machine renders the registry's own sentence through `Verbatim`.

## `CatalogList.tsx`

The fit-banded row list. Rows are real `<button>`s rather than `role="button"`
divs with hand-rolled key handling — that was two bugs waiting, since Enter and
Space were reimplemented per call site and the focus ring `base.css` gives every
native control never reached them.

Every fact on a row is read, not derived: the lamp is the fit gate's verdict, the
sentence under a refusal is the fit gate's own, and a row with no verdict draws a
hollow lamp. A `checking` row keeps the shape "unchecked" draws and changes only
the word — it is a state, not a fourth verdict. `Facts` prints an offer's price
as "not priced" when either side is null, never as `$0`, because the wire keeps
"never published a price" and "free" apart. `Numbers` draws a size only where one
was measured. `compact` restacks the row for a 360px master pane, where four
columns do not fit.

## `CardGrid.tsx`

The card view. **One subscription for the whole grid, not one per card**:
avatars and their extracted colours resolve asynchronously, and a hundred cards
each holding their own state would re-render the whole grid a hundred times as
they land. Here a batch landing is one `useReducer` bump that React reconciles
down to the cards that changed. Sections keep the same fit banding the row view
uses, through the shared `BandSection` helpers — a wall of cards with no band is
the thing this screen was supposed to stop being.

## `ModelCard.tsx`

One model as a card, with the geometry of Unsloth Studio's hub card — a fixed
tile, the publisher's mark large enough to recognise without reading, the name at
two lines, the numbers on the bottom edge. Written from that behaviour;
`studio/**` is AGPL-3.0-only and none of it is copied.

Three things are said with colour and nothing else: the accent, which identifies
the publisher and means nothing more, and the status dots, which are the app's
own `Lamp` at `size={6}` doing its usual job. There is no colour for "popular" or
"new". Every dot has a sentence behind it, and that sentence is also the card's
`aria-label`, so it is not mouse-only. The on-device dot says what is cached and
how much, and deliberately does **not** promise the first launch will skip the
pull — there is no expected size for a base repository to check against.

A bare repo id (`gpt2`) has no publisher, which is different from a publisher we
could not identify, so the owner line is omitted rather than drawn as an em dash.

## `BandSection.tsx`

`BandHeading` and `useBandCollapse`, shared by the row list and the card grid
rather than written twice: two copies of "which band hides itself" is one too
many, and the two views must not disagree about whether four hundred models are
on the screen. `expandAll` is the search — a needle that matched inside a
collapsed band has to show what it matched — and it forces the band open without
touching what the person clicked, so clearing the search puts it back. The count
is on the heading, because a band that does not say its size reads as a short one.

## `owner.ts`

Publisher identity: `ownerAccent` (ten hues, hashed, deliberately excluding the
signal colours so an accent cannot land on green and read as a verdict),
`ownerInitials`, `isFirstParty` (ten names whose own repositories are the
canonical upload — not a quality claim), and the avatar lookup.

**The avatar lookup goes through the coordinator now, and that is the whole
point of the file.** What was here before went straight to the hub, once per
publisher, on every page load, with an LRU, a six-wide semaphore and an
exponential backoff — all of it trying to stay under an unauthenticated rate
limit that a single Models grid is already over. It did not work: ~45 publishers
tripped the limit, the backoff doubled from a minute towards half an hour, and
every card sat on two letters for that whole window. It looked intermittent
because the limit is a burst window that recovers on its own.
`control_plane/resolver/avatars.py` asks the hub once per publisher ever and
serves the bytes same-origin over `GET /api/publishers/avatars?owners=…`, so
`avatarUrl(owner)` is synchronous, safe to call from render, and queues a name it
has not seen. `subscribeAvatars(fn)` is how a grid hears that a batch landed —
one listener for the whole grid, never one per card.

Four constants carry the loop: `BATCH` (64, matching the server's own cap),
`COALESCE_MS` (16 — a grid mounts its cards in a burst, so waiting a tick turns
~90 calls into one request), `RETRY_MS` (1500), `MAX_STALLS` (4, counting
*unproductive* rounds only, since 93 publishers is two full batches and neither
is a stall) and `MAX_ATTEMPTS` (4 per publisher, so a batch where 63 of 64
resolve does not poll our own coordinator for the 64th for as long as the tab is
open). A publisher absent from the answer is still resolving, so it goes back in
the queue rather than being recorded as having no mark — until its fourth
unsettled round, where `requeue` writes `known.set(owner, null)` and it settles
as a monogram. Absent from one answer is not "has no avatar"; absent from four
is.

## `dominant.ts`

The accent taken from the avatar's own pixels, which is what makes a grid read as
a catalogue of products rather than a table with pictures. Reimplemented from
Unsloth Studio's behaviour, not its source.

`dominantColor(url)` is synchronous like `avatarUrl` and `subscribeDominant(fn)`
is its half of the grid's single subscription. The read is same-origin now that
the coordinator serves the avatar, so the canvas cannot be tainted at all — it
used to depend on `cdn-avatars.huggingface.co` sending
`access-control-allow-origin: *`, which was true but was somebody else's header
to change. The guard stays: a throw or a failed load returns null and the
card keeps its hashed palette colour. `SAMPLE` is 16, because averaging a 512px
logo costs real time on a grid and gives the same answer. `pick` weights by
saturation squared and drops near-white and near-black pixels — averaging every
pixel of a logo on a white field returns a pale grey, the one colour that carries
no identity at all.

## `support.ts`

The red dot: whether anything here could load this at all. Separate from fit, and
it has to be — fit asks whether the bytes go in, this asks whether a runtime
understands the format, and merging them would tell somebody to free memory for a
file that would never have loaded.

`classifySupport(row, table, canPull)` is the whole public surface and returns a
`SupportVerdict` — `ok` / `unsupported` / `unknown`, with the sentence that goes
with it. Every judgement is read off `GET /api/models/quant-table`, so a scheme
gaining vLLM support server-side lights up here with no edit.
`SERVABLE_PIPELINES` is the one local list, and `text-to-speech` is in it
because the `tts` runtime serves `/v1/audio/speech`; whether a *particular* TTS
checkpoint loads is the narrower
question the runtime rows answer, so such a model still gets its red dot with the
architecture named. A `gguf` family is refused outright, mirroring
`gateway/serialize.py:_launchable`, and with a pullable provider configured the
route out is *appended* rather than replacing the sentence.

`detectScheme` prefers `quant_hint`, then `nativeDtype` — **never `dtype`**.
`dtype` is what the fit gate would step *down* to, a recommendation about a
different set of weights, and reading it here put a red "q4_k_m is a llama.cpp
format" dot on `Qwen/Qwen3-30B-A3B`: a bf16 safetensors repo, and the one model on
this cluster that both fits and can be served. Table keys are matched longest
first, so `iq4_nl` is not read as a shorter scheme and `fp16` is not read as
`fp8`. Unsupported is marked, never hidden — a list that quietly drops what it
cannot run is lying about the hub.

## `RouteCard.tsx`

The other half of the Serve pane: the question with no launch in it. A provider
already runs this model and the only decision left is whether this cluster's
`/v1` carries its name. It is one switch, and the switch is the provider's
allowlist — `ProviderService.servable()` feeds `Router.rebuild`, so `/v1/models`,
routing, `/api/topology` and the chat picker all change together with no restart.

**The guard is the important part.** The allowlist is written as the complete set
rather than a delta, so `ready` requires a non-empty catalogue: a write computed
from a catalogue that failed to load would send exactly one id and switch off
everything else the provider serves. There is no safe partial version of that
edit, so without the catalogue the button is not offered at all. `materialises`
states the grandfathered consequence *before* the click — with no allowlist ever
written, switching one model off necessarily writes the other N−1 as an explicit
choice. `healthy == null` renders no lamp, because a merely-offered model carries
no health and a guess would draw red beside a healthy provider.

## `BackendPicker.tsx`

OpenRouter multiplexes one model id over several backend hosts (Anthropic direct,
Bedrock, Vertex…) and picks one per request unless told otherwise. This is that
"unless told otherwise": a live list from the provider's own endpoints, and a pin
that rides along on every request thereafter — `ProviderService._prepare` injects
`provider: {only: [tag]}`.

Self-hiding rather than error-prone. A kind with no `supports_backend_routing`
answers 400 for every model it serves, which is the ordinary answer on a pane
mostly full of OpenAI, Together and Ollama providers, so
`backend_routing_unsupported` sets `unsupported` and the component renders
nothing. It also renders nothing at one row or fewer — there is no choice to
make. The pin is a single-key patch: `backend_pins` merges, so this never has to
know or resend the provider's other pins.

## `PullCard.tsx`

Putting a model on a server derate does not launch. A box with no GPU cannot run
vLLM or SGLang, so it joins as a provider instead and the way to get a model onto
it is to tell it to fetch one.

This card is now the escape hatch for Ollama's own names (`qwen2.5:0.5b`,
`gemma3:270m`), which no ladder row can express; the quantization ladder is the
primary route. Its own docstring records the reversal: it used to argue the pull
was deliberately a second namespace because none of the ladder's facts survive
the trip, and half of that is still true — fit verdicts do not, which is why the
ladder suppresses them under that runtime — but `file_bytes` is a measured
download and it is the number the pull gate weighs.

**The empty state is not nothing.** With no provider configured this rendered
nothing at all, on the reasoning that a control which cannot act is worse than an
absent one. That was wrong in the way that matters: the board directly above has
just told the operator that a GPU-less machine cannot carry a rank, and the one
path that does work was invisible, so the screen read as "this machine is
useless" with nothing to disagree with it. It now names the GPU-less machines and
says where to enable them. A 409 is the memory gate and gets the override path; a
400 does not. The accepted reply says where the progress bar is and that
restarting the coordinator cancels the transfer.

## `pullTargets.ts`

`pullableProviders` derives targets from the server's kind table —
`supports_pull` is `bool(pull_path)` on the kind spec — and never from a literal
`'ollama'`, so a build that teaches a second kind to host its own weights becomes
a target here with no change on this side. That is the whole reason
`GET /api/providers/kinds` exists instead of the form restating the table.

`alreadyOn` is case-insensitive and deliberately only ever decorative: whether
Ollama preserves the case of an `hf.co/<repo>:<tag>` ref through its catalogue is
not something this codebase has verified against a live server, so a mismatch
must cost a chip that failed to appear, never a button that refuses to work.

## `ProviderPicker.tsx`

What `NodeBoard` is for a cluster launch, this is for a pull: the same shape of
choice against a different set of machines. It reads facts and sizes nothing.

**Deliberately no free-memory column**, though the pull is gated on exactly that
figure. It comes from matching the provider's URL against the roster by exact
address (`_provider_host_memory`), and reproducing that join here would be a
second implementation of the gate's denominator — the one that disagreed would be
the one that let somebody fill a disk. The server says what it weighed, in the
reply. The health lamp is hollow rather than blocking when the last refresh
failed: a pull may be exactly what fixes an empty catalogue.

## `ollamaTarget.ts`

`ollamaRef(variant)` addresses a ladder row as something Ollama can fetch, as
`hf.co/<repo>:<quantization>` — verified against Ollama 0.33.3, where
`hf.co/bartowski/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M` resolves the manifest and
downloads. That form keeps one model namespace across the screen, so the row you
picked and the thing that gets served are the same object.

The tag is the publisher's `label`, not the canonical `dtype`: a repository
publishing `UD-Q4_K_XL` has no file called `q4_k_m`. `runnableOnOllama` also
excludes `shard_count > 1`, and that is not a guess — Ollama 0.33.3 refuses a
multi-part repository at manifest resolution with "Ollama does not yet support
pulling sharded GGUF via the registry". `variantKey` is `repo_id::gguf_file ??
label`, because `Q8_0`, `Q6_K` and `Q5_K_M` all live in one repository.

`partitionForRuntime` splits on `launchable` under vllm and sglang and on
`runnableOnOllama` under ollama, preserving the gateway's rank order in both
branches. It is not a second answer to the gateway's question: `launchable`
mirrors what `POST /api/deployments` will decide, and under ollama that endpoint
is never called.

## `UnchosenBanner.tsx`

A provider serving its whole catalogue because nobody ever chose. Enrolling
OpenRouter used to put every one of the several hundred models it publishes onto
this cluster's `/v1/models`; the allowlist fixed that going forward, but a record
written before it keeps serving everything, deliberately, because the alternative
is an upgrade that silently stops routing.

`unchosenProviders` requires two conditions. `models_chosen === false` and
nothing weaker — `null` is "this port cannot say", and the counts cannot stand in
for it, because an operator who switched everything on has
`model_count === catalogue_count` exactly like a legacy record. And a published
catalogue: a record saved before its first successful refresh has an empty model
list, so "Serve none" there would pin it to serving nothing forever for having
been saved at the wrong moment. The button patches `enabled_models: []`, which is
a choice — "serve nothing" — and a different record from the absent value the
banner is about.

## `CapabilityChips.tsx`

MoE, MLA, GQA, sliding window, vision, MTP and context, as `.pill`s rather than
`.chips button`: these report, they do not toggle. Category is carried by the
text, never by colour.

Every number arrives computed, so a chip cannot disagree with the fit gate about
the same model. The MLA chip's figure is the width cached per layer per token —
the latent *plus* the decoupled RoPE dimension, not the latent alone. The MTP
chip is stated rather than hidden: the checkpoint carries the module and the hub's
weight index counts it, but no runtime loads it unless speculative decoding is on,
so `total_params` excludes it and the download is bigger than the load.

## `ParamBreakdownTable.tsx`

Where the parameters are, over nine rows from embedding to output head. The MTP
row is the reason this is a table rather than one figure: `total` is what a
runtime loads and `total_with_mtp` is what a weight index on the hub counts, and
showing only the first leaves a reader to discover the gap between our number and
the repository's own. `billions()` renders a missing count as an em dash and
never as 0 — a real zero claims a bucket is empty, which is a different statement
from not knowing.

## `QuantTableCard.tsx`

The quantization table exactly as the gateway reports it, because it is the
reference somebody needs while reading a ladder: what "4.90 bpw" means, which
schemes need which silicon, and which runtime will admit to loading them. Fetched
rather than held in TypeScript — a second copy of these figures is a second
answer, and the one that disagrees with the fit gate costs a failed load.

It takes no props and calls `useQuantTable()` itself rather than being handed the
tab's copy — the one exception to the hand-it-down rule above, and affordable
because that hook polls at 3,600,000 ms, once for the life of the tab.
The runtime columns are read off the payload's first row, never typed here. There
were two columns and three runtimes the day `tts` landed, and a hardcoded pair
does not render as a missing column: it renders as a complete table that quietly
omits one of the answers the caption promises.

## `DegreeFields.tsx`

TP and PP, owned as a set. Per-axis handback reads well and encodes badly: with
`parallelism` sent as an object an omitted key means 1, so "TP mine, PP the
planner's" cannot be expressed on the wire at all. The whole object is adopted on
the first edit and handed back by one Reset, and `null` means the planner picks
and sends no `parallelism`. The placeholder shows the degrees in force, so an
untouched field displays the planner's answer in muted type.

Nothing here checks legality. Head divisibility, layer counts and the bandwidth
thresholds are the planner's arithmetic. `idPrefix` exists because
`ModelInspector` is mounted from the Models tab *and* the sheet while `AppShell`
keeps every destination mounted, so with one hardcoded id clicking "TP" in one
would focus the other's field.

## `CustomServes.tsx`

`CustomServesCard` replays a hand-written launch command from a previous launch
— a quantization choice, a memory knob, anything the standard recipe does not
cover — so it is not retyped. Backed by `state/customServes.ts` and empty until
the first launch that used one. Clicking an entry only *seeds* the Verdict
card's field on the model it names; nothing here launches anything.

## `InstalledModelsCard.tsx`

What is already on the cluster's disks, read-only. The same `/api/storage` report
Settings → Storage draws, taken as a prop rather than polled again — a second
`useStorage` would be a second cluster-wide disk walk every 30s for a payload
this tab already has. The per-node tables are `components/CacheTable`, so the two
screens cannot render the same cache differently. Deleting a cached model stays a
Settings → Storage action; this only says what is there.

## `rows.check.mjs`

`// requires: coordinator`. The browser-side fold against the live coordinator's
real payloads. There is no test runner in `ui/` and typecheck is the only other
gate, so this esbuild-bundles `rows.ts`, `support.ts`, `owner.ts`, `board.ts`,
`ladder.ts` and `api/client.ts` and drives them with what `/api/capacity`,
`/api/storage`, `/api/catalog`, `/api/models/search`, `/api/models/variants`,
`/api/cluster`, `/api/deployments`, `/api/memory`, `/api/topology`,
`/api/models/quant-table`, `/api/providers` and each provider's `/models`
actually answer. `DERATE_CHECK_ORIGIN` overrides `:8088`, because this box's
coordinator is shared and a check that can only talk to one port cannot run
against a throwaway on 18xxx.

What it holds: no row without a verdict carries an invented number; every verdict
on the list is one the capacity report contains; a hub hit the walk never resolved
bands as `unchecked`; a serving model bands as `running` whatever the walk says at
those numbers; a finished deployment does not claim that band; `checking` and a
verdict are never both set; banding loses no row and no sort crosses a band under
any of the four sorts; an empty cache directory is not a cached model; the board
withholds the tick from a machine with no addressable memory and says why; an
unpolled machine has `allocatable === null` and not a zero; two tick orders
produce one node list; an unmeasured pair among the ticked machines is reported.
The provider and offer rules are held to a *synthetic* catalogue as well, because
a live coordinator may legitimately have nothing switched off and a green screen
would otherwise skip them.

## `registry.check.mjs`

`// requires: coordinator`. The sibling of `rows.check.mjs`, split deliberately:
that file checks the browser's own fold, this one checks the fold that moved to
the server and the boundary between them. A source silently dropping out of the
server-side merge is invisible on a screen that still looks full, so it
reassembles the union from `/api/deployments`, `/api/catalog`, `/api/providers`
and each provider's `/models`, and asserts every id appears in `GET /api/models`.

Three boundaries it fixes in place. **No fit answer reaches the wire** — nine
fields checked by name — because a verdict is a function of (model, context,
concurrency, nodes) and this endpoint is asked none of them. **No row claims the
`hub` facet**, since `/api/models/search` resolves nothing and the browser adds
that facet. **No credential slot anywhere in the payload**, asked over field
names (`api_key`, `api_key_ref`, `base_url`, `backend_url`, `secret`, `token`) and
not by searching text: a substring test would miss `api_key: '***'` and would fire
on a `last_error` that correctly names the reference that failed to resolve.

## `serve.check.mjs`

Hermetic — nothing is bundled and esbuild is not involved. It covers the
derivation of launch permissions from `serve` and whether the Serve button may
appear, both pure functions of wire data, and both with branches a live cluster
cannot currently reach: a gateway that predates `serve.overrides`, two gates at
once (the button stays away until *both* are ticked), a gate that is needed while
the fit itself passes — the case `allowed: false` could never express, and the
reason `overrides` exists — and a hard refusal that offers no way through. The
four-line derivation is restated here rather than imported because it lives inside
components that pull in React; the branches are what matter. This was
`tabs/dashboard/planner.check.mjs`, and it lost its other half with the dashboard's
planner bar.

## `ollamaTarget.check.mjs`

Hermetic apart from esbuild. It bundles `ollamaTarget.ts` and pins the exact
string against Ollama 0.33.3, the publisher label winning over the canonical
dtype, a non-GGUF row and a sharded repository both being no target, trimmed
stray slashes, and two tags of one repository producing two `variantKey`s. The
partition tests set `launchable` deliberately the wrong way round for each
runtime, so a branch that quietly read the other field shows up; it also asserts
that reversing the input reverses the output — the gateway's rank order is
preserved, not recomputed — and that sglang partitions exactly as vllm does,
because the split is on the verb, not the engine name.

## The seam with `ModelsTab` and the shell

`tabs/ModelsTab.tsx` is the screen. It polls once through `state/resources.ts`
(`useModelRegistry`, `useCapacity`, `useStorage`, `useProviders`,
`useProviderKinds`, `useProviderCatalogues`, `useQuantTable`) and hands the
payloads down, because `useResource` does not deduplicate — every call is its own
interval and its own request.

```ts
const rows = decorate(
  withHubHits(registryRows(registry.data), hits),
  capacityIndex(capacity.data, ...batches),
  cacheIndex(storage.data),
  checking,
)
// A hub row is kept whatever the box now says: the gateway already narrowed
// those hits to the needle IT was given, and re-filtering would blank the hub's
// whole contribution for 450ms on every keystroke. Format is its own pass.
let kept = q ? rows.filter(r => r.where.includes('hub') || matches(r, q)) : rows
if (format !== 'all') kept = kept.filter(r => matchesFormat(r, format))
const groups = groupRows(kept, sort)
```

`CatalogList` and `CardGrid` take those groups. `ModelInspector` is mounted from
two places — the Models tab's detail pane and `shell/Sheet.tsx` — which is why
`DegreeFields` takes an `idPrefix`. `AUTO_CHECK` in `ModelsTab` is 10: each
verdict-less row is a hub round trip on a cold cache, so about a screenful gets a
real answer and everything below keeps saying "not checked", which is true.

Placement (`?on=`), context and concurrency live in the URL via
`state/placement.ts` and `state/routes.ts`; `state/runtime.ts` owns
`servesOnCluster` and `shardsAcrossNodes` because neither `tabs/dashboard` nor
`tabs/models` can own it without the other importing it. Outward, this folder
supplies `RowProvider`/`RowDeployment`/`RowOffer` re-exports from `api/types.ts`,
and `rows.check.mjs` drives `board.ts` through `api/client.ts`'s own
`toNodeState` — the normaliser the app already applies to `/api/cluster` — rather
than through a second reading of the schema that would agree with any bug that
came from the first.

## Things that look like details and are not

**Provenance is a set and the band is the verdict.** They are different
questions, and a scalar `where` cannot answer the first: a model can be curated,
cached on disk and serving at the same instant. Every screen in this folder reads
`Facet[]` for "where did this come from" and `band()` for "what does this cluster
say about it", and nothing derives one from the other except the two flags
`registryRows` computes (`remoteOnly`, `unservedOnly`) precisely so `band()` and
the detail pane cannot disagree.

**Model ids are never case-folded.** Not in `mergeRows`, not in `withHubHits`,
not in `offeredRows`. OpenRouter spells things `qwen/qwen3-30b-a3b` where
HuggingFace spells them `Qwen/Qwen3-30B-A3B`, and folding would produce one row
inheriting a local `fits` verdict for weights the provider may not be serving.
That is an identity inference, and this UI does not invent. The cost is a visible
near-duplicate, which is the honest picture.

**`nativeDtype` is what the repository holds; `dtype` is a suggestion.** `dtype`
is the rung the capacity walk stepped *down* to. Reading it as a fact about the
repository put a red "llama.cpp format" dot on the one model on this cluster that
both fits and can be served.

**Serve is absent, not disabled, where it cannot work.** In the ladder, on the
board's unselectable rows, on the degree fields under a runtime that does not
shard. A disabled control reads as "not right now"; the truth in each of these
cases is "not by this route at all", and offering the control anyway means
offering a refusal the client already knew about.

**Absence is the wire contract for "the coordinator picks".** `context`,
`concurrency`, `node_ids` and `parallelism` are spread into the request body, and
an explicit `null` is a *different* request. Every launch then sends back what the
verdict was actually taken at — `result.context`, `variant.max_seqs`,
`sized_on.nodes` — never what a field happens to display.

**Overrides never outlive the measurement.** `granted` is wiped on every replan
in `ServePanel`; `refusal` and `override` are cleared on every attempt in
`QuantLadder`. A tick granted against one live-memory reading must not ride along
with the next plan.

**409 is not 400.** A live-memory refusal is legal and an unchanged retry
succeeds once memory frees, so it gets the override path. A static refusal is a
400 and correctly gets the error line.

**The fit gate's sentence is rendered verbatim or withheld.** `Verbatim` is used
for every refusal, every ineligibility reason and every resolver error here.
Under a provider runtime the ladder withholds it entirely rather than
paraphrasing: the promise the verbatim rule makes is that the numbers in the
sentence are the numbers the gate used, and printing a sentence about this
cluster's memory beside a button that starts something elsewhere breaks it.

**No API key is rendered, and `registry.check.mjs` asserts it over field names.**
`ProviderFacts` draws nothing key-shaped, not `api_key_ref` and nothing derived
from it.

## Failure behaviour

- **A coordinator that never answers the avatar batch.** `MAX_STALLS` (4)
  unproductive rounds stop the loop and every card keeps its monogram, which is a
  perfectly good card. A publisher scrolled into view afterwards resets the
  counter, so a bad minute does not permanently blind the grid.
- **One publisher that never settles.** `MAX_ATTEMPTS` (4) records it as having
  no mark, so 63 resolved names out of 64 do not keep the fourth-and-sixty-fourth
  polling forever.
- **A tainted or undecodable avatar.** `dominantColor` returns null and the card
  falls back to its hashed palette accent. Never a blank card, never a throw.
- **No capacity report at all.** Rows carry `verdict: null` and band as
  `unchecked` — the absence of a verdict, not a fourth one. `remoteOnly` rows band
  as `elsewhere` instead, because nothing here can resolve them and "not checked"
  would promise an answer that is never coming.
- **A provider catalogue that failed to load.** `RouteCard` withholds the switch
  entirely and shows the error, because the allowlist write is the complete set.
- **A gateway that predates a field.** `sized_on` defaults to a local basis and
  the caption degrades to today's wording rather than rendering "undefined";
  `serve` absent falls back to the static verdict; `overrides` absent
  reconstructs the single legacy gate with its param.
- **A ticked machine that leaves the cluster.** Dropped from the selection; if
  none survive, the choice goes back to the planner rather than planning against
  a name nothing answers to.
- **A model id nothing local can resolve.** `ModelInspector` renders the
  resolver's own sentence under "no local verdict" and omits `ServePanel`
  entirely when a provider serves it — there is nothing here to plan.
- **A 409 with a code this screen has no override for.** Rendered as an error,
  not as a checkbox that sends a field the gateway ignores.
- **A launch that dies before a deployment record exists.** The post-Serve chip
  says "starting…" for the second or two of real gap; under a pulling runtime it
  reads the accepted pull reply instead, because a pull never becomes a
  Deployment and waiting for one would hang forever.

## Deliberately not built

**A ranking of machines.** `board.ts` and `NodeBoard.tsx` both say so. Nothing
server-side ranks either — `Planner._nodes_for` takes the alphabetical prefix of
the strongest homogeneous group — so a score computed in the browser would be a
second answer with no arithmetic behind it. The board joins facts and lets the fit
gate say what they mean.

**A free-memory column on the pull picker.** The pull gate's denominator comes
from an exact-address join the server owns, and a second implementation of it here
is the one that would let somebody fill a disk.

**A legality check on TP and PP.** `DegreeFields` takes any positive integer and
lets the planner refuse in its own words. Head divisibility and bandwidth
thresholds are the planner's arithmetic.

**Re-sorting the variant ladder.** The gateway's `rank` is preserved exactly
inside both partitions. Partitioning changes what is seen; re-sorting would be a
second ordering, and the one that disagreed would be the one that misled.

**A second plan surface.** The dashboard's planner bar was retired on 2026-09-07:
two surfaces answering the same question is one more than the answer needs, and
this is the one that carries the question in the URL.

**Context and concurrency fields on the default path.** They used to be two
fields here and two more above the model list, asking somebody to name a window in
the units of a model they had not picked. The coordinator solves for the largest
context that fits at the best quantization that holds it, clamps to the model's own
window, and reports what it chose — so there is nothing to type, and the fields
live behind a disclosure with a "Choose for me" button that is the way back.
