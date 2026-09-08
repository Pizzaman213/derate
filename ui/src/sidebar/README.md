# sidebar

The right-hand rail: five sections that answer, from the top down, what
machines exist, what is arriving, what the selected model was planned onto,
where its traffic goes, and what a million tokens of it costs. Each section
fetches its own data from `state/resources`, so `Sidebar.tsx` passes no props
and nothing here is prop-drilled through a parent that does not care.

The rail's recurring subject is honesty about provenance. Four of the five
sections show a figure they did not compute, and the fifth computes one and
prints its inputs beside it. Three of them say "Nothing is being served." when
no deployment is selected, and none of them fills that space with a plausible
number instead.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `Sidebar.tsx` | 26 | the composition, and the folder's only export anything outside imports |
| `RosterSection.tsx` | 113 | one row per machine: lamp, watts, memory bar, deployments, utilisation |
| `ActivitySection.tsx` | 91 | the drawing of the arriving rows, and the screen-reader wording for a bar with no number |
| `activity.ts` | 137 | those rows as data: `activityRows`, `downloadValue`, `launchValue` |
| `activity.check.mjs` | 236 | the hermetic verifier that pins every rule in `activity.ts` |
| `PlanSection.tsx` | 63 | `plan.reason` and `plan.rejected`, verbatim, behind a disclosure |
| `RoutingSection.tsx` | 242 | the policy select, the seven help strings, the per-target weight bars, and the spill lamp |
| `CostSection.tsx` | 137 | cost per Mtok, recomputed client-side against the live metrics frame |

## `Sidebar.tsx`

Five children in render order — `RosterSection`, `ActivitySection`,
`PlanSection`, `RoutingSection`, `CostSection` — inside a fragment, and nothing
else. `shell/AppShell.tsx` is the only importer, at line 14, and drops it into
the `<aside id="side">`.

The order is an argument, not a layout preference. `ActivitySection` sits
directly under the nodes and above the three sections that describe a settled
cluster, because those three cannot answer "a model is on its way": during a
download all three said "Nothing is being served." at once. Placing the
transient state above them puts the answer before the three denials rather than
after them.

## `RosterSection.tsx`

One row per node from `useCluster()`, joined against `useTopology()` for which
deployments touch it and `useMetrics()` for what it is drawing right now.
`nodeLive(n, frame, stale)` and `nodeSignal(n, live)` decide the lamp, and
`hollow={grey && !down}` decides whether it is filled: a node whose own sample
has aged past `SAMPLE_STALE_S` (30 s) goes hollow and stops claiming its state
is fresh without pretending the state changed, while an unreachable one stays a
solid `fault` — hollowing that lamp would express doubt about the one thing
here we are certain of. A double-click calls `openSheet({ kind: 'node', id })`,
which is the `?open=node:` query in the URL scheme.

**The slots model did not survive the port.** The mockup gave each node a fixed
array of GPU slots, each free or holding a model, and drew an "N free" count off
it. The wire has no such thing — a node has zero or more deployment ids under
topology's per-node `deployments` — so this renders the memory line the live
frame actually gives and no slot count at all. `live.memory_used_pct == null`
draws a `ProportionBar` with `value={null}`, whose aria label is "no memory
reading for `<id>`".

An ineligible node is dimmed to 0.55 with a dashed border and prints
`n.ineligible_reason` under its name in `pre-wrap`, because that sentence is
the only part of the row anybody can act on. `gateway/serialize.py::_eligibility`
writes three of them: `node is unhealthy`, the unrecognised-device-class hedge,
and `INELIGIBLE_BUILD_SKEW`, which ends "re-run the installer on that machine" —
an instruction that survives only if the string reaches the screen whole. A `gb10`
appends ` · shared` to its utilisation line: unified memory means the OS is in
the same pool, and the number must not read as exclusive.

## `ActivitySection.tsx`

