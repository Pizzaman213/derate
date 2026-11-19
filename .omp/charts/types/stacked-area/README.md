# Stacked area chart (`stacked-area`)

Series stacked on each other over time, so the top edge is the total.

## When to use
- How a total and its parts change together (traffic by source, revenue by region).
- The legend toggles a series and the rest restack.

## When not to use
- Comparing parts to each other precisely (only the bottom layer has a flat baseline): `multi-line`.
- Shares rather than amounts: `stacked-area-100`. Negative values: `multi-line`.

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
chart('stacked-area', el, { week, source, visits }, { fields: { x: 'week', series: 'source', y: 'visits' } })
```

## Edge cases
Empty and all-zero → empty state ("All values are zero"). Single row → one point. Negatives → drawn as zero + note. Nulls → zero by default (`gaps`) + note. Duplicate (x, series) → summed + note. 5,200 points → canvas. Unsorted / mixed-tz dates → sorted by instant. Long / RTL series names → ellipsised legend items, full text in tooltips.
