# Streamgraph (`streamgraph`)

Series flowing around a centre line over time (wiggle offset, inside-out order); the shape shows rise and fall, not exact values.

## When to use
- Many series whose ebb and flow matters more than exact values: listening by genre, topics over time.

## When not to use
- Reading values or comparing totals: `stacked-area` (there is no y scale here; tooltips carry the numbers).
- Few series or short spans: `stacked-area`.

## Input
Long rows of `{ x, series, y }` with non-negative `y`. `x` is a date (Date, ISO 8601, epoch, written forms like `Mar 2026` or `2026-Q1`) or a plain number. Rows are sorted by x, so order does not matter, and instants written with different offsets compare correctly. A missing (x, series) pair counts as zero.
```json
[{ "x": "2026-01-01", "series": "North", "y": 42000 }, { "x": "2026-01-01", "series": "South", "y": 30000 }]
```
Also accepted: CSV text, column arrays (`{ "x": [...], "y": [...] }`) and any column names through a field mapping (`fields: { x: "date", y: "revenue" }`).

## Options
| Option | Type | Default | |
|---|---|---|---|
| `curve` | `linear \| monotone` | `monotone` | |
| `gaps` | `gap \| zero \| interpolate` | `'zero'` | missing values: lift the pen, draw 0, or join the neighbours (ends stay gaps) |
| `xType` | `auto \| time \| number` | `auto` | numbers stay numeric unless they look like epoch ms |
| `points` | `auto \| always \| never` | `'never'` | dots on each value (`auto`: while sparse); hover and keyboard work either way |
| `xTitle`, `yTitle` | string | — | axis titles |

Every chart also takes the base options (`valueFormat`, `locale`, `timeZone`, `zoom`, `brush`, `onBrush`, ... — see `core/fields.ts`). `zoom: true` enables wheel/drag zoom on x (double-click resets); `brush: true` reports the selected x range (Dates) through `onBrush`.

## Example
```ts
import { chart } from '.omp/charts'
chart('streamgraph', el, csv, { fields: { x: 'week', series: 'genre', y: 'plays' } })
```

## Edge cases
Empty and all-zero → empty state ("All values are zero"). Single row → one point. Negatives → drawn as zero + note. Nulls → zero by default (`gaps`) + note. Duplicate (x, series) → summed + note. 5,200 points → canvas. Unsorted / mixed-tz dates → sorted by instant. Long / RTL series names → ellipsised legend items, full text in tooltips.