The drawing half. `useActivity()` polls `GET /api/activity` every 2000 ms —
faster than the 5000 ms every other resource in this rail uses, because it is
the only one describing something that moves. `activityRows(activity.data)`
does all the deciding; this file maps rows to markup.

**It returns `null` when there are no rows.** A permanent fifth "Nothing is
happening." line beneath the three sections that already say it makes the rail
longer and no more informative — the section appearing *is* the signal.

Two details are load-bearing. `row.status` goes through `Verbatim`, so
"Pulling image: ghcr.io/…" and "Loading safetensors checkpoint shards: 5/11"
reach the screen as the program printed them. And a bar with `value == null`
gets the spoken label "`<title>` is working, with no progress figure to
report": `ProportionBar` renders a missing value as a dashed empty track, which
is a picture a screen reader is given none of.

## `activity.ts`

The rows as data, split from the drawing for the same reason `tabs/models/
rows.ts` is split from its list: these rules can be wrong in ways `tsc` cannot
see. `ActivityRow` carries `key`, `kind`, `title`, `target`, `status`, `value`,
`detail`, `signal`, `since` and `error`; `activityRows` is the entry point and
`downloadValue` / `launchValue` are exported so the verifier can drive them
directly.

**Every rule in this file is a way of declining to print a number.**
`downloadValue` returns `null` when `total` is null or non-positive — a pull
spends its first seconds in "pulling manifest" with nothing sized, and a solid
0% bar there asserts that an unmeasured download is 0% done. The `detail` line
for the same row reads `sizing`, not `0.0 / 0.0 GiB`, because a denominator
nothing reported is an invented measurement. `launchValue` returns `null` for
anything non-finite, which is what keeps a `NaN` out of a bar's CSS width where
it renders as a blank track with no explanation.

`launchValue` used to be the constant `null`, on the reasoning that the weights
land inside a container the control plane cannot see into. `sparkrun logs`
reaches that container and the manager reads it mid-launch, and two of a
launch's four steps turn out to count themselves, both through a tqdm bar the
program printed about its own work: the weight download's
`Fetching 16 files: 19%` and the checkpoint loader's `5/11`. Those counts are
measurements somebody else made. The image pull, the compile and the graph
capture report no total at all, so they carry no bar.

`signal` is `fault` on `l.last_error || l.fatal`. `fatal` is there because the
runtime announcing its own death reaches this row before the manager has
finished writing the failure onto the record, and an amber lamp over
"EngineCore failed to start." is a row contradicting the sentence beside it.

Ordering is downloads in the order the server sent them, then launches sorted
by `since` ascending. **Nothing sorts by progress**, because the bar advancing
is the one thing guaranteed to happen and a row that reshuffles as its own bar
fills cannot be read.

## `activity.check.mjs`

236 lines, hermetic — it has no `// requires:` line, and `ui/check.mjs` reads
an absent line as needing nothing but the checkout. It esbuild-bundles
`activity.ts` through `build()` from the JS API rather than spawning `npx`,
which on Windows resolves to `npx.cmd` and cannot be launched by path without a
shell, then imports the bundle and exercises it. Comparison is `Object.is`, so
`0` and `null` cannot pass for one another.

The two assertions the file exists for are "null is not zero" and "a launch has
no percentage unless the runtime counted one". Both compile perfectly while
putting an invented figure on screen. Around them it pins the clamps
(`completed` a few bytes past `total` clamps to 1, a negative to 0), the
verbatim pass-through of the runtime's sentence, `phaseLabel` degrading to ''
on an unheard-of phase so the detail line reads `vllm` alone rather than a raw
identifier, `remainingLabel` printing "under a minute left" at 14 s rather than
a countdown, the download lamps (`warn` running, `live` done, `fault` errored),
stable ordering under moving numbers, and that `download:<pull_id>` and
`launch:<deployment_id>` cannot collide — two ids minted by different things,
and React drops a duplicate-keyed row silently.

