# derate chart pack

Every major chart type behind one API, each with a typed input schema, premade
samples (basic, realistic, and edge cases), tests, an icon and a README. Rendering is
TypeScript + D3 to SVG, switching the marks layer to a HiDPI canvas above 5,000 marks,
themed entirely by the design pack's tokens (light, dark, high contrast). No network at
runtime: fonts and map geometry are bundled.

**Rules for changing anything: [`RULES.md`](RULES.md).** Authoring contract:
[`core/CHART_SPEC.md`](core/CHART_SPEC.md). Live list: [`registry.json`](registry.json).
Look at everything: open [`gallery.html`](gallery.html) from disk.

Sample JSON is stored compactly (one line per file) to keep review diffs small;
the full datasets and edge cases remain available to builds and checks. Generated
code/design graphs are local-only; rebuild them with `cd .omp && npm run graph:update`.

## Quick use

```ts
import { chart, chartFor, recommend, listCharts, getSample, getDataset } from '.omp/charts'

const h = chart('line', el, rows, { fields: { x: 'month', y: 'revenue' }, valueFormat: 'currency' })
h.update(newRows)            // transitions; options merge
h.resize()                   // also automatic (ResizeObserver)
h.exportSVG(); await h.exportPNG(2); h.exportCSV()
h.destroy()                  // no DOM, listeners or observers left

const { handle, typeUsed, reason } = chartFor('trend', rows, el)   // let the chooser pick
recommend('composition', rows)  // [{ type: 'donut', score: 88, reason: '4 parts of total…', options, data }]
listCharts({ category: 'timeline' }); listCharts({ intent: 'distribution' })
getSample('gantt', 'realistic'); getDataset('sales-by-month')      // after dist/charts-data.js
```

In a page without a bundler: `<script src="dist/charts.iife.js">` (then `<script src="dist/charts-data.js">`
for samples/datasets) and use `window.OmpCharts`.

From the agent: `/chart <type|intent> [data]`, `/chart-for <intent> <data>`, `/chart-new <id>`,
`/chart-data add <name> <file>`, `/chart-sample add <type> <file>`, `/chart-check [type]`,
`/chart-gallery`; tools `chart_recommend`, `chart_lookup`, `dataset_lookup`,
`schema_validate`; agent `chart-smith`. All of them run `scripts/cli.mjs`.

## Input

Rows-shaped charts accept an array of objects, CSV/TSV text, column arrays
(`{ x: [...], y: [...] }`) or arrays-of-arrays with a header row, and map any column names
onto the chart's fields with `fields: { x: 'date', y: 'revenue', series: 'region' }`.
Numbers in strings (`"1,234.5"`) coerce; dates accept `Date`, epoch s/ms, ISO 8601 (with
offsets), and common written forms, interpreted in `timeZone` when they carry none.
Errors are sentences: `field 'value' must be a number, got 'abc' at row 12`, shown in the
chart's error state and returned by `validate()`. Trees, graphs, sets and KPI objects
have their own shapes (see each README); trees also accept `{id, parent}` or path rows,
graphs accept link rows.

## Every chart has

Responsive sizing, HiDPI, colour only from tokens (categorical palette gated for 3:1
contrast and colour-vision separation; sequential and diverging ramps), optional pattern
fills, smart axes (nice ticks, rotated/thinned/truncated labels with full text on hover,
locale numbers/dates/currency, SI suffixes), legend with toggles, tooltips, hover
highlight, click callbacks, zoom/pan/brush on time and scatter charts, enter/update/exit
transitions (off under reduced motion), empty and error states, a generated aria-label
summary, keyboard navigation across marks with a focus ring and announcements, a hidden
data table, and SVG/PNG/CSV export.

## Categories

| Category | Types |
|---|---|
| comparison | bar, horizontal-bar, grouped-bar, stacked-bar, stacked-bar-100, diverging-bar, lollipop, dot-plot, dumbbell, bullet, radial-bar, pareto, waterfall |
| part-to-whole | pie, donut, semi-donut, waffle, treemap, sunburst, icicle, marimekko, funnel, pyramid, nested-proportions |
| trend | line, multi-line, step-line, area, stacked-area, stacked-area-100, streamgraph, range-area, sparkline, slope, bump, horizon, candlestick, ohlc, calendar-heatmap, cycle-plot |
| timeline | gantt, event-timeline, swimlane-timeline, roadmap, milestone-timeline, burndown, burnup, cumulative-flow, control-chart |
| distribution | histogram, box-plot, violin, density, ridgeline, beeswarm, strip, ecdf, qq-plot, population-pyramid |
| relationship | scatter, bubble, connected-scatter, regression-scatter, hexbin, contour-density, correlation-matrix, splom, parallel-coordinates, radar, quadrant |
| flow-hierarchy | sankey, alluvial, chord, arc-diagram, force-network, tree, radial-tree, dendrogram, circle-packing, org-chart |
| matrix | heatmap, adjacency-matrix, punch-card, table-heatmap |
| geo | choropleth, bubble-map, dot-map, hex-tile-map, flow-map |
| kpi | gauge, progress-ring, linear-progress, kpi-tile, meter, small-multiples |
| sets | word-cloud, venn, euler, upset |

Intents the chooser understands: compare, rank, composition, trend, timeline, schedule
(project), distribution, relationship, correlation, flow, hierarchy, network, geo, kpi
(progress), change-over-time, deviation, sets.

## Layout

```
index.ts          the API (chart, chartFor, recommend, listCharts, getSample, getDataset, validate)
core/             engine: defineChart, cartesian axes, scene (SVG join / canvas), theme, format,
                  text, validate, fields, export, dom (legend/tooltip/table/states), stats, describe
adapters/         CSV, rows/columns, field mapping, pivot/unpivot, aggregate, bin, stack, tree, graph
time/             parsing, time zones, granularity floor/offset/range/bucket, fiscal, gaps, rolling
chooser/          inspect(data) + recommend(intent, data)
theme/            tokens.json (W3C; aliases the design pack) -> chart-tokens.css + tokens.generated.ts; fonts/
types/<id>/       index.ts, schema.ts, samples/, <id>.test.ts, README.md   (types/_shared: per-family helpers)
icons/            svg/<id>.svg, icons.json, icons.svg (sprite), lint.mjs
datasets/         shared datasets + datasets.json + validate.mjs
geo/              bundled TopoJSON (world 110m, US states 10m) + lookup tables
tests/            harness.ts (every chart's suite), unit/, browser/ (visual, layout, a11y, perf), perf-report.md
scripts/          build, check, registry, gallery, edges, cli, scaffold
```

## Scripts

```
npm install                    # once; Playwright's Chromium: npx playwright-core install chromium-headless-shell
npm run charts:build           # types/all.ts + dist/ (esm, iife, data)
npm run charts:test            # vitest: core, adapters, time, chooser, datasets, every chart
npm run charts:visual          # screenshots vs baselines + layout checks (tests/visual/baseline is local, like the design pack's screens)
npm run charts:a11y            # axe-core + keyboard + aria-label
npm run charts:perf            # first-render budgets -> tests/perf-report.md
npm run charts:check           # all of the above + typecheck + icons + datasets + registry + gallery
npm run charts:check -- --type gantt,pie     # scoped
npm run charts:gallery         # gallery.html
npm run theme:build            # after editing theme/tokens.json
node scripts/edges.mjs <id>    # derive edge-case samples from basic.json (never overwrites)
node tests/browser/shot.mjs <id> --widths 320,768,1280 --themes light,dark,high-contrast --hover
```
