# derate — chart & data-visualisation audit

Audited 2026-11-17 against the working tree (peer sessions edit concurrently; line
numbers are as read on that date). Since 2026-11-19 the pack holds only the six types
`PackChart.tsx` mounts; other pack type ids named below are at the `charts-full-pack` tag. Scope: every `<svg>`, `<canvas>`, `uPlot`,
`<path d=` / `<rect>` built from data, `getContext(`, and every CSS bar whose
width is driven by a value, under `ui/src`, plus the design pack in
`.omp/design/`. Pack type ids are from the chart-pack catalogue.

## Summary

- **One charting library:** `uplot@1.6.32`, used in exactly one file
  (`ui/src/tabs/dashboard/Chart.tsx`). Everything else is hand SVG or CSS bars.
- **19 chart-like items**, 7 not-a-chart look-alikes. Real charts: 1 time-series
  line (uPlot) used ×24 call sites across 4 screens, 1 SVG sparkline, 1
  segmented stacked bar, 1 proportion meter used ×9, 3 one-off CSS stacked/
  progress bars, 1 bespoke network diagram (with 4 embedded meters + an
  animated particle layer + an indeterminate loading bar), KPI readouts.
- **No chart tokens exist as CSS variables.** `chart grid / axis / warn band`
  exist only as *light-only primitives* in `tokens.json` (`color.alpha.ink-13`,
  `ink-30`, `amber-12`) and are not emitted; `chartTheme.ts` re-derives them at
  runtime from `--ink`/`--warn`.
- **No categorical data-viz palette.** The house rule is "colour reports state,
  never category" (`Bars.tsx:119-120`, `Chart.tsx:86-90`). `--tag-1..4` are
  chat-turn identity and `--avatar-1..10` publisher identity; neither is a
  sanctioned series palette.
- **A11y is uneven:** the ClusterGraph is fully keyboard-operable; the uPlot
  charts are `role="img"` + label only (crosshair is pointer-only, no data-table
  alternative); `SegmentBar`, `PhaseBar`, the routing weight bar and
  `.setup-bar` carry no role/label at all.

## Libraries

| Name | Version (installed) | Declared in | Used where | Verdict |
|---|---|---|---|---|
| `uplot` | 1.6.32 (`^1.6.32`) | `ui/package.json` | `ui/src/tabs/dashboard/Chart.tsx:12-13` only (+ `uPlot.min.css`, restyled in `ui/src/styles/derate.css:1085-1100`) | **replace, gated** — by the pack's canvas line renderer once it matches the behaviours listed under item 1. Until then keep. |
| `@xterm/xterm` / `@xterm/addon-fit` | 6.0.0 / 0.11.0 | `ui/package.json` | `ui/src/inspectors/node/Terminal.tsx` (terminal, not a chart) | keep — not data-viz |
| `react`, `react-dom` | 18.3.1 | `ui/package.json` | everywhere | keep — the pack must ship a React binding (or a framework-agnostic mount usable from React 18) |
| `@fontsource/ibm-plex-sans` / `-mono` | 5.3.0 | `ui/package.json` | `--font-sans` / `--font-mono` | keep — chart text must use these vars |
| `esbuild` | 0.21.5 | both | check harnesses bundle TS for node (`ui/src/check/harness.mjs`, `chart.check.mjs:26-49`) | keep |
| `playwright-core` | 1.63.0 (ui) / 1.65.1 (.omp) | both | `screens.check.mjs`, `.omp/tests/ui/*` | keep — version skew between the two trees |
| `axe-core` | 4.14.1 | `.omp/package.json` | `.omp/tests/ui/components/*.check.mjs` | keep — reuse for chart a11y checks |
| `svgo` | 3.3.5 | `.omp/package.json` | icon build | keep |
| `typescript` | 5.6.3 (ui) / 5.9.3 (.omp) | both | — | keep |
| d3, zod, vitest | — | **not present anywhere** | — | new deps the pack introduces; none conflicts with an existing one |

No Recharts / Chart.js / visx / ECharts / D3 anywhere. `Chart.tsx:37-39`
records why Recharts was rejected: its `spanGaps` does not default to false.

## Inventory

Legend — **Tech**: uPlot = canvas via uPlot; SVG = hand-written JSX SVG; CSS =
`<div>`/`<span>` with percentage widths. **Tests**: `*.check.mjs` that exercise
it (`screens` = `ui/src/shell/screens.check.mjs`, 215 lines, `requires: browser
coordinator`, captures every destination and asserts no console error / non-2xx /
empty root / leaked key — it does not assert on chart content).

### 1. `Chart` + `ChartGrid` — time-series line with optional band (uPlot)

- **Files:** `ui/src/tabs/dashboard/Chart.tsx` (438 lines; `ChartGrid` 65-72,
  `Chart` 112-225, `Plot` 245-384), `ui/src/tabs/dashboard/chart.ts` (205,
  pure arithmetic), `ui/src/tabs/dashboard/chartTheme.ts` (90),
  `ui/src/styles/derate.css:1039-1100` (`.chartgrid`, `.chart`, `.ax`, `.plot`,
  uPlot cursor overrides with `!important`).
- **Call sites (24 charts):** `tabs/dashboard/TelemetrySub.tsx:40-113` (3 grids:
  cluster ×2, node ×4, deployment ×3), `inspectors/node/NodeCharts.tsx:74-102`
  (113 lines; 1 grid ×4 with `band`, `envelope`, `note` from history windows),
  `inspectors/node/ServingBlock.tsx:84-95` (×3 in a bare `.chartgrid` div — **not**
  a `ChartGrid`, so no cursor sync there).
