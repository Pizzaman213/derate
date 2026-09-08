# shell

The frame every screen lands in, and the one thing in this repository that
looks at the result. Three components build the chrome -- the header bar, the
destination panels with their collapsible rail, and the single modal sheet --
and a fourth file drives a real browser over the built app and asserts that
what shipped renders, answers and leaks nothing.

Nothing here knows what a dashboard or a cluster graph contains. It mounts
`SelectionProvider` (which `state/selection.tsx` defines) and owns the
destination-to-panel mapping, the sheet's mechanics, and the boundary between
"a URL changed" and "a screen is showing".

## Layout

| File | Lines | What it owns |
|---|---|---|
| `AppShell.tsx` | 185 | the frame: `SelectionProvider`, the six sections, the rail's auto-collapse, `useFirstRunRedirect` |
| `Header.tsx` | 116 | the monogram, the roster pill, the six nav anchors, and the stored theme |
| `screens.check.mjs` | 215 | the only verifier that opens a browser; writes `ui/screens/<dest>.png` |
| `Sheet.tsx` | 249 | the one modal: portal, scrim, scroll lock, focus trap, and the kind dispatch |

## `AppShell.tsx`

`AppShell()` reads `route.dest` from `useRouter()` and renders every
destination at once, each in a `<section aria-label>` that is `hidden` unless
it is the current one. They are sections and not tabpanels on purpose: the
header's destinations are anchors to real URLs rather than tabs, and a tabpanel
with no tab pointing at it is a promise to a screen reader that nothing keeps.
Staying mounted while hidden is what preserves a destination's in-flight polls
and its scroll position across a visit elsewhere.

`SelectionProvider` is mounted here rather than in `main.tsx`. Selection --
which node, which link, which deployment, which sheet -- is shell state, needed
by `Sheet` and by every destination and sidebar section, and nothing outside
the shell has a reason to reach it. `AppShell.tsx` also re-exports `Dest` from
`state/router`; the set of destinations and the set of path segments are the
same set, and defining it twice is how they drift apart.

`setup` returns early, above the frame: `if (dest === 'setup') return
<SetupTab />`. On a fresh install the header, the roster rail and every panel
are empty, and chrome around empty boxes is a worse first impression than no
chrome. It is still a real destination with a real URL, so it can be linked,
reloaded and re-run by typing it.

### The rail collapses on the destination, not on the click

`WIDE` is `new Set<Dest>(['models'])` -- destinations that are themselves a
split and want the full width. Models puts a master list beside a detail pane;
with the roster rail open as well that is three columns on a 1440px screen and
the detail pane ends up narrower than the list feeding it.

The effect is keyed on `dest`, not on the tab click, because a destination now
also arrives from the Back button and from a pasted link -- a rail that only
collapsed when you clicked the tab yourself would leave a shared `/models` link
rendering its split in two thirds of the width. `restore` remembers what the
rail was before a wide destination closed it, so leaving one restores the
choice instead of silently reopening a rail somebody had deliberately shut.
`openRef` mirrors `sidebarOpen` so the effect can read the current value
without taking it as a dependency; without the ref it would fire on every
manual toggle. An explicit click on the rail button sets `restore.current =
null` and so overrides the automatic behaviour in both directions: once
somebody has said what they want the rail to do here, restoring an older value
would fight them.

### `useFirstRunRedirect`

Sends a coordinator nobody has set up to `/setup`, exactly once. Three
constraints, each of them a failure it prevents:

- **Once**, guarded by `asked`, so a re-render never re-asks.
- **Only from the default landing destination.** `startedAt` is a ref holding
  the destination the person arrived on, read through a ref so the effect does
  not re-run when the destination changes; the redirect fires only if that was
  `dash`. A person who typed a URL, followed a link or clicked a tab has said
  where they want to be, and yanking them out of it because a fetch came back
  late is worse than never offering setup at all.
- **`{ replace: true }`, never a push**, so Back does not land on the dashboard
  they never saw.

`backend.setup()` failing does nothing at all -- the `.catch(() => {})` is
deliberate. The dashboard on a fresh install is a thin screen but a working
one, and a coordinator that cannot answer `/api/setup` has a bigger problem
than onboarding.

