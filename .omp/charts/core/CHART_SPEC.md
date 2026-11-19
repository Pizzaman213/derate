# Writing a chart type

The contract every folder in `types/` is written to. `types/grouped-bar/` is the reference
implementation: read it first, copy its shape.

## The folder

```
types/<id>/
  index.ts          defineChart({...}); default export AND a named export
  schema.ts         zod `input` + `options` (and their output types)
  samples/          basic.json, realistic.json, edge-*.json  -- { description, data, options? }
  <id>.test.ts      chartSuite({...}) + this chart's own math/geometry tests
  README.md         when to use, when NOT to use, input, options, example
icons/svg/<id>.svg  the chart's icon (see icons/README.md for the spec)
```

`npm run charts:build` regenerates `types/all.ts` (one import per folder), so a new
folder is picked up by the API, the registry, the gallery and the browser suites.

## index.ts

```ts
export const lollipop = defineChart<Row[], Options>({
  id, name, category, intents, summary,      // registry metadata; intents from core/types INTENTS
  input, options, shape: 'rows' | 'object',  // schema.ts; rows => CSV/column arrays/field mapping for free
  fields, dateFields, numberFields,          // canonical field names (rows shape)
  adapt?(raw, opts),                         // raw -> canonical before validation (trees from flat rows...)
  isEmpty?(data, opts) => boolean | string,  // string = the empty-state message ("All values are zero")
  draw(ctx) => { marks, legend?, note?, plot?, clip?, invertX? },
  describe({ data, options, fmt, name }) => string,   // the aria-label; see "Summaries"
  table(data, options, fmt) => { columns, rows },     // hidden table + exportCSV; raw numbers, not strings
  zoom?: 'x' | 'xy',                          // time and scatter charts: enables options.zoom/brush
  aspect?, minHeight?, maxHeight?,
})
export default lollipop
```

`draw` is pure: data in, marks out. Never touch the DOM, `document`, `window`, timers
or `Math.random` (use `ctx.random`, seeded). Never hard-code a colour, font or size:
paints come from `ctx.theme` (`cat(i)`, `seq(t)`, `div(t)`, `mix(a,b,t)`, `ink`, `muted`,
`grid`, `axis`, `track`, `positive`, `negative`, `warning`, `neutral`, `highlight`,
`surface`), sizes from `SIZES` (core/theme). `ctx.measure`/`ctx.truncate` measure text.

### Marks

`rect | circle | path | line | text` (core/types.ts). The fields that matter:

- `key` -- stable across updates (use the category/series/id). It drives the
  enter/update/exit transitions; index-based keys make every update a full redraw.
- `datum` -- set it on every **data mark** (a bar, a slice, a point, a task). Data marks
  are counted (`handle.markCount()`), focusable with the arrow keys, hoverable and
  clickable. Decorations (gridlines, labels, connectors) have no `datum`.
- `label` -- the sentence announced on keyboard focus and used as the default tooltip:
  `"Bravo: 340"`, `"Design (Mar 3 – Mar 14): 60% done"`. Every data mark needs one.
- `tooltip` -- `{ title, rows: [[k, v]], color }` for a richer tooltip.
- `role` -- what it is (`bar`, `arc`, `point`, `task`, `cell`, `node`, `link`...), exported
  as `data-role` and used by tests to count marks.
- `series` -- legend key: legend hover dims other series, legend toggle hides it (core
  drops data marks of hidden series; charts that re-layout -- stacks, pies -- must also
  read `ctx.hidden` themselves).
- `layer` -- `back | grid | marks | labels | axes | front`. Above 5,000 data marks
  (`options.canvasThreshold`) core paints `back`, `grid` and `marks` on a canvas; put
  every data mark in `marks` (the default) and text in `labels`.
- `transform` -- only `translate(x,y)` is understood by the focus ring/tooltip anchor
  (radial charts: `translate(cx,cy)` and geometry relative to the centre). For
  `path` marks set `cx/cy` (anchor) and `box` (focus ring) -- arcs: use the centroid.
- `pattern` -- index 0-7; applied when `options.patterns` is on. Give it to filled
  categorical marks so charts survive greyscale print.
- `full` on text marks -- the untruncated text, shown in a tooltip on hover.
- `halo` on text -- surface-coloured outline so labels read on top of marks.

### Axes