- **Draws:** one stroke (1.5px `--ink`) over time; optional band between avg and
  bucket max / p50 and p99 (`bands: [{series:[2,1]}]`, 331) with a 0.75px band
  edge; known archive gaps as filled rects under the trace (`drawClear` hook,
  337-351; the comment at 98-102 says "hatched", the code fills solid
  warn@12%). No axes, no legend (287-288): the box's own `<h4>` title +
  live/hover readout and a `min · note|clock · max` line (212-222) are the chrome.
  x-only crosshair, 5px cursor point (290-298). Empty/all-null → "no samples
  yet" (160-170), never a flat zero.
- **Data shape:** `TelemetryPoint[]` = `{ t: number /*unix s*/, v: number|null }`;
  optional `band: TelemetryPoint[]`; `envelope: HistoryEnvelope` (gaps,
  resolution); `unit` → `YKind` (`'percent'|'rate'|'span'`, `chart.ts:32,120-124`)
  → fixed y range (`yRange`, 134-153: percent 0..100, rate from 0, span with a
  10-unit floor, 8% headroom). `align()` (96-114) merges N series onto one strictly
  ascending x column and **never interpolates** — `Columns = [number[],
  ...(number|null)[][]]` (51).
- **Volume:** live ring 60 s @1 Hz (`state/useTelemetry.ts:38`); archive windows
  `5m | 1h | 24h` (`state/history.ts:27-50`); a 24-hour window is "tens of
  thousands of points" (`Chart.tsx:33-35`) — budget ≥50k points/series.
- **Theming:** `chartTheme.ts` reads live computed `--ink`, `--ink-muted`,
  `--rule`, `--panel`, `--warn` from `:root` (54-71), derives `band = ink@0.13`,
  `bandEdge = ink@0.30`, `gap = warn@0.12` via `alpha()` (47-52), falls back to
  `tokens.ts` light values (34-42, `design-pack: computed-color` marker), and
  rebuilds the uPlot instance on `prefers-color-scheme` change or `data-theme`
  mutation (77-90). No literal colours remain (`.omp/design/AUDIT.md:50-57` still
  lists literals at 30-36/56 — stale). `muted`, `rule`, `panel` are read but
  unused by `Plot` (cursor styling is done in CSS instead).
- **Perf design:** instance built once per `(width>0, height, hasBand, syncKey,
  theme)` (364); data via `setData(cols, false)` (371), size via `setSize`,
  range/gaps via refs + `redraw(false,false)` (379-381); memo keyed by
  `seriesKey()` content signature, not array identity (141-147). Waits for a
  non-zero width (ResizeObserver, 265-274) because hidden sub-tabs mount at 0.
- **A11y:** host `<div role="img" aria-label="{title}[, average and {bandLabel}]">`
  (383). No keyboard crosshair, no `aria-live` on the readout, no tabular
  alternative. Hover readout over a gap says `missing · {reason}` (182-183).
- **Tests:** `ui/src/tabs/dashboard/chart.check.mjs` (270 lines, hermetic) pins
  `align` null-preservation incl. NaN/Infinity and band-vs-avg timestamp
  disagreement (71-114), strict ascending x / dedupe (116-133), `yRange` rules
  (135-184), `bounds` null≠0 (186-205), `seriesKey`, `gapSpans`/`gapAt`.
  `ui/src/state/history.check.mjs` (241, `requires: fixtures HISTORY_FIXTURES`)
  covers the series/band/envelope feeding it. `screens` captures it. **Nothing
  asserts on the rendered canvas or on `spanGaps:false` itself** — the promise is
  pinned at the `align` boundary.
- **Pack type:** `line` (+ `range-area` band, gap annotations); `ChartGrid` =
  `small-multiples` with a synced x-crosshair.

### 2. `ChartCell` — sparkline (hand SVG)

- **File:** `ui/src/tabs/dashboard/Chart.tsx:396-438`; used once per running
  deployment row in `tabs/dashboard/DeploymentsStrip.tsx:129` (176 lines).
- **Draws:** one `<path>` in a `100×24` viewBox, `preserveAspectRatio="none"`,
  1px `var(--ink)` with `vector-effect: non-scaling-stroke`; pen lifts on null
  (`M` after a hole, 407-419); self-scaled min..max with 2px inset. <2 points →
  "no samples yet" (398-400).
- **Data:** `TelemetryPoint[]` (`telemetry.depTps[served_name]`, the 60 s live ring).
- **Theming:** `var(--ink)` only. **A11y:** `role="img"` + `aria-label`
  ("{name} throughput over the last 60 seconds"); the row itself is a keyboard
  button (`DeploymentsStrip.tsx:103-119`).
- **Tests:** shares `align`/`bounds` with item 1 (`chart.check.mjs`); `screens`.
- **Pack type:** `sparkline`.

### 3. `SegmentBar` — memory breakdown against the fit line (CSS)

- **File:** `ui/src/components/Bars.tsx:1-124` (177 total). One call site:
  `tabs/models/Verdict.tsx:248-251` (537 lines).
- **Draws:** absolutely-positioned segment `<div>`s across a recessed track;
  span = `max(total, usable, ceiling) × 1.02` (33); every segment `--ink` at a
  descending opacity ramp 0.85→0.30 (`shade`, 121-124) — monochrome by policy;
  segments past `usable` turn `--fault` when the total overruns (59, 70-71);
  the gate's limiting term is **outlined** 1.5px, not recoloured (76-77); a 2px
  solid `--ink` rule at `usable` that overhangs the track by `--space-4` (100-113)
  and a 1px dashed `--ink-muted` rule at the static `ceiling`, suppressed when
  ≤ `usable` or within 1% of it (39-43, 85-98). `usable: null` → no line, no
  fault region.
