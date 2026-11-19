# 100% stacked bar chart (`stacked-bar-100`)

Each category as a full-length bar split into its series' shares of that category's total, so mixes compare across categories.

## When to use
- Comparing composition across categories: commute mode by age group, channel mix by region.
- When totals differ widely and only proportions matter.

## When not to use
- Totals matter: `stacked-bar`. A single whole: `pie` / `donut` (≤ 6 parts) or `treemap`.
- Signed values: shares of a whole cannot be negative — use `stacked-bar`.

## Input
Rows of `{ label, series, value }` (long format). `value` may be `null` (left out, noted).
```json
[{ "label": "Q1", "series": "North", "value": 120 }, { "label": "Q1", "series": "South", "value": 95 }]
```
Also accepted: CSV text (`"label,series,value\nQ1,North,120"`), column arrays
(`{ "label": [...], "series": [...], "value": [...] }`), and any column names through a field
mapping: `{ fields: { label: 'month', series: 'region', value: 'sales' } }`.
Duplicate (label, series) pairs are summed and noted under the chart.
Negative values are omitted (a share of a whole cannot be negative) and noted.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `orientation` | `vertical \| horizontal` | `vertical` | stack direction |
| `showValues` | boolean | `false` | share printed inside each segment when it fits |
| `xTitle`, `yTitle` | string | — | axis titles |

Every chart also takes the base options (`valueFormat`, `locale`, `currency`, `theme`,
`patterns`, `title`, `onClick`, ... — see `core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('stacked-bar-100', el, csv, { fields: { label: 'age_group', series: 'commute_mode', value: 'respondents' }, orientation: 'horizontal', showValues: true })
```

## Edge cases
Empty, and all-zero data → empty state ("All values are zero"). A single category whose total is zero → a dashed empty outline + note
(`edge-zero-category`). Every other bar sums to exactly 100%. Tooltips show value, share and total. Negatives → omitted + note.
Legend toggle re-expands the remaining series to 100%. 5,200 segments → canvas. Long/RTL labels truncate with full text on hover.
