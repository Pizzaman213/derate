# `tabs/cluster/` — the wiring editor

`/cluster` is a typed node-graph editor over the cluster's routing. It replaced
the machine floor (a hand-laid SVG of plates and bands) on 2026-11-18, because
the floor could show where requests went but could not change it; the editor
draws the same facts and lets the operator rewire the parts the server will
actually accept.

Requests read left to right in six columns:

```
API Input -> rate limits -> split ◁ -> machines and providers -> merge ▷ -> API Output
```

One split/merge pair per served name. Draft model blocks (palette items) sit
under the balancers. Machines, providers and balancers come from the server;
only draft models and rate limits are created in the editor. Double-clicking a
split or merge opens its balancer dialog; double-clicking a machine opens its
node page.

LB_POLICY = failover: `gateway/policies.py::select_failover` returns the first healthy, admitting target in `_stable_order` (locals before remotes, then by id), so a remote is chosen only when no local admits; `select_local_first` additionally spills to remote when every local is merely saturated, which is not the triangle's contract.

The triangle means "local while any local deployment is admitting, else
cloud". A name whose policy is something else (set from the sidebar or the
balancer dialog) shows `policy: <value> (sidebar)` and loses its `1 ·` / `2 ·`
port ranks; the editor never changes that policy silently. To switch to
spill-on-saturation, change the one constant `LB_POLICY` in `sockets.ts`.

## Files