- **Data:** `Segment[] = {key, label, bytes, limiting?}`, `usable: number|null`,
  `ceiling?: number|null`, `height` (22).
- **Theming:** `--panel-recessed`, `--rule`, `--ink`, `--fault`, `--panel`,
  `--ink-muted`, `--z-base`, `--z-sticky`. Registry/DESIGN claim
  `--bar-track/keyline/limiting` and `panel-sunk, edge, fill, warn-solid`
  (`.omp/design/components/registry.json` SegmentBar entry;
  `DESIGN.md:270-276`) — **the code uses none of the `--bar-*` tokens** (drift).
- **A11y:** none — no role, no aria-label; only `title` tooltips per segment
  (63) and per rule (96, 111). Screen readers get nothing.
- **Tests:** `.omp/tests/ui/components/segmentbar.check.mjs` (9 lines,
  `requires: browser`; generic variant×state render + axe via
  `../lib/component.mjs`, states `fits|overrun|limiting|no-usable`); `screens`.
- **Pack type:** `stacked-bar` (single horizontal row) with reference-line
  annotations; `bullet` is the nearest single-type match (measure vs target vs
  qualitative range).

### 4. `ProportionBar` — single 0..1 fill (CSS)

- **File:** `ui/src/components/Bars.tsx:126-177`.
- **Call sites (9):** `components/CacheTable.tsx:100-106` (bytes ÷ largest,
  `tone` live when in use), `inspectors/DeploymentInspector.tsx:74-82` (launch
  load fraction, fault tone when fatal) and `:400-403` (per-node memory %),
  `sidebar/ActivitySection.tsx:54-69` (download / launch fraction, null when
  unknown), `sidebar/RosterSection.tsx:102-105` (node memory %),
  `tabs/models/NodeBoard.tsx:175-178` (allocatable ÷ ceiling),
  `tabs/storage/EstateCard.tsx:55-60` (bytes ÷ ceiling),
  `tabs/storage/FilesystemsCard.tsx:67-71` (fs used %, tone from severity).
- **Draws:** track + left-anchored fill (`right: (1-v)×100%`, clamped 0..1,
  170). `value: null` → dashed hollow track with no fill, distinct from a solid
  empty 0 (127-129, 160-161).
- **Data:** `value: number|null`, `width` (default 100%), `height` (4),
  `tone: ink|live|warn|fault|muted`, `label` (required).
- **Theming:** `--panel-recessed`, `--rule`, `--ink`, `--ink-muted`, `--live`,
  `--warn`, `--fault`. Again not `--bar-track/keyline` as the registry claims.
- **A11y:** `role="img"` + `aria-label` + `title` (154-156). Not
  `role="meter"/"progressbar"` with `aria-valuenow`.
- **Tests:** `.omp/tests/ui/components/proportionbar.check.mjs` (9 lines,
  browser + axe; states `value|null|zero|full`); `screens`.
- **Pack type:** `meter` (memory, fs, allocatable, cache size) and
  `linear-progress` (launch / download fraction).

### 5. Spend "Where requests went today" split (CSS)

- **File:** `ui/src/tabs/SpendTab.tsx:204-217` (314 lines); CSS
  `ui/src/styles/derate.css:539-554` (`.split`, 26px). Data prep
  `tabs/spend/rows.ts` (224, verified by `tabs/spend/rows.check.mjs`).
- **Draws:** two spans, local `--fill`/`--on-fill`, cloud `--warn-solid`/
  `--on-signal`, in-bar labels hidden below 10%.
- **Data:** `localPct`, `cloudPct` (0..100).
- **A11y:** `role="img"` + `aria-label="N percent local, M percent cloud"`.
  **Known contrast debt:** `cloud 42%` text is `--on-signal` on `--warn-solid`
  = 2.67:1 light (`.omp/design/DESIGN.md:102-106`).
- **Tests:** `screens` only (rows.check covers the arithmetic, not the bar).
- **Pack type:** `stacked-bar-100` (single row, 2 parts).

### 6. `PhaseBar` — prefill vs decode (CSS)

- **File:** `ui/src/inspectors/DeploymentInspector.tsx:440-472` (547 lines);
  CSS `derate.css:946-961` (`.phase`, 22px).
- **Draws:** two spans, both `--fill`, decode at `--opacity-45`; "prefill"
  label hidden below 12%; legend row with ms/s figures; prose caption.
- **Data:** `ttftMs`, `meanDurationS` → `pp` %.
- **A11y:** no role/label on the bar; the legend text carries the numbers.
- **Tests:** `screens` only.
- **Pack type:** `stacked-bar-100` (single row, 2 parts, one hue two strengths).

### 7. Routing target weight bars (CSS, inline styles)

- **File:** `ui/src/sidebar/RoutingSection.tsx:162-242` (`TargetWeight`, 242 total).
- **Draws:** per routing target a 15px track (`--panel-sunk` + inset `--edge`
  keyline when the policy is proportional, hollow `--rule` otherwise) with a
  `--fill` bar at `weight×100%`; opacity encodes mode/zero
  (`1 | --opacity-25 | --opacity-55 | 0`, 205); excluded targets dim to
  `--opacity-62` (188); `NN%` mono readout beside it.
- **Data:** `RoutingTarget.weight` (0..1), `healthy`, `admitting`, `circuit`,
  `zero_weight_reason`, policy.