Use `ctx.cartesian({ x, y, xTitle, yTitle, grid, pad, zoom })` for anything with x/y
axes. It measures tick labels, rotates/thins/truncates them so they never overlap or
leave the box, sizes the margins, and returns `{ x, y, plot, marks, px, py, bw, bh,
invertX, invertY }`. Spread `f.marks` into your marks. Scale specs: `band`, `point`,
`linear` (zero-based unless `zero: false`), `log`, `symlog`, `time` (time-zone aware
ticks in `options.timeZone`). `options.xDomain` / `options.yDomain` override domains
(small multiples share scales this way).

Charts without axes (pies, trees, maps...) compute their own layout inside
`ctx.width x ctx.height` and must keep every mark and label inside it: truncate with
`ctx.truncate`, or drop labels that do not fit (tooltips still carry the value).

### Legend

Return `legend: { items: [{ key, label, color, shape }] }` when there is more than one
series; `key` must equal the marks' `series`. Continuous colour: `legend: { gradient:
{ stops: theme.seqStops(), min, max } }`.

### Summaries (`describe`)

`"<Name>, <what>, <range>, <notable>"`, generated from the data, e.g.
`"Line chart, revenue by month, Jan 2026 to Dec 2026, peak 48k in Nov"`. Helpers in
`core/describe.ts` (`phrase.count`, `phrase.extremes`, `phrase.dateSpan`,
`phrase.listPhrase`). Must never contain `undefined`, `NaN` or `[object`.

### Edge cases (all must render; none may throw)

| Sample | Expected behaviour |
|---|---|
| `edge-empty` | `isEmpty` -> empty state (core draws it) |
| `edge-single` | one mark, sensible axes (no zero-width domain) |
| `edge-zeros` | draws, or empty state with a message ("All values are zero") where a zero total is meaningless (pie, treemap) |
| `edge-negatives` | handled per chart: drawn below zero, or omitted with a `note` ("2 negative values omitted") where negative is meaningless (pie, treemap, funnel) |
| `edge-nulls` | gaps/skips, with a `note` when something was left out |
| `edge-duplicates` | summed / kept as distinct marks -- whichever is right, and noted |
| `edge-large` | >5,000 data marks where the chart can have them (canvas path); otherwise "large" means many categories (collapse the tail into "Other" with a note) |
| `edge-long-labels` | truncated with full text in the tooltip; nothing overflows |
| `edge-unicode-rtl` | Arabic/Hebrew/CJK/emoji labels render |
| `edge-outliers` | scale still readable (log option, clamp, or just a tall domain) |
| `edge-unsorted-dates`, `edge-mixed-tz` | time charts sort; instants compare correctly |

`node scripts/edges.mjs <id>` derives these from `basic.json` for rows-shaped inputs and
never overwrites; hand-write them for object-shaped inputs (trees, graphs, KPI objects).
Keep `edge-large` under ~120 KB (it is stored as column arrays).

## The test

```ts
chartSuite({
  module: lollipop, dir: import.meta.dirname, role: 'point',
  marks: { basic: 5, realistic: 12, 'edge-large': 5200 },     // expected data-mark counts
  summary: /^Lollipop chart, 5 items, highest .../,           // basic.json's aria-label
  malformed: [{ name: 'text value', data: [...], message: /field 'value' must be a number, got 'abc' at row 2/ }],
})
describe('lollipop: geometry', () => { /* proportionality, sums, ordering ... */ })
```

`chartSuite` checks every sample (validates, renders, summary, table/CSV agreement,
standalone SVG export, keyboard announcements), malformed inputs (human-readable
messages, error state), and the lifecycle (update with/without transitions, resize,
destroy with zero leaked listeners/observers). Add the chart's own math on top: sums,
percentages totalling 100, bin counts, proportional areas, running totals.

Run: `npx vitest run types/<id>` and `npx tsc -p tsconfig.json`.
Look at it: `node tests/browser/shot.mjs <id>` writes PNGs of every sample to
`tests/.build/shots/<id>/` -- open them. Layout bugs are invisible to jsdom.

## README.md

```md
# <Name>
<one line>
## When to use / ## When not to use (and what to use instead, by id)
## Input (canonical shape, a JSON example, accepted alternates: CSV, column arrays, field mapping)
## Options (table: option, type, default, description -- chart-specific ones; base options link to core)
## Example (chart('<id>', el, data, {...}))
## Edge cases (what each edge sample does)
```
