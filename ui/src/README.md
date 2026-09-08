# ui/src

The whole browser application. Four files sit at this level and everything else
is a folder with its own README: the four are the entry point, the number
formatter, the theme switch and one type reference — the parts that have no
screen of their own and are needed by every screen that does.

The rule that reaches the furthest from here is `format.ts`'s: **a reading the
coordinator did not send renders as an em dash, never as zero, and never as an
em dash still wearing a unit it was never measured in.** Thirty-seven modules
import that file. It is the difference between a cluster that is idle and a
cluster nobody has heard from.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `main.tsx` | 30 | The entry point: fonts, the three stylesheets, and the four providers in dependency order |
| `format.ts` | 146 | Every number and unit the UI prints, and the em dash for the ones it does not have |
| `theme.ts` | 15 | `loadTheme` / `applyTheme` over `localStorage['derate.theme']`, as a `data-theme` attribute |
| `vite-env.d.ts` | 1 | `/// <reference types="vite/client" />` — what makes a CSS import typecheck |

## `main.tsx`

`ui/index.html` loads exactly one module, `<script type="module"
src="/src/main.tsx">`, and this is it. It imports the four IBM Plex faces from
`@fontsource` (Sans 400/500, Mono 400/500 — bundled, so the page never asks a
font CDN), then `styles/tokens.css`, `styles/base.css` and `styles/derate.css`
**in that order**, because tokens.css defines the palette the other two spend,
then mounts `AppShell` into `#root`.

The provider nesting is `RouterProvider` → `BackendProvider` →
`MetricsProvider` → `TelemetryProvider`, inside `StrictMode`. That order is a
dependency order, not a preference:

```tsx
<RouterProvider>          {/* the URL decides which screen mounts */}
  <BackendProvider>       {/* one backend, one origin, one revision counter */}
    <MetricsProvider>     {/* state/useMetrics.ts reads the backend — one SSE stream */}
      <TelemetryProvider> {/* calls useMetrics() and useCluster() */}
        <AppShell />
```

Only one of those edges fails loudly. `TelemetryProvider` calls `useMetrics()`,
which throws `useMetrics must be used within a MetricsProvider`, so hoisting it
above `MetricsProvider` is a white screen with a named cause. The other two
dependencies are silent. `MetricsProvider` does not call `useBackend()` itself —
it calls the subscription in `state/useMetrics.ts`, which does — and
`TelemetryProvider`'s `useCluster()` reaches the same hook through
`useResource`. `useBackend` is a bare `useContext` over a context whose default
is already populated (`httpBackend`, `revision: 0`, `origin: ''`), so it never
throws: hoist `MetricsProvider` above `BackendProvider` and the app renders,
streams from the default origin, and ignores every coordinator change after
that.

The router is outermost because the address bar is the question, and `useRouter`
is the other hook that throws on absence — the one with the most call sites
below it: `AppShell`, `Header`, `ModelsTab`, `ServePanel`, `SetupTab`,
`placement.ts` and `selection.tsx` all call it.

`SelectionProvider` is deliberately not here. `shell/AppShell.tsx` mounts it,
because which node, link, deployment and sheet are open is shell state that
nothing outside the shell reaches for.

## `format.ts`

The number layer. Its opening rule is that every readout declares its decimals
up front, so a value never changes width because it crossed a rounding
boundary — a column of figures that reflows while you watch it is a column
nobody can compare down.

`fmt(v, decimals)` returns a fixed-decimal string, or `—` for `null`,
`undefined` and any non-finite number. `fmtUnit(v, decimals, unit)` is `fmt`
plus its unit **dropped together**: `— GB/s` is a missing reading claiming a
unit it was never measured in. It was lifted here from two inspectors that each
carried a copy, when a third caller made it a convention; three copies of a rule
is how one of them quietly stops following it. `gbytes` and `gbNum` divide by
`1024 ** 3`, binary; `gbytes` guards `null` and non-finite input and `gbNum`
does not, because its parameter is a bare `number` and its call sites have
already established they have one. `pct` rounds for use inside a sentence — an
`aria-label` or a `title`, where an em dash reads strangely — and still refuses
to let `NaN percent` or
`Infinity percent` into copy. `relativeTime` is `Ns ago` / `Nm ago` / `Nh ago`,
and floors the elapsed seconds at 0, so a node whose clock runs ahead of the
browser's reads `0s ago` rather than a time in the future.