**Both ends decline by default, by construction.**
`control_plane/deploy/progress.py::_fraction` returns `None` unless a line
matched `_SHARDS_RE` or `_FETCHING_RE` — a count a program printed about its
own work — and there is no fraction and no ETA at all for the image pull, the
compile or the graph capture, because none of them reports a total.
`launchValue` then refuses anything that is not a finite number. Neither side
derives a rate and extrapolates from it, and this file is where the client half
of that is nailed down.

## `PlanSection.tsx`

The selected deployment's shape and the planner's own words for it.
`selDep` from `useSelection()` names the served name; the section filters
`cluster.data.deployments` by it and takes `runners(named)[0]`.

**The ledger keeps every attempt, so one served name can match several rows.**
`runners()` (from `tabs/cluster/layout.ts`) drops the terminal states, so the
row shown is a live one — a failed attempt's plan is not what the cluster is
running, and rendering it here attributes machines to nobody.

The header line is `planShortFromDegrees(plan)` ("TP 2 + PP 2", or "single
node") against the measured link. `spanning` is `plan.node_ids.length > 1`, and
a single-node plan prints an em dash rather than `measured_link_gbps`: there is
no inter-node link to report and the field would otherwise show an unrelated
number. Behind the `why` disclosure, `plan.reason` renders through `Verbatim`
and `plan.rejected` through `VerbatimList`. The mockup's hand-written `#whyBox`
markup, keyed on a fixture's shape, did not survive the port — the real
deployment carries both sentences itself.

## `RoutingSection.tsx`

The policy select, its help text, and one weight bar per target for the
selected deployment, from `useRouting()`. Changing the select calls
`backend.setPolicy(cfg.served_name, p)` — `PUT /api/routing/{served_name}` —
holds the new value in `pending` while it is in flight, and invalidates on
success.

`POLICIES` is seven entries, matching `gateway/policies.py`'s `SELECTORS`
exactly: `least_outstanding`, `round_robin`, `weighted_capacity`,
`cache_affinity`, `failover`, `local_first`, `cost_aware`. The mockup carried
help text for five; the `cache_affinity` and `failover` strings are transcribed
from those selectors' own docstrings rather than written here.

**A failed policy change shows the server's rejection verbatim.** Without the
`catch` that sets `error`, a 409 or a 500 looked exactly like nothing
happening — in the one section whose whole thesis is that the server's own
sentences are the product.

**A bar means two different things and says which.** `PROPORTIONAL` (from
`state/policy.ts`, and shared with `tabs/cluster/ClusterGraph.tsx` so the two
screens cannot disagree) holds `weighted_capacity` and `round_robin` — the only
policies under which `weight` is a configured share. Under those the fill is
opaque over a `--panel-sunk` track and the caption reads "Bars are the
configured share."; under the other five the track has no background at all and
the fill drops to 0.55 opacity, over a caption saying the bars show where
traffic is going right now, not a configured share. Both captions and the help
string are suppressed below two targets, where the line reads "Single target.
Policy has no effect." instead. `Math.round(t.weight * 100)` is the only number
drawn either way. The mockup's `curW()`, which recomputed a per-policy weight
client-side, is gone: both grammars vary in presentation, never in the value
shown.

**The select is the smallest part of the section.** `cfg.auto_reason` renders through `Verbatim` whenever `cfg.auto_selected`, so a
policy nobody picked says who picked it and why; `t.admission_blocks` renders
through `VerbatimList`, because a target reporting `admitting: false` with
nothing under it is the bug rather than the ordinary case. Under `local_first`
the section also draws `cfg.flow` as a lamp — `live` for "Serving locally",
`warn` for "Spilled to remote: every local target is saturated" — and suffixes
a remote row with ` · backup` instead of ` · remote`: a zero-weight row labelled
only "remote" reads as a target nothing is using rather than one held in
reserve.

`targetLabel` splits a remote `target_id` on the **first** colon only, into
`<provider> · <upstream model>` — `gateway/targets.py` builds that id and
`ProviderService.split_target_id` takes it apart, and an upstream id may
contain further colons. Printed raw it read as one opaque string, so the row
said a remote was serving this name without saying which provider, which is the
question somebody selected the model to answer.

`zeroReason` is shown only for a local target at weight 0. The floor itself is
`gateway/strength.py::compute_weights`, which zeroes a local below
`weak_target_floor` of the strongest local; `targets.py::apply_scores` re-tests
that same condition beside it for one purpose, to attach `zero_weight_reason`,
and only ever to a local — it writes `None` for every remote, under the comment
"remotes are never floored". A remote at zero is simply not currently favoured
and has no wire reason to print.

An unhealthy, non-admitting or circuit-flagged target is dimmed to 0.62 opacity
and, for the circuit, given an `idle` hollow `Lamp` with the state in its label.
It is deliberately not a coloured pill: `--fault` and `--warn` already mean node
health and pressure elsewhere in this rail, and painting a circuit chip in
either hands them a second meaning.

## `CostSection.tsx`

One row per target, priced per million tokens. A remote prints
`t.cost_per_mtok` and captions it "published by the provider". A local is
computed here:

```ts
const usd = ((watts / 1000) * rate / (tps * 3600)) * 1_000_000
```

which is `gateway/targets.py::local_cost_per_mtok` term for term.

**The arithmetic is repeated client-side so the provenance line can be true.**
`RouteTarget.cost_per_mtok` is computed the same way on the server but from a
settings-poll snapshot; this recomputes it against `useMetrics()`'s live frame,
summing `power_w` over `t.node_ids` and taking `tokens_per_sec` from the frame,
falling back to topology's figure. The caption then reads `from 412 W at 38
tok/s · $0.15/kWh` about the exact numbers that produced the dollar figure
above it, rather than about a snapshot taken somewhere else.

Three refusals. No electricity rate set says "set an electricity rate to price
local generation" — the only one a reader can act on, and so the only one that
gets a sentence. No wattage for any of the target's nodes, or no positive
`tokens_per_sec`, prints an em dash and nothing else, and so does a remote whose
`cost_per_mtok` is null. None of the three substitutes `$0.000`, which is a
price rather than a refusal.

## The seam with `AppShell`

`shell/AppShell.tsx` imports `Sidebar` and renders it once, inside
`<aside id="side">`, under the same `SelectionProvider` the tabs are under —
`AppShell.tsx` opens it at line 114, around both the rail and `<main>`. That is
the entire interface: no props, no context defined here, and nothing outside
this folder importing any file in it but `Sidebar.tsx`.

```tsx
import { Sidebar } from '../sidebar/Sidebar'
// ...
<aside id="side"><div className="inner"><Sidebar /></div></aside>
```

Everything the rail knows it fetches itself:

- `state/resources` — `useCluster`, `useTopology`, `useRouting`, `useSettings`
  (5000 ms each) and `useActivity` (2000 ms).
- `state/metrics` — `useMetrics()`, for `frame` and `stale`.
- `state/selection` — `selDep`, and `openSheet` for the node sheet.
- `state/backend` — `useBackend()`, for `backend.setPolicy` and `invalidate()`.
  It is the rail's only write; every other call here is a read.
- `components/` — `Lamp`, `Readout`, `ProportionBar`, `Verbatim`,
  `VerbatimList`, `Disclosure`.
- `state/live`, `state/names`, `state/policy`, `state/launchPhase`, `format`.

`activity.ts` is imported by `ActivitySection.tsx` and by its verifier and by
nothing else; `tabs/cluster/loading.ts` cites the split as the pattern it
follows.

## Things that look like details and are not

**`null` and `0` are not interchangeable anywhere in this folder.**
`ProportionBar` draws `value == null` as a transparent track with a dashed
border and no fill, and `value === 0` as a recessed track with a solid border,
specifically so a missing measurement is never pixel-identical to a measured
zero. Every `null` returned by `downloadValue` and `launchValue` is spending
that distinction.

**Three sections say "Nothing is being served." and the fourth renders
nothing.** `PlanSection`, `RoutingSection` and `CostSection` each guard on their
own lookup failing, so the rail degrades section by section rather than as a
unit — a deployment with routing but no live plan row still shows its routing.
`ActivitySection` returns `null` instead, because a fourth denial is not
information.

**The launch detail line leads with the step, not the runtime.**
`[phaseLabel(l.phase), remainingLabel(l.eta_s), l.runtime].filter(Boolean).join(' · ')`
puts the part that changes first. `phaseLabel` returns `''` for a phase this
build has not heard of, so an unknown phase contributes nothing and the runtime
name still prints — the server owns that vocabulary and can name a phase a
deployed UI predates.

**`remainingLabel` is deliberately vague above a minute.** The estimates it
renders come from tqdm, which extrapolates from throughput so far, so "3m 47s"
would be spurious precision on a number that moves every second. An estimate is
planned around, which is why it may only come from the thing being estimated.

**Weight bars and cost rows read the same `RouteTarget` list and label it
differently.** The two `targetLabel`s are identical on a local target —
`t.node_ids.join(' + ')` — and diverge only on a remote, where `RoutingSection`
splits the id into provider and model and `CostSection` returns `t.target_id`
whole. The
routing row exists to say which provider is taking traffic; the cost row is a
price beside a name and the split would only add width.

## Failure behaviour

- **No deployment selected, or none matching `selDep`.** `PlanSection`,
  `RoutingSection` and `CostSection` each render their heading and "Nothing is
  being served."
- **The named deployment exists only as failed attempts.** `runners()` filters
  them out and `PlanSection` falls to the same message, rather than showing a
  dead attempt's plan as the current one.
- **No nodes in the roster.** `RosterSection` renders "No nodes yet."
- **The metrics frame is missing or stale.** `nodeLive` reports `fresh: false`;
  a reachable node's lamp goes hollow and an unreachable one stays a solid
  `fault`, `Readout` renders `--ink-muted`, the memory bar tones to `muted`, and
  a node absent from an otherwise healthy frame is caught on its own sample age
  against `SAMPLE_STALE_S`.
- **A node has no memory reading.** `value={null}` — a dashed track, labelled
  "no memory reading for `<id>`", never a full or empty bar.
- **`activity` is `null`, `undefined`, or missing its arrays.**
  `activityRows` returns `[]` and the section renders nothing. Pinned three ways
  in the verifier.
- **A download reports no total.** `value: null`, `detail: 'sizing'`.
- **A launch reports no fraction.** `value: null` for as much of the launch as
  the runtime is not counting shards.
- **`setPolicy` rejects.** The server's message renders in `--fault` with
  `pre-wrap`, and `pending` clears in the `finally`, so the select snaps back to
  the config's own policy.
- **No electricity rate, no wattage, or no throughput.** An em dash and, for the
  rate, the sentence that says how to fix it.

## Deliberately not built

**A slot model for the roster.** A fixed array of GPU slots per node, each free
or occupied, with an "N free" count and a per-slot bar. It has no counterpart on
the wire, and inventing one would put a capacity figure on screen that nothing
measured.

**Client-side weight arithmetic.** The mockup's `curW()` derived a per-policy
weight in the browser. `RouteTarget.weight` is the one real number the wire
gives, and two implementations of "what share is this target getting" is two
answers on one screen.

**A permanent Activity section.** It renders only when there is something to
report. A fifth line reading "Nothing is happening." under three sections that
already say it makes the rail worse.

**Trusting `cost_per_mtok` for a local target.** The wire's number is correct
and computed from a settings-poll snapshot; the provenance line under it would
then describe inputs other than the ones that produced it, which is the one
thing this section exists to avoid.
