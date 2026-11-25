## Layout

| File | Lines | What it owns |
|---|---|---|
| `rows.ts` | 1034 | the row model: facets, bands, the joins, sorting, search |
| `useModelList.ts` | 446 | the list's data: registry, capacity, hub search, auto-check, the four views |
| `query.ts` | 234 | the search box: Hugging Face links, typed filters, suggestions |
| `ModelToolbar.tsx` | 355 | view select, search combobox, filter chips, sort, Downloads, Provider |
| `ModelRows.tsx` | 352 | the fit-banded row list (fits, cluster and providers views), caption, empty note |
| `HubCards.tsx` | 133 | the Hugging Face view: task chips and cards |
| `WorthTrying.tsx` | 57 | undownloaded models that fit now, above the fits view |
| `DownloadsPanel.tsx` | 103 | launches and pulls in flight, read-only, plus the pull form |
| `SplitHandle.tsx` | 94 | the resizable split between list and detail |
| `useModelSession.ts` | 815 | the ONE owner of everything the plan request and the launch request share |
| `ModelPane.tsx` | 300 | one model: header, Serve / Advanced / History / Hugging Face, sticky footer |
| `VerdictBox.tsx` | 328 | the verdict word, where, per-node GiB, the primary button, both budgets |
| `NodeBars.tsx` | 282 | "Where it runs": one row per machine (tick, name, free/left/short), the split select, Why X? |
| `QuantSection.tsx` | 244 | Recommended picks, precision tiers, format filter; selecting re-plans |
| `LivePanel.tsx` | 143 | a running deployment's engine metrics and log tail |
| `AdvancedTab.tsx` | 527 | engine, speed, memory, context, parallelism, API name, custom flags |
| `HistoryTab.tsx` | 130 | this model's deployments, their events, "Serve again with these settings" |
| `HubTab.tsx` | 223 | hub stats, the repos on the hub, About, the quantization table |
| `gates.ts` | 118 | launch permissions: `gatesOf`, `canServeWith`, `claimFor`, `refusalCode` |
| `verdictView.ts` | 89 | the words a verdict is drawn with: `bandWord`, `planWord`, `formatParams`, `gib` |
| `quantPicks.ts` | 180 | tiers, formats and picks over server-judged ladder rows |
| `ladder.ts` | 81 | the ladder's two pure derivations, out where a verifier can reach them |
| `board.ts` | 357 | the machine board's arithmetic, with no DOM in it |
| `owner.ts` | 230 | publisher accent, initials, and the avatar lookup through the coordinator |
| `OwnerMark.tsx` | 70 | the publisher mark, drawn from `owner.ts` |
| `dominant.ts` | 103 | the accent taken from the avatar's own pixels |
| `support.ts` | 125 | the red dot: whether any runtime here loads the format at all |
| `RouteCard.tsx` | 212 | serving by routing to somebody else's hardware |
| `BackendPicker.tsx` | 121 | pinning OpenRouter's backend host (and its fallback) for one model |
| `PullCard.tsx` | 204 | putting a model on a server derate does not launch |
| `pullTargets.ts` | 42 | which providers can be told to fetch weights, from the server's kind table |
| `ProviderPicker.tsx` | 94 | which machine a pull lands on |
| `ollamaTarget.ts` | 129 | `hf.co/<repo>:<tag>`, and the ladder partition that goes with it |
| `UnchosenBanner.tsx` | 105 | a provider serving its whole catalogue, and the one click that ends it |
| `CapabilityChips.tsx` | 104 | MoE, MLA, GQA, sliding window, vision, MTP, context — as tags |
| `ParamBreakdownTable.tsx` | 82 | where the parameters are, and why two totals differ |
| `QuantTableCard.tsx` | 86 | the quantization table exactly as the gateway reports it |
| `DegreeFields.tsx` | 114 | TP, PP and EP, owned as a set; `null` means the planner picks |
| `SizingSliders.tsx` | 178 | Serve's context-per-user and users-at-once sliders, and the KV cache they cost |
| `speculative.ts` | 180 | the speculative field's pure decisions |
| `SpeculativeField.tsx` | 697 | which speculative method to serve with; `null` means one token per step |
| `CustomServes.tsx` | 59 | replaying a hand-written launch command |
| `rows.check.mjs` | 987 | the browser-side fold against the live coordinator's real payloads |
| `registry.check.mjs` | 215 | `GET /api/models`, the fold that moved to the server, and the boundary |
| `serve.check.mjs` | 123 | launch permissions from `gates.ts`, including branches a live cluster cannot reach |
| `query.check.mjs` | 55 | link parsing, typed filters, suggestion sections |
| `quantPicks.check.mjs` | 310 | tiers, picks and the default variant against a trimmed live ladder |
| `verdictView.check.mjs` | 35 | every `planWord` rule, every band, `formatParams` |
| `speculative.check.mjs` | 205 | the speculative field's decisions |
| `ollamaTarget.check.mjs` | 150 | the target string and the ladder partition |