### The two labels that carry an incident

**`deviceClassLabel` exists because "unknown" was two different facts.** Every
surface that prints hardware does `shortGpu(gpu_name) || device_class` and used
to print the wire value raw, so a Raspberry Pi's row read `unknown` — the same
word the roster used for a DGX Spark whose container was started without
`--gpus`. The probe now tells those apart (`DeviceClass.CPU` vs `UNKNOWN`) and
this is where the distinction has to survive into the sentence somebody reads:
`cpu` becomes "CPU only", a description of the machine; `unknown` becomes
"unidentified", an admission about the probe. An unrecognised value passes
through as *itself* rather than becoming "unidentified", because a class this
build predates is not a class the coordinator could not read, and inventing the
stronger claim hides a version skew behind a hardware fault.
`shortGpu` strips a leading `NVIDIA ` or `GeForce `; the marketing prefix wastes
width in a 200px column, and "GB10" is the part that identifies the machine.

**`planShortFromDegrees` is a port of `internal_api.py::_plan_label`, and it had
drifted twice over.** It never read `data_parallel`, and it joined with `' · '`
where the server joins with `' + '`. The cluster floor plates render the
server's own string (`TopologyDeployment.plan`) while every other screen
rendered this one, so one deployment with DP > 1 read as two different plans
depending which screen you were on — against the rule that planner strings are
the product and go on screen verbatim. `api/contracts.check.mjs` now runs the
Python over a matrix of degrees and diffs it against this function, so the port
cannot drift again in silence. Prefer a caption the server sent; this is for the
call sites that hold degrees and no string.

### The two that are deliberately imprecise

`remainingLabel` renders an estimate somebody else measured, and the estimates
it renders come from tqdm, which extrapolates from throughput so far. So it is
vague at the top end and honest at the bottom: "about 4 min left" rather than a
"3m 47s" that would be spurious precision on a number moving every second, and
"under a minute left" rather than a second-by-second countdown that will be
wrong before it is read. Above an hour it is `about 2 h left`, or
`about 2 h 15 min left` when the remainder is not zero. `null` in, `null` out —
no estimate is not an estimate of zero, and every caller is expected to say
nothing rather than "0s left".

`sizeLabel` is the byte count at a scale that shows it, and exists because
`gbytes` is right for weights and wrong for everything under a gigabyte: a
1.6 MB metadata stub rendered as `0.0 GiB` reads as an empty measurement rather
than a small one, and the model cache is full of them. GiB at one decimal above
a gigabyte, MiB (one decimal below 10 MiB, none above), and a floor of 1 KiB so
nothing on the storage screen is a file of no size.

**Two files have a private twin of it that disagrees.**
`components/CacheTable.tsx` and `tabs/storage/ModelCacheCard.tsx` each declare
their own `sizeLabel` at the top of the file — identical to each other, and
different from the exported one in both units and rounding: `GB` / `MB` / `KB`
rather than GiB / MiB / KiB, no decimal on MB at any size, and an em dash for
`null` that the exported version will not take, since its parameter is a bare
`number`. So the storage screen prints one file's size in decimal-looking units
beside a memory budget printed in binary ones. Three copies of a rule and two
have already diverged is the exact failure `fmtUnit`'s docstring records,
happening one folder over.

## `theme.ts`

`Theme` is `'system' | 'light' | 'dark'`, the key is `derate.theme`, and
`applyTheme` does two things: set or remove `data-theme` on
`document.documentElement`, and write the choice to `localStorage`. `'system'`
*removes* the attribute rather than writing a third value, which hands the
question back to `styles/tokens.css`, where the dark block is
`@media (prefers-color-scheme: dark) { :root:not([data-theme='light']) }` and
the explicit override is `:root[data-theme='dark']`.

That is the whole of dark mode. Components reference token names and never a
literal colour, so there is no component-level branch to keep in step and no
second set of components — the file says it in one line: *dark mode is a token
swap, nothing here touches a component.*

Two callers. `shell/Header.tsx` applies the stored theme once on mount
(`useTheme()`), so it takes effect app-wide whichever destination renders
first. `tabs/settings/AppearanceCard.tsx` is the control, moved out of the
header on the argument that a colour scheme is a setting, not a destination
control.