## `Header.tsx`

`Header({ dest })` renders the monogram, the wordmark, a roster pill, an
optional `cloud off` pill when `settings.data.local_only`, and the nav. The
pill counts nodes, healthy nodes, and deployments in state `ready` or
`degraded` -- three numbers off `useCluster()`, and the whole pill is absent
until `cluster.data` arrives rather than showing zeros.

`DESTS` lists six destinations: Dashboard, Models, Cluster, Chat, Spend,
Settings. `setup` is a real `Dest` and is deliberately not among them -- it is
reachable by URL and by the first-run redirect, and putting it in the bar would
advertise a screen that is finished the moment it is used.

**The destinations are anchors, not buttons.** Each has a real URL from
`linkTo({ dest })`, and a real URL is only worth having if the browser's own
affordances reach it: right-click to copy the link to the Cluster view,
middle-click to open Models in a second tab, hover to see where a tab goes. The
click handler runs only for `plainClick(e)` -- no meta, ctrl, shift or alt, and
button 0 -- and a modified click falls through to the browser, which is the
whole point. `aria-current="page"` marks the current one.

**There is no lamp in this header.** A dropped metrics stream (`stream.status
=== 'stale'`) puts `stream-fault` on the `<header>` element, and
`styles/derate.css` turns the bar's own bottom rule `--fault` red. The lamp on
its own was easy to miss.

`useTheme()` applies `applyTheme(loadTheme())` once on mount. The control that
changes it moved to Settings -> Appearance
(`tabs/settings/AppearanceCard.tsx`); this stays here so the stored theme takes
effect on every destination and not only on the one that owns the control.

## `screens.check.mjs`

`// requires: browser coordinator` -- the first verifier in this repo that
looks at a screen, and the only thing in the project that sees what shipped
rather than inferring it from source. Every other `*.check.mjs` checks a pure
function: the URL scheme, the graph layout, the quantization ladder, the QR
encoder. That is the right shape for most of what can go wrong and it leaves
one whole class untouched -- the app can pass the entire suite *and* a green
typecheck and still render a blank page, because "it mounted" is not a type and
nothing here had ever loaded the bundle.

```bash
node src/shell/screens.check.mjs                 # assert, and capture
node src/shell/screens.check.mjs --capture-only   # capture only (npm run screens)
DERATE_CHECK_ORIGIN=http://localhost:18088 node src/shell/screens.check.mjs
```

`ORIGIN` defaults to `http://localhost:8088`. `npm run screens` is the
`--capture-only` form and asserts nothing; `npm run check` runs it with
assertions on.

**No browser is downloaded to run it.** `findBrowser()` from
`../check/browser.mjs` finds the Chromium already in the Playwright cache and
says plainly when there is none, which is why `playwright-core` is the
dependency: it never fetches anything, and an explicit `executablePath` means
the driver's version does not have to match the cached browser's revision. The
page is opened at 1440x900, `reducedMotion: 'reduce'`, `colorScheme: 'dark'`.

### The screen list is derived, never written down

`SCREENS` is built from `routes.DESTINATIONS`, loaded out of `state/routes.ts`
through the harness's `load()`. This was a literal array of seven paths under a
header that already claimed it was "one per destination in `state/routes.ts`",
and the two disagreed the moment an eighth was added: `/speech` shipped, and
the only verifier that opens a real browser did not know it existed. The name
is the path segment, so `screens/<dest>.png` keeps the filenames it had.

Three more screens are appended when the live coordinator has the material, and
their ids come off `/api/models`, `/api/topology` and `/api/deployments` rather
than being written here -- a hardcoded model id rots the moment the cluster
changes, and a verifier that silently walks a 404 is worse than one that does
not walk it at all. `model-detail` is `/models/<id>`; `node-sheet` is
`/cluster?open=node:<id>`; `dep-sheet` is `/dashboard?open=dep:<name>`, picked
by `depRank` in the order ready, degraded, launching/planned, everything else,
because the sheet of a stopped deployment is mostly dashes and asking one for
its log costs a `sparkrun logs` on the machine.