- **A11y:** none on the bar; percentage is adjacent text; circuit state via `Lamp`.
- **Tests:** `screens` only. (The same visual is what the dead `.bar` CSS rule
  at `derate.css:327-344` used to style — no TSX uses `className="bar"` now.)
- **Pack type:** `bar` (horizontal, one row per target, fixed 0..100 scale).

### 8. Setup launch progress (`.setup-bar`) + phase list (CSS)

- **File:** `ui/src/tabs/SetupTab.tsx:1002-1035` (1257 lines); CSS
  `ui/src/tabs/setup/setup.css:212-251`.
- **Draws:** determinate `.is-known` (`--flow`, width transitions
  `--dur-slow --ease`) only when a real fraction exists; otherwise indeterminate
  `.is-open` sweep (`--dur-sweep --ease-inout`, gradient of `--flow`); under
  reduced motion a static 100% at 0.45 opacity — explicitly never a parked 40%.
  Below it an `<ol>` of 4 phases with check/dot/circle icons and per-phase timings.
- **Data:** `fraction: number|null`, `done`, `phase`, `reached` timestamps.
- **A11y:** bar has no role/`aria-valuenow`; the `<ol>` is text.
- **Tests:** `screens` (setup is a destination). Phase logic shared with
  `state/launchPhase.ts`.
- **Pack type:** `linear-progress` (determinate + indeterminate); the phase list
  is a stepper (closest pack id `milestone-timeline`, but it is UI, not a chart).

### 9. `ClusterGraph` — machine floor / topology diagram (hand SVG)

- **Files:** `ui/src/tabs/cluster/ClusterGraph.tsx` (1780), `layout.ts` (2957,
  pure geometry), `motion.ts` (119), `order.ts` (129), `particles.ts` (405),
  `providerLogo.ts` (67), `GraphToolbar.tsx` (48), `SelectionRail.tsx`;
  docs `tabs/cluster/README.md` (588).
- **Draws:** one `<svg viewBox="0 0 {layout.width} {layout.height}">` (618-624)
  in authored units (×4/3 GSCALE); machine plates, deployment bands spanning the
  machines they occupy with leads, provider plate, entry/exit endpoint plates,
  measured links (stroke width = measured bandwidth, dashed = never measured,
  813-815) or a bracket bar for packed links (784-801), junction dots, captions
  with knockout plates; pan/zoom (0.4–4× of fit), drag-to-place with persisted
  offsets, alt+arrow reordering.
- **Data:** `Topology` (nodes, edges with `all_reduce_gbps`/`measured`/`stale`),
  `NodeStateDTO[]`, `DeploymentDTO[]`, `RoutingConfig[]`, `Provider[]`, live
  metrics frame, request mix.
- **Theming:** all `var(--…)`: `--ink`, `--ink-muted`, `--rule`, `--panel`,
  `--fill`, `--on-fill`, `--on-fill-dim`, `--select-on-panel`, `--flow`,
  `--stream`, `--batch`. No hex literals (grep clean). Literal opacities
  (0.16, 0.18, 0.45, 0.6, 0.85, 0.92) rather than `--opacity-*`.
- **A11y:** best in the app: focusable root with a descriptive `aria-label`
  (622-623) and keyboard pan/zoom (512-579); edges, bands, machine plates are
  `role="button" tabIndex=0` with Enter/Space handlers and `<title>`
  (766-783, 1100-1120, 1552-1563); provider plate `role="group"` (934-938);
  `announce()` live-region callback.
- **Tests:** `tabs/cluster/layout.check.mjs` (2149), `motion.check.mjs` (153),
  `particles.check.mjs` (326), `loading.check.mjs` (339) — all hermetic,
  geometry/logic only; `screens` for the render.
- **Pack type:** nearest `force-network` (it is a deterministic slot layout, not
  a force simulation).

### 10. ClusterGraph embedded meters (hand SVG, inside item 9)

| Meter | Lines | Draws | Data | Pack type |
|---|---|---|---|---|
| Machine memory meter | `ClusterGraph.tsx:1617-1647` | 14-unit track `--on-fill`@0.18 + fill @0.85; null → dashed hollow track @0.45 | `mem` % (live or topology) | `meter` |
| `ShareBar` routing share | `ClusterGraph.tsx:1677-1711` | 6-unit track + fill, `NN%` text, `<title>` | `{served_name, weight}` | `meter` |
| Link bracket bar | `ClusterGraph.tsx:784-801` | `--ink`@0.16 track (dashed if unmeasured) + `--ink` fill at `b.fraction` | measured bandwidth fraction | `meter` |
| Non-contiguous band ticks | `ClusterGraph.tsx:1141-1145` | 20×2.5 ticks over member machines | band membership | — (diagram furniture) |

### 11. Particle flow layer (imperative SVG + rAF)

- **File:** `ui/src/tabs/cluster/particles.ts:249-405`; host `<g id="particles">`
  at `ClusterGraph.tsx:692`, driver `ParticleField` 1726-1780.
- **Draws:** `createElementNS('rect')` blocks walking routed paths, one per
  measured in-flight request (`var(--flow)`), out-stream slivers tinted
  `--stream`/`--batch`/`--ink-muted` (`streamTone`, 249-252). Fully suppressed
  under `prefers-reduced-motion` (355).
- **Tests:** `particles.check.mjs`. **Pack type:** `flow-map`-like animation;
  no pack equivalent.

### 12. `LoadingBand` — `BandSweep` + `BandLaunchLayer` (hand SVG)