## `rows.ts`

The row model, and the file every other one here reads a type from. `ModelRow`
keeps its facts apart instead of pre-joined into a string — the list it feeds
used to receive `{label, detail, group}` with `detail` already concatenated,
which is why it could never badge fit, sort by size, or say what was on disk.

`Facet` is where a model was found and `FACET_ORDER` is the order the six are
canonicalised into: `running`, `ondisk`, `catalog`, `provider`, `offered`,
`hub`. `Band` is the verdict — `running`, `fits`, `degraded`, `notnow`,
`unchecked`, `wont`, `elsewhere`, `unserved` — and `band()` reads reality before prediction:
on this cluster `openai/gpt-oss-120b` is serving right now and the capacity walk
at 8192/1 refuses it, because the live budget already has that deployment's own
memory taken out of it. Filing a model that is answering requests under "Needs
more memory" would be flatly false. `TERMINAL` (`failed`, `stopped`) is exported
so `board.ts` shares one definition of "over": a stopped deployment holds no
machine. `notnow` ("Fits after freeing a node") is a live `wont_fit` whose
static verdict fits, read through `CapacityIndex.staticRow`; it sits right after
`degraded`. `ModelRow` also carries `staticVerdict`, `needBytes`, `fitContext`
and `fitSeqs` off the same capacity row `withFit` picks.

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
exported for the same reason `TERMINAL` is — `ModelRows` must not disagree with
`HistoryTab` about what `degraded` looks like — and `BAND_TITLE` is the one place a
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

## `useModelList.ts`

The list's data, moved out of `ModelsTab.tsx` unchanged in behaviour: the
registry, providers, kinds, catalogues and storage hooks; the debounced capacity
numbers; the hub search; `decorate`; the auto-check batch; `problems` and
`fatal`. `useModelList(query, sort, view, showWont, hubFilters?)` returns the
decorated rows, the view's `groups`, `pinnedGroup`, `hubOnlyGroup`, per-view
`counts`, `worthTrying`, `wontCount`, `pins` (a set, from `GET /api/pins`),
`profiles` (every profile, `GET /api/profiles`) and the payloads the panes need.
`machinesPhrase` and `AUTO_CHECK` are exported beside it.

The query goes through `parseQuery` first. Only its `words` reach the hub search
and `matches` — the hub never gets the raw text, so `fits` is not searched for on
Hugging Face — and the typed filters are applied with `rowPasses`. `ListView` is
`fits | cluster | hub | providers | profiles`; `derate.models.view` stores one, and an old
`cards`/`rows` value falls back to `fits`. In the fits view a query pulls hub-only
rows out into `hubOnlyGroup` ("On Hugging Face"), so a search never mixes
"checked against your nodes, not downloaded" into the bands for what is here.
Pinned rows (every view but hub) leave their band for `pinnedGroup`, drawn first
under "Pinned"; each row's pin toggle (`aria-pressed`, a sibling of the row
button) `PUT`s the whole list. On the hub view, `HubCards`' Source (Original
repos / Unsloth quants = `author=unsloth`) and task chips become the search's
`author`/`task` (`hubFilters`), and search even with an empty box. The
`profiles` view is `ProfilesList`: Load opens the model and hands the request to
the pane as `initialRequest`, which the session applies once its ladder lands.
The toolbar's Provider button opens `components/ProviderAddDialog`.
`AUTO_CHECK` keys on the visible groups of the current view.

## `query.ts`