### What it asserts

Per screen, after `domcontentloaded`, a best-effort `networkidle` wait capped
at 15s, and a stylesheet that kills every animation and transition -- the
particle field never goes idle, so a screenshot has to be of a screen rather
than of an animation mid-stride:

- `#root` has non-zero height.
- No console error. `Failed to load resource` is excluded here because the
  response handler already judges that one by origin; counting it twice would
  fail the run on a third party.
- No uncaught rejection (`pageerror`).
- Nothing >= 400 **from this coordinator**. Off-site failures go to `offsite`,
  are reported with `note()` and are deliberately not gated: the publisher
  avatars come straight from huggingface.co and come back 429 in bulk, and
  failing the gate on somebody else's rate limiter would make it useless. They
  are reported once per screen through `note()`, which prints the count and the
  first URL. They do **not** reach `screens/console.txt`: `transcript` takes one
  line per screen -- name, path, `root=`, `errors=`, `assets=` -- plus the text
  of each console error, and `offsite` is never pushed to it.
- No stored secret is on screen.

**The secret scan runs the server's own `Redactor`, not a shape heuristic.**
`secretScan()` shells `python3` into the repo root, loads
`control_plane.providers.secrets`, primes a `Redactor` with every value in the
real `SecretStore` plus every `*_API_KEY` in the environment, and asks
`contains_secret(document.body.innerText)`. The first cut used
`looks_like_secret()` on every long word and flagged
`audeering/wav2vec2-...`: a model id is exactly as long and as random-looking
as a key, so a shape test over rendered text can only cry wolf. Comparing
against what is actually in the store cannot. When the box has no stored
secrets, the run says so with a `note()` rather than passing a check it did not
perform.

### The deep-path block

After the loop, four fetches pin the fallback that `routes.ts` names and
nothing else checked -- a screen URL typed straight into the bar has to be
answered with the app, and breaking it 404s a shared link only on reload, which
is the failure that makes people stop sharing links.

- `/models/<live id>` with a navigation `Accept` header is `ok`.
- `/models/meta-llama/Llama-3.1-8B` with `accept: text/html` is `ok`. The
  dotted case is about the *shape* of a path, not about whatever this cluster
  holds, so it uses a fixed id; the first cut asked this of a live model id and
  passed or failed depending on whether that id happened to have a dot in its
  last segment.
- The same dotted path with `accept: */*` is a 404. A model id is allowed a
  dot, so a request for one is indistinguishable from a request for a missing
  file by path alone -- the header is the whole difference, and both halves of
  that rule are worth pinning.
- `/assets/index-doesnotexist.js` is a 404 and not `index.html`, because an
  asset 404 that renders as a page is a broken deploy with nothing in the
  network log to explain it.

## `Sheet.tsx`

The one modal host. Each inspector owns its own header row -- lamp, label,
Close -- and this file owns only the mechanics every use of the sheet needs
regardless of which one is showing: the portal into `document.body`, the scrim,
the scroll lock, and a focus trap that holds across a content swap.