- **File:** `ui/src/tabs/cluster/LoadingBand.tsx` (260), logic `loading.ts` (145).
- **Draws:** `BandSweep` (36-87): indeterminate sweep clipped to the band —
  `--on-fill`@0.18 track, a 40%-wide `--flow` gradient (`.bandsweep-move`) and a
  separate full-width `--flow`@0.45 still rect for reduced motion. 
  `BandLaunchLayer` (104-260): the four-phase stepper with nested `<Icon>` marks
  and a 1 s clock, one layer for all loading bands, `pointer-events:none`.
- **A11y:** state is folded into the band's own `aria-label` ("…, still
  starting", `ClusterGraph.tsx:1096-1098`).
- **Tests:** `tabs/cluster/loading.check.mjs` (339). `loading-preview.svg` beside
  it is a **0-byte file** (artefact).
- **Pack type:** `linear-progress` (indeterminate) + stepper.

### 13. KPI readouts — `Readout`, `AggregateRow` tiles

- **Files:** `ui/src/components/Readout.tsx` (56), `tabs/dashboard/AggregateRow.tsx`
  (102; 4 tiles at 22-27), also `HeadroomSub.tsx:133`, `FilesystemsCard.tsx`,
  `DeploymentsStrip.tsx:139`, `Quad` in `DeploymentInspector.tsx:431-438`.
- **Draws:** mono tabular number with reserved `ch` width, unit, tone, stale grey,
  null → em dash.
- **Theming:** `--ink/--live/--warn/--fault/--ink-muted` (registry says
  `--readout-*` component tokens exist; code uses the semantic ones).
- **Tests:** `.omp/tests/ui/components/readout.check.mjs`.
- **Pack type:** `kpi-tile`.

### Not charts (recorded so they are not re-audited)

| Item | Evidence | Why not |
|---|---|---|
| `Qr` | `tabs/setup/Qr.tsx` (41), `qr.ts` (551), `qr.check.mjs` (350) | QR symbol: data encoded as one `<path>` in `--qr-ink` (theme-invariant) on a pinned light tile; `role="img"`. An encoding, not a visualisation. |
| `Lamp` | `components/Lamp.tsx` (38) | Status dot (`role="img"`, live/warn/fault/idle, hollow = stale). Indicator, not a chart. |
| `Icon` | `components/Icon.tsx:34` | Icon sprite `<svg>`. |
| `dominant.ts` canvas | `tabs/models/dominant.ts:54` | `getContext('2d')` samples a 16px avatar for an accent colour. |
| `Terminal` | `inspectors/node/Terminal.tsx` (303) | xterm.js terminal. |
| QuantLadder skeleton | `tabs/models/QuantLadder.tsx:665-666` | Loading-skeleton blocks; QuantLadder/HeadroomSub/LoadSub are tables. |
| `Interconnect` | `inspectors/node/Interconnect.tsx` (95) | Text rows of measured GB/s; no drawing. |
| `.bar` / `.bar.free` / `.bararea` CSS | `styles/derate.css:280-288, 327-344` | `.bar*` has no TSX consumer (dead); `.bararea` is a form row in `ServePanel.tsx:296,368`. |

## Design pack: what it already says about charts

- `.omp/design/components/registry.json` lists `Readout`, `Lamp`, `SegmentBar`,
  `ProportionBar` with tests under `.omp/tests/ui/components/`. **`Chart`,
  `ChartGrid`, `ChartCell`, the Spend split, `PhaseBar`, the routing bar, the
  setup bar and every ClusterGraph meter are absent from the registry.**
- `.omp/design/AUDIT.md:426,468-470,529` lists `NodeCharts`/`ChartGrid`/
  `Chart`/`ChartCell` as unregistered components and `Chart.tsx:421` as an inline
  data SVG; `:50-57` still lists literal colours in `chartTheme.ts` that have
  since been replaced by `tokens.ts` reads (stale). Rule in force (`:628-629`):
  data drawings carry `// design-pack: data-svg`, canvas colour reads carry
  `// design-pack: computed-color` — present in `Chart.tsx:1`,
  `ClusterGraph.tsx:1`, `LoadingBand.tsx:1`, `Qr.tsx:1`, `chartTheme.ts:31`.
- `DESIGN.md:154-164` Motion: `--dur-slow` is "progress width", `--dur-dots`/
  `--dur-sweep` the two loops; reduced motion clamps to 0.001ms, and the two
  motion-only progress indicators render a static frame rather than nothing.
- `DESIGN.md:270-276` SegmentBar/ProportionBar spec names `--bar-track/keyline/
  limiting`; code does not use them (see items 3-4).
- `DESIGN.md:91-106` Contrast: `--rule`/`--rule-strong` are decorative (1.47:1 /
  1.86:1, not promised); `--flow/--stream/--batch` promised 3:1 as filled marks;
  `--warn-solid` 2.67:1 light, a dot not a text surface.

## Tokens the chart pack must reuse

All names are emitted by `.omp/design/tokens/tokens.css` (imported first in
`ui/src/main.tsx:10`). Theme selection: `:root` = light;
`@media (prefers-color-scheme: dark) :root:not([data-theme='light']):not([data-theme='high-contrast'])`,
`:root[data-theme='dark']`, `:root[data-theme='high-contrast']`,
`@media (prefers-contrast: more) :root:not([data-theme])`. Values from
`tokens.flat.json`.

### Surfaces, ink, rules

