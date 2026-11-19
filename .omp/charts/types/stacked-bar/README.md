# Stacked bar chart (`stacked-bar`)

Bars built from stacked series segments: each category's total and what makes it up. Positive segments stack up from zero, negative ones down (diverging offset).

## When to use
- Totals per category plus their composition: revenue by product line per quarter.
- Gains and losses together: new vs churned accounts per month (negatives stack below zero).

## When not to use
- Comparing a middle segment across categories (only the bottom one shares a baseline): `grouped-bar`.
- Shares rather than amounts: `stacked-bar-100`.
- Continuous time with many points: `stacked-area`.

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
| `orientation` | `vertical \| horizontal` | `vertical` | stack direction |
| `showTotals` | boolean | `false` | net total printed past the outer end of each stack |
| `xTitle`, `yTitle` | string | — | axis titles |

Every chart also takes the base options (`valueFormat`, `locale`, `currency`, `theme`,
`patterns`, `title`, `onClick`, ... — see `core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('stacked-bar', el, csv, { fields: { label: 'month', series: 'category', value: 'net_change' }, showTotals: true })
```

## Edge cases
Empty, and all-zero data → empty state ("All values are zero"). Legend toggle hides a series and restacks the rest.
Negatives stack below zero. Zero and null segments are not drawn (nulls noted). Duplicates → summed + note.
5,200 segments → canvas. Long/RTL labels truncate with full text on hover. Table and CSV carry a Total column.