The search box, as data. `parseHfLink` accepts `huggingface.co/<owner>/<repo>`
and `hf.co/<owner>/<repo>:<QUANT>` and rejects the `models`, `datasets` and
`spaces` paths. `parseQuery` splits typed filters (`fits`, `pinned`, `on:`, `quant:`,
`task:`) from words; an unknown `key:value` stays a word and an empty `key:` is
dropped. `pinned` needs the pin set, passed as `rowPasses(row, f, { pins })`.
`suggest` returns sections and no closures — "From this link", "Finish
the filter", "Filters", "Models matching" — so the toolbar renders and applies
them and `query.check.mjs` asserts them without a DOM.

## `ModelToolbar.tsx`, `ModelRows.tsx`, `HubCards.tsx`, `WorthTrying.tsx`

The list pane. The toolbar is one bar: view select with counts, the search
combobox (`#mdl-suggest` listbox, ↑↓/Enter/Tab/Esc), Clear, the syntax help
toggle, Sort, Downloads with its badge, Provider. Filter chips sit under it and
each one rewrites the query without its token.

`ModelRows` is the row list. Rows are real `<button>`s rather than
`role="button"` divs with hand-rolled key handling — that was two bugs waiting,
since Enter and Space were reimplemented per call site and the focus ring
`base.css` gives every native control never reached them. Every fact on a row is
read, not derived: the lamp is the fit gate's verdict and a row with no verdict
draws a hollow lamp. A `checking` row keeps the shape "unchecked" draws and
changes only the word — it is a state, not a fourth verdict. `rowFacts` prints an
offer's price as "not priced" when either side is null, never as `$0`. Exactly
one band collapses (`isCollapsibleBand`: `unserved`), and a query forces it open
without touching what the person clicked — a needle that matched inside a
collapsed band has to show what it matched. The count is on the heading, because
a band that does not say its size reads as a short one. `ListCaption` and
`emptyNote` live here too.

`ModelRows`, `HubCards` and `WorthTrying` each call `useAvatars()` **once for the
list, never once per row or card**: avatars and their colours resolve
asynchronously, and a hundred cards each holding their own state would re-render
the whole grid a hundred times as a batch lands.

## `DownloadsPanel.tsx`

From `/api/activity`: launches with a `phase` (the shard-counting fraction is the
only progress bar, and only where one exists) and pulls not yet done. Hub
prefetch rows (`kind: "hub"`, started by "Download only" in `ServeActions`) show
`completed/total` and Pause/Resume and Cancel; Cancel confirms and never deletes
what is on disk. Provider rows stay read-only. `useDownloadsBadge` is the
toolbar's count. `PullCard` sits under it, as "Pull onto a provider".

## `SplitHandle.tsx`

A `role="separator"` between the panes, keyboard-operable, width clamped to
420..1200 and stored under `derate.models.side`. Below 1100px the split collapses
to one column and the handle is hidden.

## `useModelSession.ts`

The one owner of everything the plan request and the launch request share —
detail and ladder fetch, runtime pick, provider, route targets and destination,
the selected variant, kv dtype, forced quant, eager/cudagraph, custom command,
custom flags, served name, trust remote code, gates, this model's deployments,
`serve()` and `stop()`. Two copies of any of these would let the verdict describe
one shape while Serve started another; that is why nothing in the panes keeps its
own.

It fetches `modelDetail` and `modelVariants` itself rather than through
`state/resources.ts`, because `useResource` refires on every `revision` bump and
an open model would re-run its hub calls after every launch, admit and settings
change. The two loads are reported separately: detail is one resolve and usually
cached, the ladder is a search plus a repository read per GGUF repo. A `seq` ref
is bumped on every model change so a response for a model the pane no longer
shows cannot land. `routeTargets` assembles both provider surfaces and counts a
provider in both once, on the served side. The runtime follows the model once on
arrival, not at render: a speech checkpoint is *refused* on vllm rather than
merely slow, so the default must move, and picking vllm afterwards to read its
refusal has to keep working.

**The variant is the subject.** `subjectId = launchId(variant)` is sent as
`model_id` to BOTH `/api/plan` and the launch, so the verdict on screen is the
verdict for the file that would be started. `dtype` (forced quant) only applies
to the base repo's safetensors and is cleared otherwise; `kv_dtype` and `dtype`
go in BOTH requests, because a width the gate priced and the launch dropped
serves a fraction of the approved context in silence.