## `vite-env.d.ts`

One line, `/// <reference types="vite/client" />`, and it is not decoration.
Nothing in `src/` reads `import.meta.env`; what the reference actually buys is
the ambient module declarations for non-TS imports. `tsconfig.app.json` is
`strict` with `include: ["src"]`, so without this file the three
`import './styles/*.css'` lines in `main.tsx` and the four `@fontsource`
imports are unresolved modules and `npm run typecheck` (`tsc -b`) fails at the
entry point.

## The folders

Each has its own README. One line of thesis here; the argument is over there.

| Folder | Importers | Thesis |
|---|---|---|
| [`api/`](./api/README.md) | 86 | Almost every call to the coordinator, and the one place that should hold them all. Live only, and every response is scrubbed of keys on the way in |
| [`state/`](./state/README.md) | 65 | Everything the UI knows that is not a component: the URL scheme, polled resources, the one SSE stream, and the vocabulary every screen agrees on |
| [`components/`](./components/README.md) | 34 | The shared primitives — `Verbatim`, `Readout`, `Lamp`, `Panel`, `Bars`, `Popover`, `Copyable`, `OverrideGate`, `CacheTable` |
| [`tabs/`](./tabs/README.md) | 7 | The destinations. Seven screen components, each with a folder of its own parts behind it, plus `storage/`, which is four cards composed inside Settings |
| [`shell/`](./shell/README.md) | 1 | The frame: header, app shell, the one sheet — and `screens.check.mjs`, the only verifier that opens a browser |
| [`sidebar/`](./sidebar/README.md) | 1 | The right-hand rail: roster, activity, the scoped plan, that deployment's routing and its cost per Mtok |
| [`inspectors/`](./inspectors/README.md) | 1 | Deployment detail and its log, plus `node/` — the thirteen parts of a machine's page |
| [`styles/`](./styles/README.md) | — | The token layer. `tokens.css` is the entire palette, `base.css` the reset, `derate.css` the structural chrome |
| [`check/`](./check/README.md) | — | Shared machinery for the `*.check.mjs` suite — `load()`, `report()`, and where the Chromium is. Not verifiers itself |

`styles/` and `check/` show no TypeScript importers because neither is imported
as a module: the stylesheets enter through `main.tsx`'s three side-effect
imports, and `check/` is imported only by the `.mjs` verifiers scattered through
the other folders.

One module outside `api/` calls `fetch` directly. `tabs/models/owner.ts`'s
`flush()` batches publisher names into
`/api/publishers/avatars?owners=<names>` against the page's own origin, so it is
the one read that does not go through `api/client.ts` and therefore not through
`coordinatorBase` — point the UI at another coordinator and the avatars still
come from this one.

## The seam with the rest of the app

Nothing imports `main.tsx`; `index.html` names it and Vite starts there. The
other three are imported outward:

- **`format.ts`** — 37 modules import it. `gbytes` in 15 of them,
  `relativeTime` in 10, `fmt` in 8, `planShortFromDegrees` in 7, `shortGpu` in
  6, `fmtUnit` in 5, `gbNum` and `deviceClassLabel` in 4 each, `pct` in 3,
  `sizeLabel` and `remainingLabel` in 2. `api/contracts.check.mjs` is an
  eighth user of `planShortFromDegrees` and is not in that 37: it reaches the
  function through an esbuild bundle, not a static import. Import the helper;
  do not re-implement it beside the readout — `sizeLabel` is down at 2 because
  two files did exactly that.
- **`theme.ts`** — exactly two: `shell/Header.tsx` and
  `tabs/settings/AppearanceCard.tsx`.
- **`vite-env.d.ts`** — imported by nothing and required by everything that
  imports a stylesheet.

```tsx
import { fmtUnit } from '../format'

// inspectors/node/Interconnect.tsx — no reading and the unit goes with it
{fmtUnit(edge?.all_reduce_gbps, 1, 'GB/s all-reduce')}
```

`components/Readout.tsx` is the other half of the *width* rule, and not of the
unit rule. It takes the raw `number | null | undefined`, calls `fmt` itself, and
sets `minWidth: <width>ch` with `fontVariantNumeric: 'tabular-nums'`, so the
digits never move. Its `unit` is a separate `<span>` rendered whenever the prop
is set and never consulted about what `fmt` returned, which means
`<Readout value={null} unit="GB/s" />` prints the `— GB/s` that `fmtUnit` exists
to prevent. Where the unit has to vanish with the reading, call `fmtUnit`.