`SheetBody` dispatches on `sheet.kind` from `state/selection`'s `SheetTarget`
(the URL's form of the same thing is `SheetRef` in `state/routes.ts`):
`model` -> `ModelInspector`, which resolves its own payloads because a model is
not something the cluster roster holds; `node` -> `NodeInspector` after finding
the node by `profile.node_id`; everything else -> `DeploymentInspector`. `Gone`
-- two lines reading "No longer in the cluster." -- is the `node` and `dep`
fallback only, taken when the id matches nothing in `cluster.nodes` or
`cluster.deployments`. The `model` branch has no such guard: it never consults
the roster, so it has nothing to miss in.

**A deployment sheet takes the live row, not the first match.** A sheet is
addressed by served name and the ledger can hold several rows under one: three
failed attempts at a model that is now up are four rows named the same thing,
in store order. Taking the first opened the oldest corpse of a model that is
serving right now. `runners(named)[0] ?? named[0]` prefers a non-terminal row
and falls back to the ledger row, so a model whose every attempt failed still
opens and still says why.

`size` is three-way: `card full` for `node` and `dep` -- both are pages rather
than cards, two columns with the backend log filling the right one, which a
700px card would have squeezed into two narrow strips -- `card wide` for
`model`, which carries a quantization ladder, and bare `card` for anything
else. Bare `.card` sets no max-height at all, which is fine now that the only
thing left on it is `Gone`.

### The focus rules are three separate effects, deliberately

- **Open-session effect**, keyed on `open` alone: remember
  `document.activeElement`, set `body.style.overflow = 'hidden'`, and reverse
  both on close. Keyed on `open` and not on the target, because switching from
  one node to another while the sheet stays open would otherwise "remember" a
  button inside the sheet itself as the restore target.
- **Initial focus**, keyed on `open`, `sheet?.kind` and `sheet?.id`: focus the
  first focusable element, or the card. It re-runs on a content swap because
  otherwise focus is left on whatever the *previous* inspector rendered at that
  DOM position -- a button the new inspector may not even have.
- **Keydown**, on `document`: Escape closes; Tab is contained.

**The Tab trap tests containment, not just the two ends.** If focus is anywhere
outside the card -- it escaped via a removed element handing focus back to
`<body>`, a stray portal, anything -- Tab snaps it back inside, rather than
only catching the boundary case. The card itself is a valid Shift+Tab
boundary: it is `tabIndex={-1}`, focusable by a click on its own padding and
the fallback target when it has no focusable children at all, and
`card.contains(card)` is true, so without the `active === card` clause a
Shift+Tab from the card would fall through to the browser's default and move
focus outside the portal entirely -- the card lives under `document.body`.

**`SheetBody` is a plain function, not a component defined inside `Sheet`.**
Nesting it there hands React a new function identity every render and remounts
the inspector, dropping focus, on every parent re-render -- exactly the failure
this file exists to prevent.

The scrim's click handler fires only when `e.target === e.currentTarget`. A
click that started inside the card and released on the scrim -- a drag
selection -- still targets the card, so this closes on an actual scrim click
and not on a sloppy text selection.

## The seam with the rest of the UI

`main.tsx` is the only importer of `AppShell`, and it mounts the providers this
folder assumes:

```tsx
<RouterProvider>       {/* outermost: the URL decides which screen mounts */}
  <BackendProvider>
    <MetricsProvider>
      <TelemetryProvider>
        <AppShell />
```

Downwards, `AppShell` imports the seven destination components
(`tabs/DashboardTab`, `ModelsTab`, `ClusterTab`, `ChatTab`, `SpendTab`,
`SettingsTab`, `SetupTab`) and `sidebar/Sidebar`, and passes them nothing. They
read `useSelection()`, `useRouter()` and the resource hooks themselves.

`Sheet` is the only consumer of `useSelection().sheet`.
`inspectors/node/NodeInspector` and `inspectors/DeploymentInspector` are reached
from nowhere else. `tabs/models/ModelInspector` is not: `tabs/ModelsTab.tsx`
also renders it inline as the Models split's detail pane, keyed on the selected
id, and hands it `providers` and `providerKinds` off polls the list already made
-- props the sheet does not pass, because the sheet has no list to have polled
them for. Changing its signature touches both call sites.

Nothing imports `Header` or `Sheet` outside this folder. `components/Popover.tsx`
records why it is deliberately *not* this file: a portalled modal with a scrim
is the wrong shape for a transient anchored panel.

## Things that look like details and are not

**`Header.tsx` draws the monogram that `docs/screenshots/brand/build.py` copies, so the two
change together.** The build script says so in its own comments -- the two
`<path>` strings, `M8 25 H26 V11 H56` at full opacity and `M26 25 V39 H56` at
0.32, are transcribed there as `("solid", ...)` and `("echo", ...)`, and the
wordmark's `fontWeight: 500`, `fontSize: 18` and `letterSpacing: '-.3px'` are
transcribed too. Change either here and rerun `python3 docs/screenshots/brand/build.py`, or
the README's banner stops being the mark the product draws.

**The monogram's 2px nudge is not a rounding error.** The solid stroke sits
above the icon's own bounding-box centre and the faint echo stroke below it
does not carry the same visual weight, so flex-centring against the wordmark
leaves the mark looking high. `style={{ transform: 'translateY(2px)' }}`
corrects it, and `SetupTab`'s copy of the mark gets the identical offset from
`.setup-head svg { transform: translateY(2px); }` in `tabs/setup/setup.css`.

**`setup` is in `DESTINATIONS`, so `screens.check.mjs` captures it even though
the nav does not.** That is the whole value of deriving the list from
`SEGMENT`: a screen deliberately kept off the bar is still a screen, and the
one verifier that opens a browser walks it anyway. `ui/screens/setup.png` is
there for the same reason `dashboard.png` is.

**`ui/screens/` is written and never pruned.** `speech.png` is still on disk;
`/speech` was retired and is no longer a `Dest`, so nothing regenerates that
file and nothing deletes it. A PNG in that folder is evidence of a screen that
existed at some point, not proof of one that exists now -- read the folder
against `DESTINATIONS`, not the other way round.

**The `?open=` sheet is a sheet over a destination, not a destination.** Node
and deployment sheets have URLs (`/cluster?open=node:spark-01`) but no entry in
`routes.ts`'s `DESTINATIONS`, which is why `screens.check.mjs` appends them by
hand after the derived loop instead of finding them in the list.

## Failure behaviour

- **`/api/setup` fails or is slow.** `useFirstRunRedirect` swallows it and the
  dashboard renders. No redirect, no error surface.
- **The person is not on `dash` when the setup answer arrives.** No redirect,
  ever, whatever the answer says.
- **`cluster.data` is null.** The header renders monogram, wordmark and nav,
  and simply omits the roster pill rather than showing `0 nodes · 0 healthy ·
  0 serving`.
- **The metrics stream goes stale.** `stream-fault` on the header; nothing
  unmounts, nothing is hidden.
- **A sheet's subject vanishes from the cluster while it is open.** `Gone`
  renders with the id and a Close button. The sheet does not close itself out
  from under a reader.
- **Every deployment row under a served name is terminal.** `runners()`
  returns nothing and `named[0]` -- the oldest attempt -- is shown, so the
  failure is still readable.
- **No Chromium in the Playwright cache.** `screens.check.mjs` prints `no
  browser: <why>` and exits 1. It never downloads one, and `check.mjs` reports
  an unmet `requires:` as a skip in its own column with the reason printed --
  never folded into the passes. `--strict` makes that skip a failure.
- **The coordinator is down.** The three id-gathering fetches `.catch(() =>
  null)`, so `someModel`, `someNode` and `someDep` go null and only the derived
  destination screens are attempted -- each of which will then fail its own
  checks loudly rather than being skipped.
- **A third party 429s.** Counted into `offsite`, printed by `note()` in the
  run's output, not gated. The `assets=` column in `screens/console.txt` counts
  `badRequests` -- this coordinator's own 4xx and 5xx -- and nothing off-site,
  so a clean transcript is not evidence that nothing off-site failed.
- **`--capture-only`.** Asserts nothing, writes the PNGs, prints the count and
  exits 0. This is the only path in the file that exits 0 without running a
  check, and it is reached by an explicit flag rather than by an unmet
  condition.

## Deliberately not built

**A tab widget.** The destinations were buttons and `role="tabpanel"` sections;
both went when destinations got real URLs. Anchors give copy-link,
open-in-new-tab and hover-preview for free, and `aria-label`led sections make
no promise to a screen reader that no tab keeps.

**Unmounting the hidden destinations.** They stay mounted behind `hidden`, at
the cost of their polls continuing, because the alternative loses scroll
position and every in-flight request on every visit elsewhere.

**A shape heuristic for the secret scan.** Tried, and it flagged model ids. The
verifier asks the real store through the real `Redactor` or it says it had
nothing to look for.

**A hand-written list of screens, or of verifiers.** Both were tried and both
stopped covering the newest thing at exactly the moment somebody added one.
`DESTINATIONS` is derived from `SEGMENT`; `check.mjs` discovers `*.check.mjs`
files rather than listing them.
