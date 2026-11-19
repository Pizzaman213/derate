# Grouped bar chart (`grouped-bar`)

Per category, one bar per series side by side, for comparing series within and across categories.

## When to use
- A few series (2–5) measured across categories: regions per quarter, platforms per browser.
- When both the per-series values and the comparison within a category matter.

## When not to use
- Totals matter more than parts: `stacked-bar`. Mix/shares: `stacked-bar-100`.
- Many series (> 5) or many categories: `heatmap` or small multiples.
- One series: `bar` / `horizontal-bar`.

## Input
Rows of `{ label, series, value }` (long format). `value` may be `null` (left out, noted).
```json
[{ "label": "Q1", "series": "North", "value": 120 }, { "label": "Q1", "series": "South", "value": 95 }]
```
Also accepted: CSV text (`"label,series,value\nQ1,North,120"`), column arrays
(`{ "label": [...], "series": [...], "value": [...] }`), and any column names through a field
mapping: `{ fields: { label: 'month', series: 'region', value: 'sales' } }`.
Duplicate (label, series) pairs are summed and noted under the chart.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `orientation` | `vertical \| horizontal` | `vertical` | bars stand up, or run left to right with categories on y |
| `showValues` | boolean | `false` | value at the end of each bar (dropped when bars are too thin) |
| `groupPadding` | number 0–0.8 | `0.2` | gap between groups as a share of the step |
| `xTitle`, `yTitle` | string | — | axis titles |

Every chart also takes the base options (`valueFormat`, `locale`, `currency`, `theme`,
`patterns`, `title`, `onClick`, ... — see `core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('grouped-bar', el, rows, { fields: { label: 'browser', series: 'platform', value: 'share_pct' }, orientation: 'horizontal' })
```

## Edge cases
Empty → empty state. Legend toggle hides a series and the remaining bars regroup to fill the band. Zeros → 1px stubs.
Negatives hang below zero. Nulls → gaps + note. Duplicates → summed + note. 5,200 bars → canvas.
Long/RTL labels rotate and truncate (vertical) or truncate horizontally, full text on hover.
