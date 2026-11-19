# 100% stacked area chart (`stacked-area-100`)

Each series' share of the total over time; every x adds up to 100%.

## When to use
- How the mix changes: market share, traffic mix, energy mix.

## When not to use
- The total matters too: `stacked-area`. One moment in time: `pie` or `stacked-bar-100`.

## Input
Long rows of `{ x, series, y }` with non-negative `y`. `x` is a date (Date, ISO 8601, epoch, written forms like `Mar 2026` or `2026-Q1`) or a plain number. Rows are sorted by x, so order does not matter, and instants written with different offsets compare correctly. A missing (x, series) pair counts as zero.
```json
[{ "x": "2026-01-01", "series": "North", "y": 42000 }, { "x": "2026-01-01", "series": "South", "y": 30000 }]
```
Also accepted: CSV text, column arrays (`{ "x": [...], "y": [...] }`) and any column names through a field mapping (`fields: { x: "date", y: "revenue" }`).

## Options
| Option | Type | Default | |
|---|---|---|---|
| `curve` | `linear \| monotone` | `linear` | |
| `gaps` | `gap \| zero \| interpolate` | `'zero'` | missing values: lift the pen, draw 0, or join the neighbours (ends stay gaps) |
| `xType` | `auto \| time \| number` | `auto` | numbers stay numeric unless they look like epoch ms |
| `points` | `auto \| always \| never` | `'never'` | dots on each value (`auto`: while sparse); hover and keyboard work either way |
| `xTitle`, `yTitle` | string | — | axis titles |

Every chart also takes the base options (`valueFormat`, `locale`, `timeZone`, `zoom`, `brush`, `onBrush`, ... — see `core/fields.ts`). `zoom: true` enables wheel/drag zoom on x (double-click resets); `brush: true` reports the selected x range (Dates) through `onBrush`.

## Example
```ts
import { chart } from '.omp/charts'
chart('stacked-area-100', el, csv, { fields: { x: 'month', series: 'browser', y: 'share' } })
```

## Edge cases
Empty and all-zero → empty state ("All values are zero"). Single row → one point. Negatives → drawn as zero + note. Nulls → zero by default (`gaps`) + note. Duplicate (x, series) → summed + note. 5,200 points → canvas. Unsorted / mixed-tz dates → sorted by instant. Long / RTL series names → ellipsised legend items, full text in tooltips. An x whose total is zero draws no thickness there.
