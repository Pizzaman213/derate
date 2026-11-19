# Lollipop chart (`lollipop`)

A thin stem from a baseline to a dot, one per category: a bar chart with less ink.

## When to use
- Comparing or ranking one value across many categories, especially when the values are
  close together and a wall of bars would be heavy (use `sort: 'descending'` to rank).
- Values measured against a reference other than zero (`baseline: 100` for an index).

## When not to use
- Few categories where the area of a bar helps: `bar` or `horizontal-bar`.
- Two values per category (before/after): `dumbbell`. Several series: `dot-plot`.
- Values over continuous time: `line`.

## Input
Rows of `{ label, value }`. `value` may be `null` (row left blank, noted).
```json
[{ "label": "Alpha", "value": 120 }, { "label": "Bravo", "value": 340 }]
```
Also accepted: CSV text (`"label,value\nAlpha,120"`), column arrays (`{ "label": [...], "value": [...] }`),
and any column names through a field mapping: `{ fields: { label: 'occupation', value: 'median_weekly_usd' } }`.
Duplicate labels are summed and noted under the chart.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `orientation` | `horizontal \| vertical` | `horizontal` | categories down the side (long names read) or along the bottom |
| `sort` | `none \| ascending \| descending` | `none` | row order (descending: largest at the top) |
| `baseline` | number | `0` | value the stems grow from; values below it grow the other way |
| `showValues` | boolean | `false` | value beside each dot (dropped when rows are too thin) |
| `xTitle`, `yTitle` | string | — | axis titles |

Every chart also takes the base options (`valueFormat`, `locale`, `currency`, `theme`,
`patterns`, `title`, `onClick`, ... — see `core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('lollipop', el, rows, { fields: { label: 'occupation', value: 'median_weekly_usd' }, valueFormat: 'currency', sort: 'descending', showValues: true })
```

## Edge cases
Empty → empty state. Single → one lollipop. Zeros → dots on the baseline, no stems.
Negatives → stems grow the other way from the baseline. Nulls → blank rows + note.
Duplicates → summed + note. 5,200 rows → canvas; axis labels thin out. Long and RTL labels
truncate with the full text on hover. Outliers → a tall linear domain (the dot is still
readable; the rest compress toward the baseline).