The plan effect debounces at 450 ms and short-circuits under a provider runtime:
`POST /api/plan` asks whether a model fits on a machine in *this* cluster, and a
verdict about the wrong memory pool would arrive looking exactly like one that
meant something. `context`, `concurrency`, `node_ids` and `parallelism` are all
**spread, never sent as null** — absence is the contract for "the coordinator
picks", and an explicit null is a different request. The launch then sends
`result.context` and `result.concurrency`, the numbers the verdict was actually
taken at, not what the boxes show. Custom flags become `extra_args`; `extra_args`
and `custom_command` are never both sent.

`granted` and the 409-derived `extraGates` are cleared on every replan so an
override cannot outlive the measurement that justified it, and the launch spreads
one key per gate the backend itself published. A 409 whose `refusalCode` names a
param becomes a gate; a 400 stays an error line. Under a pulling runtime `serve()`
takes the pull branch (`ollamaRef`, `pullToProvider`) and reads `pulled` rather
than a deployment — a pull never becomes a Deployment. On success it navigates to
`{dest: 'cluster', dep: dep.served_name}` — `served_name`, never
`deployment_id`, because `?dep=` is matched against the served name and an id
there does not fail, it falls through to `defaultDep()` and silently selects
somebody else's deployment.

The stale-node effect drops a ticked machine that has left the cluster, and its
`every` short-circuit is not an optimisation: without it a fresh array is written
on every 5s cluster poll, which refires the plan effect forever.

## `ModelPane.tsx`

One model, mounted from `ModelsTab.tsx` and from `shell/Sheet.tsx`, keyed on the
model id so its local state resets. The tab (Serve, Advanced, History, Hugging
Face) is component state rather than the URL — the URL names the screen and its
subject, and which view of one subject you are reading is neither. With route
targets, a segmented group above the tabs chooses This cluster or a provider; a
provider destination makes the Serve body `RouteCard` plus `BackendPicker`. A
model nothing local can resolve but a provider serves gets `ProviderFacts` and
the resolver's sentence under "no local verdict". The sticky footer carries Stop
(confirmed) and a one-line summary of what Serve would send.

## `VerdictBox.tsx`

The dry run and the refusal in one box. The word comes from `planWord`; the
fit gate's sentence, the decode range, the speculative range and any measured
decode figure (beside the range, never in place of it) render through
`Verbatim`. Fix links ("use N context", "stop <name>") never launch on their own.
Each gate renders the server's reason verbatim with a checkbox labelled by
`claimFor`, and the primary button appears only once `canServe`. "Where it runs"
(`NodeBars`) and the Why disclosure keep **both** budgets on screen — against
idle hardware and against what is free now — because the operator has to see
which one governs.

## `NodeBars.tsx`

Which machines this model would be served on, and the facts you need in order to
disagree with the planner about it. The planner does not choose machines on live
memory, cached weights or what is running, and every one of those is knowledge a
person has. Rows are in cluster order, not an order this file invented. Ticking
uses `board.ts::toggle`, which sorts before handback; unticking the last machine
is impossible, because "plan on nothing" is a 400 rather than "the planner
picks". An ineligible machine renders the registry's own sentence through
`Verbatim`. The split select lists `result.alternatives` as the planner sent them.

## `QuantSection.tsx`

One quantization already chosen, the rest a click away: a forty-row table asks a
question most people cannot answer. Recommended cards come from
`recommendedPicks`, then one tab per precision tier, filtered by format.
Selecting a variant re-plans through `useModelSession`. Rows outside
`partitionForRuntime(...).servable` are drawn faded and stay selectable, so the
refusal can be read in the verdict box.

## `LivePanel.tsx`

A running deployment: the metrics stream's `load` block (kv cache, running,
waiting, decode) and the deployment's log tail, re-polled only while the source
is `buffer`. The `FailureBanner` for the newest failed deployment lives here.

## `AdvancedTab.tsx`