| file | what |
|---|---|
| `sockets.ts` | the socket types, every block's sockets, `canConnect`, every sentence the editor says about a socket, `LB_POLICY`. Pure |
| `doc.ts` | the document: server snapshot in, blocks/wires/ops/launches/issues out. Pure, no React, no `@xyflow` |
| `WiringCanvas.tsx` | the `<ReactFlow>`: node and edge types, drag state, wire reconnects, double-click, Delete on a balancer, measured sizes, refusals to the `aria-live` region |
| `edges.tsx` | `WireEdge` and `WireConnectionLine` (square-cornered orthogonal steps via `wirePath`, the same for a placed wire and the drag preview; stroke by socket type, red on fault, label, the × on a selected editable wire) and `PathRegistry` for particles |
| `ParticleLayer.tsx` | particles in the viewport, fed from routing and the 1 Hz frame |
| `particles.ts` | what gets emitted, where and how fast; `useParticleField` |
| `positions.ts` | dragged block positions in localStorage |
| `GraphToolbar.tsx` | Add model, Add rate limit, zoom, Arrange, Discard, Save N changes |
| `BalancerDialog.tsx` | one served name's dialog: policy, its rate limit, its live targets, Remove this load balancer |
| `SelectionRail.tsx` | the Links card: measured pairs, Measure, reachability |
| `loading.ts` | a launch's phase (preparing, downloading, loading, serving) and its clock |
| `providerLogo.ts` | a provider's logo URL, or none (the monogram tile is always painted under it) |
| `blocks/editor.tsx` | `EditorContext` (the edit callbacks blocks call) and `Socket`, the one handle component |
| `blocks/Terminals.tsx` | `InputBlock`, `OutputBlock`, `LimitBlock` |
| `blocks/Balancer.tsx` | `SplitBlock`, `MergeBlock`: SVG triangle chrome, handles at the socket points |
| `blocks/Containers.tsx` | `MachineBlock` (deployment rows with their launch-flag chips, memory bar, `deploy here`, link handles), `ProviderBlock` (an OpenRouter row's Choose host popover, a `HostTable`) |
| `blocks/ModelBlock.tsx` | a draft model: model, runtime, context, concurrency, verdict, MTP |
| `../ClusterTab.tsx` | owns `doc`, `base`, plans, dry run and Save; the four cards under the canvas |

## Sockets

`sockets.ts` is the single source of truth for what can connect; everything
else imports it.

| type | token | meaning |
|---|---|---|
| `requests` | `graph-req` (alias of `flow`) | traffic going toward a model |
| `responses` | `graph-resp` (alias of `stream`) | answers coming back |
| `limit` | `graph-limit` (magenta, `tag-2`'s steps) | a rate limit applied to a served name |
| `deploy` | `graph-deploy` (alias of `ink`) | where a draft model will be launched |
| `link` | `graph-link` (alias of `edge`) | measured machine-to-machine link (display only) |

Every input sits on a block's left edge and every output on its right, so a
wire always leaves to the right and enters on the left. The two exceptions are
the machine link (top/bottom) and the split's `limit`, drawn on the triangle's
lower slanted edge; `sockets.check.mjs` holds the rule as data. Port labels sit
inside the triangles, so nothing hangs into the next column.

Red (`graph-fault`) is a fault and amber (`graph-warn`) a warning; colour means
nothing else. Editable sockets are filled circles; fixed ones are hollow with a
coloured ring and do not start a drag — pressing one announces its fixed
sentence. Editable wires draw 2.4 px at full opacity, fixed ones 1.6 px at 0.45.

`canConnect(from, to)` returns `null` or the refusal sentence. Its rules and
their order live in `sockets.ts` and nowhere else; `dragClass` (source / valid /
dim) and `dropOnBlock` are derived from it, so what glows during a drag is what
a drop would accept.

## What an editable wire means on the server

| wire / control | server state |
|---|---|
| split `cloud` -> provider row | `provider.patch`: the upstream in `enabled_models`, `aliases[upstream] = served` when they differ. Cutting it drops the upstream |
| provider `enabled` checkbox | `provider.patch` `enabled` |
| rate limit -> split `limit` | `route.set` with `limits` for that served name; one block in several splits caps each |
| balancer dialog: policy | `route.set` with that policy and the name's current limits; back to `auto` with no limit is `route.clear`; a limit under `auto` is saved as `failover` (the dialog says so) |
| balancer dialog: share sliders | `route.set` under `weighted_capacity` with `shares` (target id -> whole percent, the sliders linked so they always sum to 100); "Use the measured split" sends `shares: {}`; another policy drops them |
| balancer dialog: Remove | stops every deployment serving the name (after the batch), switches off every cloud row aliased to it, `route.clear` |
| × on a machine row's flag chip | stop that deployment, then launch the same shape (context, concurrency, runtime, nodes, plan degrees > 1, MTP, custom command, remote code) with the flag and its values gone |
| provider row: Choose host | `provider.patch` `backend_pins` / `backend_fallbacks` for that upstream (a cleared pin sends `""` and `false`); the dry run refuses a tag OpenRouter does not list |
| draft model `deploy to` -> machines | the launch's `node_ids`; two or more machines = one instance with `parallelism.tensor_parallel` = the count |
| MTP checkbox + `draft tokens` | the launch's `speculative_tokens` (0 when unticked); shown only when the checkpoint has an MTP module |

Dragging an editable wire's end onto another socket that takes it moves the
wire (cut, then connect); onto a block's body, the first socket there that
takes it; onto a socket that refuses it, the refusal and the wire stays; onto
empty canvas, the wire is cut (ComfyUI's convention), announced as
`Wire dropped: <type> wire cut. Undo with Discard.` A selected editable wire
also shows a `Cut this wire` ×, and Delete cuts it.

Everything else — Input -> split, split `local` -> machine row, rows -> merge,
merge -> Output, machine links — is a server-reported fact: drawn, not editable.
A deployment spanning machines has a row on each; its wires attach on the
lowest `node_id` only.

## The document (`doc.ts`)

- `fromServer(snapshot)` — routes (`auto_selected` reads as `auto`), enabled
  provider models, one limit block per group of identical non-empty limits, no
  draft models.
- `connect` / `disconnect` and the `add*`/`set*`/`remove*` helpers return a new
  `Doc`; `connect` assumes `canConnect` already returned `null`.
- `diff(base, next, snapshot)` — `provider.patch` ops (including per-key
  `backend_pins`/`backend_fallbacks`), then `route.set` where limits or the
  policy changed, then `route.clear` for removed names and for a policy handed
  back to `auto` with no limit. It never emits launches or stops.
- `launches(doc, plans, snapshot?)` — one `LaunchRequest` per draft model,
  built as `tabs/models/useModelSession.ts` builds one, plus `node_ids` and `speculative_tokens`;
  given the snapshot, then one per pending relaunch (`relaunchesOf`). `stops(doc)`
  — removals, then relaunches. `flagGroups` reads `--flag value` as one flag.
- `removeServedName(doc, snapshot, name)` and `removalSentence` — the whole
  removal and the confirm that names it.
- `validate` — the issues for the Checks card. Server-owned rules (catalogue
  membership, limit validity, fit, a backend host's tag) come back from the dry
  run instead.
- `rebase(base, doc, next, deployments?)` — per entity: untouched follows the
  server, edited is kept, edited on both sides is kept and reported as a
  conflict. A relaunch or stop drops out once its deployment no longer runs; a
  route clear once the server no longer routes the name.
  `takeServer(doc, next, key)` resolves one conflict to the server's side.
- `dealPositions(blocks, stored, sizes?)` — stored position wins, else a
  deterministic column deal stacked by measured sizes (estimates until React
  Flow has measured), then `resolveOverlaps` moves every dealt block down until
  it touches nothing; a stored block is never moved.

## Dry run and Save

500 ms after `diff()` changes and is non-empty, `POST /api/wiring` with
`dry_run: true`; each failed row is shown on its block verbatim. Save posts the
same ops with `dry_run: false`; then, if they applied, stops each deployment in
`stops()`, then calls `backend.launch` for each draft launch and each relaunch
in sequence, each with its own result row (a relaunch whose stop failed is
`not applied`), then `invalidate()`. A 400 `wiring_invalid` renders its per-op
`results`. Applied changes fold back into `base` on the next poll through
`rebase`. No undo; Discard returns to server state.

## Particles

Request blocks ride `requests` wires, keyed to a wire id by `keyFor`, one per
request in flight. Response blocks ride `responses` wires at a rate derived from
tok/s, capped (`STREAM_MAX_HZ`). Paths come from `PathRegistry`, which
`WireEdge` fills with its rendered `<path>`. Reduced motion suppresses them
entirely.

## Positions

Dragged positions are stored per cluster id in localStorage under
`derate.wiring.positions.<cluster>` as `{ v: 1, positions }`. Garbage, another
version, more than 256 entries or a non-finite coordinate drops the whole
record: losing it costs a re-deal, nothing else. `Arrange` clears the record:
every block takes its dealt, overlap-free place. A block added from the
toolbar is stored at its first free slot the moment it is added, so it never
lands on another.

## React Flow pitfalls

- React Flow's built-in node types `input` and `output` are styled by its own
  stylesheet, so the blocks register as `api-input` / `api-output`.
- Nodes are derived from the document in the same render, never copied into
  state in an effect: a node one render behind resets a controlled input
  mid-word.
- React Flow's wrapper writes inline `position: relative; height: 100%`, which
  needs a definite parent height, so it sits inside an absolutely pinned
  `.graph-box`.
- A non-connectable handle gets `pointer-events: none` from React Flow;
  `.sock-fixed` turns them back on so a press can announce the fixed sentence.
- The canvas mounts only while `/cluster` is the visible destination
  (`AppShell` passes `active`); `ClusterTab` keeps the draft across navigation.

## Checks

Each is runnable alone (`cd ui && node src/tabs/cluster/<name>.check.mjs`) and
discovered by `npm run check`.

- `sockets.check.mjs` — every socket-table row, the left-in/right-out side
  rule and its two exceptions, every refusal sentence once, fixed sockets not
  connectable, drag classification for a cloud and a limit drag.
- `doc.check.mjs` — the shared fixture `tests/fixtures/wiring_rules.json`
  (`tests/unit/test_wiring_rules.py` posts the same ops to the server, so the two
  pin one contract), `fromServer` round trip, `diff(base, base)` empty, op
  order, row and multi-node rules, MTP in `launches()`, `rebase` conflicts,
  stable `dealPositions`, `resolveOverlaps`, policy route.set/route.clear,
  removal, flag relaunches, pins.
- `dialog.check.mjs` (browser, coordinator) — the balancer dialog on the real
  screen: eight policy options, the limit note under auto, the removal confirm
  equal to `removalSentence` and to its template.
- `positions.check.mjs` — garbage, wrong version, >256 entries, NaN are dropped.
- `particles.check.mjs` — emission is one block per request in flight, flight
  times, stream rates and their caps.
- `loading.check.mjs` — a launch's phase and stepper rules.