| CSS var | Light | Dark | High contrast | Chart role |
|---|---|---|---|---|
| `--panel` | `#EDE9E0` | `#191817` | `#FFFFFF` | plot background, segment separators, cursor point ring |
| `--panel-raised` | `#F7F4EE` | `#232120` | `#FFFFFF` | tooltip/popover surface |
| `--panel-recessed` | `#E3DED3` | `#141312` | `#F2F2F2` | bar track (SegmentBar, ProportionBar) |
| `--panel-sunk` | `#D6D0C2` | `#0E0D0C` | `#E6E6E6` | bar track (routing, setup bar) |
| `--ink` | `#1A1917` | `#EDE9E0` | `#000000` | the trace, fills, reference line |
| `--ink-secondary` | `#423D37` | `#C9C2B4` | `#1F1F1F` | secondary labels |
| `--ink-muted` | `#5C5851` | `#979186` | `#3A3A3A` | axis text, crosshair, ceiling line, "no reading" |
| `--rule` | `#C9C2B4` | `#423D37` | `#6B6B6B` | chart box border, track keyline, null dashed track |
| `--rule-strong` | `#B4AC9A` | `#5A544C` | `#000000` | stronger hairline |
| `--edge` | `#82796B` | `#7C746A` | `#000000` | perceivable keyline (3:1) |
| `--fill` | `#33302B` | `#E2DCD0` | `#000000` | solid bars (split, phase, routing) |
| `--on-fill` | `#EDE9E0` | `#191817` | `#FFFFFF` | label on `--fill`; meters on plates |
| `--on-fill-dim` | `#A8A398` | `#5A5750` | `#CFCFCF` | secondary text on plates |
| `--select-on-panel` | `#1A1917` | `#EDE9E0` | `#000000` | selected-mark outline |
| `--focus` / `--focus-ring` | `#1A1917` | `#EDE9E0` | `#000000` | focus ring (`--focus-ring-width` 2/2/3px) |

### State, flow, identity

| CSS var | Light | Dark | High contrast |
|---|---|---|---|
| `--live` | `#2F6E3A` | `#72C182` | `#0A5F1E` |
| `--warn` | `#7F5C14` | `#D6A64A` | `#6B4A00` |
| `--fault` | `#9A3327` | `#E58273` | `#9B1500` |
| `--info` | `#3E6194` | `#7FA3D4` | `#003E9C` |
| `--live-solid` | `#3D8B4A` | `#4E9E5C` | `#0A5F1E` |
| `--warn-solid` | `#B8862C` | `#C4922F` | `#6B4A00` |
| `--fault-solid` | `#A93A2E` | `#BC4A3B` | `#9B1500` |
| `--info-solid` | `#4A6FA5` | `#6489BC` | `#003E9C` |
| `--on-signal` | `#EDE9E0` | `#191817` | `#FFFFFF` |
| `--flow` (= `--accent`) | `#4A6FA5` | `#7FA3D4` | `#003E9C` |
| `--stream` | `#377A86` | `#79C0CE` | `#005A66` |
| `--batch` | `#7A62A9` | `#B49BDB` | `#4B1E8F` |
| `--tag-1` | `#3A51B0` | `#8CA4E2` | `#1F3FA0` |
| `--tag-2` | `#9C2E70` | `#E88AC0` | `#8A1760` |
| `--tag-3` | `#0B6A6A` | `#54C6C6` | `#005B5B` |
| `--tag-4` | `#7D42A4` | `#C298E4` | `#5B1E8E` |

### Component tokens already named for bars/readouts (unused by the bar code today)

| CSS var | Light | Dark | High contrast |
|---|---|---|---|
| `--bar-track` (= `--panel-sunk`) | `#D6D0C2` | `#0E0D0C` | `#E6E6E6` |
| `--bar-keyline` (= `--edge`) | `#82796B` | `#7C746A` | `#000000` |
| `--bar-limiting` (= `--warn-solid`) | `#B8862C` | `#C4922F` | `#6B4A00` |
| `--readout-ink/live/warn/fault/muted/stale` | = `--ink/--live/--warn/--fault/--ink-muted/--ink-muted` | | |
| `--lamp-live/warn/fault/idle/ring` | = `*-solid` / `--ink-muted` / `--edge` | | |
| `--tooltip-bg` / `--tooltip-fg` | `#33302B` / `#EDE9E0` | `#E2DCD0` / `#191817` | `#000000` / `#FFFFFF` |
| `--skeleton-bg` / `--skeleton-shimmer` | `#D6D0C2` / `#E3DED3` | `#0E0D0C` / `#141312` | `#E6E6E6` / `#F2F2F2` |

### Chart grid / axis / band — NOT CSS variables

`tokens.json:362-418` has three primitives described as chart colours, **light
only, not emitted to CSS** (DESIGN.md:44: only `space.*` and `opacity.*`
primitives are emitted):

| Primitive | Value | Described as | Actually used by `chartTheme.ts` as |
|---|---|---|---|
| `color.alpha.ink-13` | `rgba(26, 25, 23, 0.13)` | "chart grid, light" | `band` fill = `alpha(--ink, 0.13)` |
| `color.alpha.ink-30` | `rgba(26, 25, 23, 0.30)` | "chart axis, light" | `bandEdge` = `alpha(--ink, 0.30)` |
| `color.alpha.amber-12` | `rgba(127, 92, 20, 0.12)` | "chart warn band, light" | `gap` fill = `alpha(--warn, 0.12)` |