Profile bar, Engine, Speed, Memory, Context, Multimodal, Parallelism, API,
Lifecycle, Custom flags, and the command preview, as disclosures (the sections
live in `AdvancedSections.tsx`). Engine rows come from `useRuntimes`
(`/api/runtimes`) and are disabled only when the variant's format is not in the
runtime's `formats`; otherwise picking one shows that runtime's refusal in the
verdict box. Typed knobs travel as `engine_options`: parsers, prefix caching,
tokens per scheduler step, async scheduling, multimodal limits, text only,
YaRN (offered when the context passes `original_window`), sampling defaults and
NCCL env rows. The GPU share is read-only: the figure `/api/plan/command`
derives. The preview is fetched 600 ms after the plan settles and only while
this tab is mounted (`wantPreview`). Lifecycle edits on a running deployment
PATCH it at once; otherwise they ride the launch request. "Create a key for this
model" mints a key restricted to the served name and shows it in `RevealModal`.
Parallelism offers Measure for an unmeasured pair. It mints its `DegreeFields`
`idPrefix` with `useId`: the pane is mounted in the Models tab *and* the sheet
while `AppShell` keeps every destination mounted, so a hardcoded id would focus
the other pane's field.

## `ProfilesTab.tsx`, `ProfilesList.tsx`

Saved profiles (`/api/profiles`, shared across browsers and admins) beside this
model's previous runs (each deployment's `relaunch_request`). Load and Serve
only `applyRequest` and switch tab -- neither launches. `ProfilesList` is the
list pane's Profiles view.

## `BenchmarksTab.tsx`

Runs `/api/benchmarks` with `currentRequest()` after a confirm: a temporary copy
launches, is measured at each user count and is stopped. Ticking two rows shows
compare bars; failed records show `error` through Verbatim.

## `UsersChart.tsx`, `usersChart.ts`

"How many users" on the Serve tab, from `/api/plan/concurrency`: By quant (one
request per recommended pick) or By context. A `multi-line` pack chart with a
dashed wall marker per series and the newest matching benchmark as points. The
line is a bandwidth CEILING per user; at one user it equals the card's tok/s.
The server's `caveat` renders through Verbatim. Hidden for a provider runtime or
when no series has room for a user.

## `SizingSliders.tsx`

Context per user and Users at once, as two sliders above the chart; they
replaced its context chips. A drag is held locally and written to
`?ctx=`/`?seq=` once, on pointer or key release (or blur): Chrome stops
applying `replaceState` past ~200 writes in 10 s, and then the address bar
and the screen disagree. Stops are `contextStops(native_window)` and
`USER_STOPS` (`usersChart.ts`), with the COMMITTED value inserted by
`withValue` so a planner pick between stops is a stop of its own and the track
never changes length mid-drag. Each slider has Auto, which clears its key. The
line under them is the gate's own `breakdown.kv_cache`, never extrapolated,
and "Re-planning…" while the URL differs from `plannedContext`/
`plannedConcurrency` (what the session plans with) or a plan is in flight.
Headroom is stated only from `fit_live` (the static ceiling is not room to
spare). A slider is disabled, its figure "—", until a plan has said its
value (and, for context, the model's window). The sliders outlive a failed plan (the window is remembered) and
are hidden while the model runs, when the card describes the deployment.

## `launchRequest.ts`, `profiles.ts`, `folders.ts`

Pure, React-free, each with a `.check.mjs`. `buildLaunchRequest` is the one
launch-body builder (spread, never null; custom command excludes derived
flags) and `knobsFromRequest` is its inverse, round-tripped by the check.
`profiles.ts` has `changedKeys` ("Changed: …"), `profileSummary` (chips) and
`preloadChoice` (starred default, else the last run's `relaunch_request`, else
planner defaults). `folderFor(repo)` is the hub cache folder name the Serve
tab's "Delete from disk" passes to `deleteCachedModel`.

## `HistoryTab.tsx`

This model's deployments, their events from `/api/history` (a STOPPED event's
`reason`, e.g. `idle for 15 minutes`, is shown), and "Roll back to this", which
applies the deployment's `relaunch_request` and moves to the Serve tab -- it
never launches.

## `HubTab.tsx`

Stats from `detail.hub` (downloads, likes, updated, license, task), falling back
to the search row when the hub answered nothing; the repos on the hub
(`repoGroups`, with Use), the About body and `QuantTableCard`.

## `gates.ts`

