# Horizontal bar chart (`horizontal-bar`)

Horizontal bars comparing one value across categories; category names sit on the y axis and read horizontally.

## When to use
- Comparing one measure across categories with long names (countries, survey answers, product names).
- Rankings: `sort: 'descending'` reads top to bottom.
- More categories than fit side by side in a `bar` chart.

## When not to use
- Short labels and few categories, or an ordinal x (months): `bar`.
- Several series per category: `grouped-bar` or `stacked-bar` with `orientation: 'horizontal'`.
- Signed deviation from a reference: `diverging-bar`.
- Causes ranked with their cumulative share: `pareto`.

## Input
Rows of `{ label, value }`. `value` may be `null`.
```json
[{ "label": "Alpha", "value": 120 }, { "label": "Bravo", "value": 340 }]
```
Also accepted: CSV text (`"label,value\nAlpha,120"`), column arrays (`{ "label": [...], "value": [...] }`),
and any column names through a field mapping: `{ fields: { label: 'country', value: 'respondents' } }`.
Duplicate labels are summed and noted under the chart.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `sort` | `none \| ascending \| descending` | `none` | bar order, top to bottom |
| `showValues` | boolean | `false` | value past the end of each bar (dropped when bars are thinner than 9px) |
| `colorBy` | `single \| category` | `single` | one colour, or one per category (pattern fills follow) |
| `labelWidth` | number | 38% of width | max px for category labels before they truncate |
| `xTitle`, `yTitle` | string | — | axis titles |

Every chart also takes the base options (`valueFormat`, `locale`, `currency`, `theme`,
`patterns`, `title`, `onClick`, ... — see `core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('horizontal-bar', el, csv, { fields: { label: 'country', value: 'respondents' }, sort: 'descending', showValues: true })
```

## Edge cases
Empty → empty state. Zeros → 1px stubs on the baseline (still hoverable). Negatives extend left of the zero line.
Nulls → gaps + note. Duplicates → summed + note. 5,200 bars → canvas, with category labels thinned so they never overlap.
Long labels truncate horizontally (full text on hover); RTL/CJK/emoji render. Outliers → a long domain; small bars stay hoverable.