Runtime-derived values per theme (what users see today): band
`rgba(26,25,23,.13)` / `rgba(237,233,224,.13)` / `rgba(0,0,0,.13)`; band edge
same at `.30`; gap `rgba(127,92,20,.12)` / `rgba(214,166,74,.12)` /
`rgba(107,74,0,.12)` (light / dark / HC). The pack should add semantic,
three-theme tokens (e.g. `--chart-band`, `--chart-band-edge`, `--chart-gap`,
and `--chart-grid`/`--chart-axis` if it draws gridlines) via
`tokens.json` + `npm run tokens:build`, rather than re-deriving alphas — or
keep deriving with `color-mix(in srgb, var(--ink) 13%, transparent)` for SVG
and a computed-style read for canvas, as `chartTheme.ts` does.

### Opacity, type, shape, motion

- Opacity: `--opacity-25` .25, `--opacity-38` .38, `--opacity-45` .45,
  `--opacity-55` .55, `--opacity-60` .6, `--opacity-62` .62, `--opacity-72` .72,
  `--opacity-85` .85; aliases `--opacity-faint` .45, `--opacity-muted` .62,
  `--opacity-dim` .72.
- Fonts: `--font-sans` = `'IBM Plex Sans', ui-sans-serif, system-ui, sans-serif`;
  `--font-mono` = `'IBM Plex Mono', ui-monospace, 'SF Mono', monospace`
  (numbers are always mono + `tabular-nums`).
- Sizes used by chart chrome: `--size-unit` 12px (title/axis), `--size-body`
  15px (value), `--size-micro` 11px, `--size-mono-unit` 11px,
  `--size-mono-label` 12px, `--size-badge` 10px.
- Shape: `--radius-sm` 2px (bars), `--radius` 3px (chart box), `--radius-full`
  50%; `--z-base` 0, `--z-sticky` 1.
- Durations: `--dur-fast` 120ms, `--dur-mid` 160ms, `--dur` 180ms,
  `--dur-slow` 600ms (progress width), `--dur-dots` 1400ms, `--dur-sweep` 1600ms.
- Easing: `--ease` `cubic-bezier(0.2, 0.7, 0.3, 1)`, `--ease-default` `ease`,
  `--ease-inout` `ease-in-out`.

### No categorical data-viz palette