## Things that look like details and are not

**The provider order in `main.tsx` is a dependency graph written down, and only
part of it fails loudly.** Two of the four hooks throw on a missing provider —
`useRouter` (seven call sites below it) and `useMetrics` (one, `TelemetryProvider`)
— so those reorderings are a white screen with one named console error.
`useBackend` does not throw; it reads a context with a populated default. Hoist
`MetricsProvider` above `BackendProvider` and nothing complains, the SSE stream
just pins itself to the default origin for the life of the tab. The loud half is
the half you will notice.

**An em dash is a claim, and zero is a different claim.** `fmt` returning `—`
says the coordinator did not send this number. `0` says it sent zero. On a
throughput readout those are "the stream is down" and "the cluster is idle",
which want completely different actions from whoever is looking.

**`gbytes` and `sizeLabel` are not two spellings of the same function.**
`gbytes` always answers in GiB, which is right for weights and for a memory
budget where every figure must be comparable down a column. `sizeLabel` picks
the scale that shows the number, which is right for a cache listing where the
entries span six orders of magnitude. Using the first where the second belongs
prints `0.0 GiB` for every small file in the store.

**`planShortFromDegrees` is the fallback, not the source.** Where the server
sent a caption, render the server's caption. This function is for the call
sites that hold degrees and no string, and it is verified against the Python by
`api/contracts.check.mjs` precisely because a second implementation of a product
string is a second answer to the same question on the same screen.

**Three stylesheet imports, and their order is the cascade.** `tokens.css`
first, because `base.css` and `derate.css` both reference token names. Moving
`derate.css` above `tokens.css` does not fail a build; it produces a page whose
colours are whatever the browser defaults to.

## Failure behaviour

- **No `#root` in the document.** The `!` in
  `document.getElementById('root')!` is a compile-time assertion and erases to
  nothing, so the `null` reaches `createRoot`, which throws
  `Target container is not a DOM element.` at module scope. The page is blank
  with one console error and nothing else runs.
  `shell/screens.check.mjs` is what catches this: it opens every destination in
  a real browser and asserts `#root` has a rendered height, because "it mounted"
  is not a type and a green `tsc -b` has never proved it.
- **A `null`, `NaN` or `Infinity` reading.** `fmt`, `gbytes` and `pct` return
  `—`; `fmtUnit` returns `—` with no unit attached. Nothing renders zero.
- **A `null` handed to `gbNum` or `sizeLabel`.** Neither guards — both take a
  bare `number` — so the first returns `NaN` and the second the string
  `NaN KiB`. `strict` catches a literal `null`; it does not catch a widened
  field that is `number` at the call site and absent on the wire.
- **A device class this build has never heard of.** `deviceClassLabel` passes it
  through unchanged rather than calling it "unidentified". `undefined` becomes
  the empty string, which lets the caller's `||` fall through to its own
  fallback.
- **No remaining-time estimate, or a negative one.** `remainingLabel` returns
  `null` and the caller renders no caption at all.
- **A corrupt `derate.theme` value.** Anything that is not `light` or `dark` is
  written to `data-theme` and matches neither `:root[data-theme='dark']` nor the
  `:not([data-theme='light'])` guard's exclusion, so it behaves exactly like
  `system`. Missing entirely is `?? 'system'`.
- **`localStorage` that throws on access.** Neither `loadTheme` nor `applyTheme`
  guards it and no caller catches, so a browser that refuses storage takes
  `Header`'s mount effect down with it.

## Deliberately not built

**A component-level dark mode.** `tokens.css` owns the entire palette and
components reference token names only, so `applyTheme` is one attribute write.
The alternative — a second set of components, or a theme value threaded through
props — is a second thing to keep in step, and it goes stale the first time
somebody edits only one of them.

**A selection context at the root.** It belongs to the shell and
`shell/AppShell.tsx` mounts it there, on the argument that nothing outside the
shell has a reason to reach which node is open.

**A second copy of the plan caption as the default.** `planShortFromDegrees`
exists for call sites holding degrees and no string, and the rule stays "render
what the server sent". The verifier exists because the first version of that
rule was not enforced and the port drifted twice.
