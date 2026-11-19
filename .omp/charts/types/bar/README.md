# Bar chart (`bar`)

Vertical bars comparing one value across categories, from a zero baseline.

## When to use
- Comparing a single measure across a handful to a few dozen categories.
- Ranking (use `sort: 'descending'`).

## When not to use
- Many categories or long names: use `horizontal-bar` (labels read horizontally).
- Several series per category: `grouped-bar` or `stacked-bar`.
- Values over continuous time: `line` or `area`.
- Parts of a whole: `stacked-bar-100`, `pie` (≤ 6 parts) or `treemap`.

## Input
Rows of `{ label, value }`. `value` may be `null` (left blank, noted).
```json
[{ "label": "Alpha", "value": 120 }, { "label": "Bravo", "value": 340 }]
```
Also accepted: CSV text (`"label,value\nAlpha,120"`), column arrays (`{ "label": [...], "value": [...] }`),
and any column names through a field mapping: `{ fields: { label: 'plan', value: 'mrr_usd' } }`.
Duplicate labels are summed and noted under the chart.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `sort` | `none \| ascending \| descending` | `none` | bar order |
| `showValues` | boolean | `false` | value above each bar (dropped when bars are too narrow) |
| `colorBy` | `single \| category` | `single` | one colour, or one per category (pattern fills follow) |
| `xTitle`, `yTitle` | string | — | axis titles |

Every chart also takes the base options (`valueFormat`, `locale`, `currency`, `theme`,
`patterns`, `title`, `onClick`, ... — see `core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('bar', el, rows, { fields: { label: 'plan', value: 'mrr_usd' }, valueFormat: 'currency', sort: 'descending' })
```

## Edge cases
Empty → empty state. Zeros → 1px stubs on the baseline (still hoverable). Negatives hang
below the zero line. Nulls → gaps + note. Duplicates → summed + note. 5,200 bars → canvas.
Long and RTL labels rotate 45°, truncate, and show the full text on hover.