There is none, by policy: segments are one hue at descending opacity
(`Bars.tsx:119-124`), a band is one hue at two strengths (`Chart.tsx:86-90`,
`chartTheme.ts:10-14`), colour is reserved for state (`--live/warn/fault`) and
flow (`--flow/stream/batch`). `--tag-1..4` (chat-turn identity) and
`--avatar-1..10` (publisher identity, theme-invariant `hsl()`, "identity, never
status") exist but neither is sanctioned for series. A pack chart that needs N
categorical series (grouped/stacked by category, multi-line, pie) needs a new
contrast-checked `--series-*` ramp added to `tokens.json` + `pairs.json` — a
design decision, not something to invent in the pack.

## Migration plan

| # | Item | Verdict | Pack type + options | Must preserve | Risk |
|---|---|---|---|---|---|
| 1 | `Chart` (uPlot) | **migrate, gated on parity** | `line` `{renderer:'canvas', axes:false, legend:false, curve:'linear', spanGaps:false, band:{upper,lower}, annotations:{gaps}, yDomain:fixed, crosshair:'x', syncGroup}` inside `small-multiples` for `ChartGrid` | **Null is a gap** — never interpolate across null/NaN/Infinity, band breaks with the lines (`chart.ts:11-15`, `chart.check.mjs:71-114`; re-point the check at the pack's data adapter and add a render-level assertion on the drawn path). Strictly ascending, de-duplicated x. Fixed y rules: percent 0..100, rate from 0, span floor 10 + 8% pad, chosen over both band edges. **Synced x-crosshair across a `ChartGrid`**, independent between grids, `ServingBlock`'s bare `.chartgrid` deliberately unsynced (or fix). Hover readout = value at crosshair, `value → band`, `missing · {reason}` inside a gap, `no sample` at a null; axis line `min · note|clock · max` with `avg`/`peak` labels. Gap spans drawn *under* the trace. "no samples yet" for empty/all-null. **≥50k points/series at 24h** with instance reuse: no rebuild on data/size/hover, content-keyed memo, wait for width>0 (hidden tabs). Theme switch (media query + `data-theme`) without reload. `role="img"` label (improve: keyboard crosshair + table fallback). | **High.** 24 call sites, perf at archive scale, the null contract currently pinned only at `align`; SVG at 50k points will not hold — canvas path is mandatory. |
| 2 | `ChartCell` | **migrate** | `sparkline` `{renderer:'svg', width:'100%', height:24, stroke:'var(--ink)', nonScalingStroke:true, spanGaps:false}` | Pen lifts on null; no canvas, no ResizeObserver, no per-row subtree beyond one `<path>` (drawn once per deployment row); "no samples yet" under 2 points; `role="img"` + label; self-scaled y. | Low. |
| 3 | `SegmentBar` | **migrate** | `stacked-bar` `{orientation:'horizontal', rows:1, palette:'mono-opacity'(0.85→0.30), domainMax:max(total,usable,ceiling)×1.02, refLines:[{at:usable, weight:2, overhang:'--space-4', solid}, {at:ceiling, weight:1, dashed, muted, suppressWithin:0.01}], overflow:{after:usable, color:'--fault'}, emphasis:{limiting:'outline 1.5px'}}` — or `bullet` if the pack's bullet supports multi-segment measure | Monochrome segments (colour = state only); fault only past `usable` and only when the total overruns; `usable:null` → no line, no fault; limiting term outlined not recoloured; ceiling suppressed ≤ usable or within 1%. Add the missing `role="img"`/label (it has none). Reconcile with the `--bar-*` tokens the registry claims. | Medium — single call site, but `segmentbar.check.mjs` + registry states must move with it. |
| 4 | `ProportionBar` (9 sites) | **migrate** | `meter` `{value, min:0, max:1, tone, width, height}`; `linear-progress` for the 3 load/download fraction sites (`DeploymentInspector:74`, `ActivitySection:54`) | `null` = dashed hollow track, never pixel-identical to 0; clamp 0..1; tones ink/live/warn/fault/muted; numeric width (70/80px) and height (4/5/6); required label. Upgrade to `role="meter"`/`"progressbar"` + `aria-valuenow` without losing the null≠0 announcement. | Medium — 9 call sites, high visibility, `proportionbar.check.mjs`. |
| 5 | Spend split | **migrate** | `stacked-bar-100` `{rows:1, height:26, parts:[local:'--fill', cloud:'--warn-solid'], inlineLabels:{minShare:0.10}}` | aria-label sentence; labels hidden <10%; fix the 2.67:1 `cloud` label contrast (DESIGN.md:102-106) — a visible change to decide, not to waive. | Low. |
| 6 | `PhaseBar` | **migrate** | `stacked-bar-100` `{rows:1, height:22, parts:[prefill:'--fill', decode:'--fill'@--opacity-45], inlineLabels:{minShare:0.12}}` | One hue two strengths; legend row + caption stay outside; add role/label. | Low. |
| 7 | Routing weight bar | **migrate** | `bar` `{orientation:'horizontal', domain:[0,1], valueLabel:'right mono', mode:{proportional:'track --panel-sunk + --edge keyline', other:'hollow --rule'}}` | Opacity semantics (proportional: 1 / `--opacity-25` at zero; non-proportional: `--opacity-55` / hidden at zero); excluded-row dim `--opacity-62`; no new hue for circuit state. Add a label. Delete dead `.bar` CSS at cutover. | Low. |
| 8 | Setup progress bar | **migrate** | `linear-progress` `{value:number|null → indeterminate, color:'--flow', track:'--panel-sunk', transition:'--dur-slow --ease', sweep:'--dur-sweep --ease-inout'}` | Indeterminate never shows a position; reduced motion = static full-width @0.45, not a parked 40%; determinate only when measured. Add `role="progressbar"` (omit `aria-valuenow` when indeterminate). Phase `<ol>` stays as is. | Low. |
| 9 | `ClusterGraph` | **keep** — bespoke deterministic topology diagram; 2957-line `layout.ts` pinned by a 2149-line check, drag/pan/zoom/keyboard model, authored-unit coordinate system; `force-network` would be a different (worse) product | — | — | Replacing it is high risk for no gain. |
| 10 | ClusterGraph meters | **keep** — drawn in the plate's authored SVG units, `--on-fill` on `--fill`; a pack `meter` would only be usable if it can emit bare SVG `<rect>`s at given coordinates (`renderInto(g, x,y,w,h)`); adopt then, optional | `meter` (svg fragment mode) | null = dashed hollow track; one colour two opacities | Low if adopted later. |
| 11 | Particle layer | **keep** — imperative rAF animation of measured in-flight requests; not a chart | — | reduced-motion off switch | — |
| 12 | `LoadingBand` | **keep** — lives in the ClusterGraph SVG; could consume the pack's indeterminate `linear-progress` geometry/keyframes if exposed as SVG | `linear-progress` (indeterminate, svg fragment) | still-frame vs moving-frame are different shapes; never implies a position | Low. |
| 13 | `Readout` / `AggregateRow` tiles | **keep** — `Readout` is the typographic primitive (reserved `ch` width, tabular mono, stale grey, null em dash) used far beyond charts; a pack `kpi-tile` should compose it, not replace it | `kpi-tile` (wraps `Readout`) | zero-reflow width contract | — |
| — | `Qr`, `Lamp`, `Icon`, `dominant.ts`, `Terminal`, skeletons, `Interconnect`, `.bararea` | **not a chart** | — | — | — |
| — | `.bar` CSS (`derate.css:327-344`), `loading-preview.svg` (0 bytes) | **not a chart** — dead weight; remove at cutover | — | — | — |

### Cross-cutting requirements for the pack (derived from the above)

1. A **null-is-a-gap** data contract at the adapter boundary, tested in node like
   `chart.check.mjs`, plus a render assertion — the default must be
   `spanGaps:false` and it must be written down, not inherited.
2. **Canvas renderer** for `line` at ≥50k points with imperative data/size
   updates; SVG for sparklines and bars.
3. **Cursor sync groups** across small multiples (x only).
4. **Theme by CSS variables**: SVG paints `var(--…)` directly; canvas reads
   computed style and re-reads on `prefers-color-scheme` change and `data-theme`
   mutation (`chartTheme.ts:77-90` is the reference).
5. **Monochrome by default**; colour only from state/flow tokens; no categorical
   palette until one is added to `tokens.json`.
6. **null ≠ 0** in every bar/meter (dashed hollow track).
7. **Reduced motion**: progress that exists only as motion renders a static
   frame, not nothing.
8. Every mark labelled: no chart ships without `role` + accessible name; the
   current gaps (SegmentBar, PhaseBar, routing bar, setup bar, uPlot keyboard
   access) are to be closed by the migration, not carried over.
9. Register `Chart`/`ChartGrid`/`ChartCell` (or their pack replacements) in
   `.omp/design/components/registry.json` and refresh the stale
   `.omp/design/AUDIT.md:50-57` rows.