Launch permissions in one place. `gatesOf(serve)` reads the published gates;
`canServeWith` lets the backend's `serve` decision govern, and a gateway that
predates `serve` falls back to the fit result rather than treating absence as a
refusal. `claimFor` writes the checkbox label in the operator's first person —
"Serve anyway. I am overriding the live fit gate, which measured N GB allocatable
and refused" — because it is a claim they are making, not a message they are
dismissing. Its final fallback ("Launch anyway. I have read the reason above and
am overriding it.") matters as much as the named branches: a gateway that adds a
gate tomorrow still gets a blocking control. `tabs/cluster/blocks/ModelBlock.tsx`
imports `claimFor` from here.

`refusalCode` maps a 409 to the field that reopens it: the cluster launch refuses
with `live_memory_insufficient` / `allow_over_live_memory`, the pull with
`pull_over_memory` / `allow_over_memory`. An unrecognised code returns null on
purpose — a checkbox that sends a field the gateway ignores is a button that does
nothing twice.

## `verdictView.ts`

`bandWord` (a list band) and `planWord` (the detail's plan) as label plus signal,
first matching rule wins; `formatParams` and `gib`. Pure so
`verdictView.check.mjs` can hold one fixture per rule.

## `quantPicks.ts`

Selection over server-judged ladder rows, with no fit arithmetic. `tierOf` — a
llama.cpp `Q<n>` name wins over measured bits, because the name is what the
publisher calls it — `formatOf`, `isUnslothDynamic`, `recommendedPicks`,
`defaultVariant` and `repoGroups`. Everything reads `partitionForRuntime`'s
servable list and keeps the gateway's rank order.

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

The machine board's arithmetic with no DOM and no React in it: it is the part
that can be wrong in a way
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
figure, and is the one definition the Links rail and the wiring editor use too. `toggle` sorts before handback so two
tick orders produce one request body, one answer and one server-side memo key.

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
"unless told otherwise": a live list from the provider's own endpoints drawn as
the shared `HostTable` (`components/HostTable.tsx`: Automatic row, each host with
its $/Mtok in and out, context, quantization, a `cheapest` pill), and a pin that
rides along on every request thereafter — `ProviderService._prepare` injects
`provider: {only: [tag]}`, or `{order: [tag], allow_fallbacks: true}` when the
"Fall back to other hosts" box is ticked. The pinned host's price becomes the
model's price, and the line `host: <provider_name> · $in / $out` sits directly
under the `RouteCard` while a pin is in force.

Self-hiding rather than error-prone. A kind with no `supports_backend_routing`
answers 400 for every model it serves, which is the ordinary answer on a pane
mostly full of OpenAI, Together and Ollama providers, so
`backend_routing_unsupported` sets `unsupported` and the component renders
nothing. Any other kind shows the table even at zero or one host — zero says
`OpenRouter lists no hosts for this model yet.` Both writes are single-key
patches (`backend_pins: {[upstream]: tag | ''}`, `backend_fallbacks:
{[upstream]: bool}`), which merge, so this never has to know or resend the
provider's other pins; the rows are refetched after each. The fallback flag's
current value comes in as a prop, from the catalogue row's `backend_fallback`.
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

What `NodeBars` is for a cluster launch, this is for a pull: the same shape of
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

`partitionForRuntime` splits on `runnableOnOllama` under ollama and, on the
cluster, on `launchable` AND the runtime's file format (llamacpp: GGUF only;
vllm, sglang, tts: no GGUF), preserving the gateway's rank order in every
branch. The format half is needed because the gateway's `launchable` became
runtime-blind when llamacpp arrived, and a GGUF default opened every model on a
vllm refusal. It is not a second answer to the gateway's question: `launchable`
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
thresholds are the planner's arithmetic. `idPrefix` exists because
`ModelPane` is mounted from the Models tab *and* the sheet while `AppShell`
keeps every destination mounted, so with one hardcoded id clicking "TP" in one
would focus the other's field.

## `CustomServes.tsx`

`CustomServesCard` replays a hand-written launch command from a previous launch
— a quantization choice, a memory knob, anything the standard recipe does not
cover — so it is not retyped. Backed by `state/customServes.ts` and empty until
the first launch that used one. Clicking an entry only *seeds* the custom
command field (Advanced › Custom flags) on the model it names; nothing here launches anything.

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

Hermetic. It loads `gates.ts` and covers the derivation of launch permissions
from `serve` and whether the Serve button may appear, both pure functions of wire
data, and both with branches a live cluster cannot reach: a gateway that predates
`serve` (a `fits` fit serves, a `wont_fit` one does not), two gates at once (the
button stays away until *both* are ticked), a gate that is needed while the fit
itself passes — the reason `overrides` exists — and a hard refusal that offers no
way through. This was `tabs/dashboard/planner.check.mjs`; it used to restate the
derivation inline because it lived inside React components, until `gates.ts`
pulled it out.

## `query.check.mjs`, `quantPicks.check.mjs`, `verdictView.check.mjs`

Hermetic, bundled through `harness.mjs` `load()`. `query.check.mjs` pins link
parsing, typed filters and suggestion order (local rows before hub-only ones).
`quantPicks.check.mjs` runs on a fixture trimmed by hand from a live
`/api/models/variants` reply. `verdictView.check.mjs` has one fixture per
`planWord` rule and every band through `bandWord`.

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

`tabs/ModelsTab.tsx` is the screen: `ModelToolbar`, `DownloadsPanel` when
toggled, and the split — the list pane (`UnchosenBanner`, `CustomServesCard`,
then `HubCards` or `ModelRows` plus `WorthTrying`) beside `ModelPane`. Its data
comes from `useModelList`, which polls once through `state/resources.ts`
(`useModelRegistry`, `useCapacity`, `useStorage`, `useProviders`,
`useProviderKinds`, `useProviderCatalogues`) and hands the payloads down, so
every consumer renders the same answer. The fixed hooks are shared by name and
would not double their requests; `useCapacity` and `useProviderCatalogues` are
keyed and would.

```ts
const rows = decorate(
  withHubHits(registryRows(registry.data), hits),
  capacityIndex(capacity.data, ...batches),
  cacheIndex(storage.data),
  checking,
)
// A hub row is kept whatever the box now says: the gateway already narrowed
// those hits to the needle IT was given. Typed filters are their own pass.
```

`ModelPane` is mounted from two places — the Models tab's detail pane and
`shell/Sheet.tsx` — which is why ids inside it are scoped. `AUTO_CHECK` is 10:
each verdict-less row is a hub round trip on a cold cache, so about a screenful
gets a real answer and everything below keeps saying "not checked", which is true.


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

**Serve is absent, not disabled, where it cannot work.** In the verdict box, on the
board's unselectable rows, on the degree fields under a runtime that does not
shard. A disabled control reads as "not right now"; the truth in each of these
cases is "not by this route at all", and offering the control anyway means
offering a refusal the client already knew about.

**Absence is the wire contract for "the coordinator picks".** `context`,
`concurrency`, `node_ids` and `parallelism` are spread into the request body, and
an explicit `null` is a *different* request. Every launch then sends back what the
verdict was actually taken at — `result.context`, `variant.max_seqs`,
`sized_on.nodes` — never what a field happens to display.

**Overrides never outlive the measurement.** `granted` and the 409-derived
gates are wiped on every replan in `useModelSession`. A tick granted against one
live-memory reading must not ride along with the next plan.

**409 is not 400.** A live-memory refusal is legal and an unchanged retry
succeeds once memory frees, so it gets the override path. A static refusal is a
400 and correctly gets the error line.

**The fit gate's sentence is rendered verbatim or withheld.** `Verbatim` is used
for every refusal, every ineligibility reason and every resolver error here.
Under a provider runtime the verdict box withholds it entirely rather than
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
- **A model id nothing local can resolve.** `ModelPane` renders the
  resolver's own sentence under "no local verdict" and shows no plan
  when a provider serves it — there is nothing here to plan.
- **A 409 with a code this screen has no override for.** Rendered as an error,
  not as a checkbox that sends a field the gateway ignores.
- **A launch that dies before a deployment record exists.** The pane
  shows no Running state for the second or two of real gap; under a pulling runtime it
  reads the accepted pull reply instead, because a pull never becomes a
  Deployment and waiting for one would hang forever.

## Deliberately not built

**A ranking of machines.** `board.ts` and `NodeBars.tsx` both say so. Nothing
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