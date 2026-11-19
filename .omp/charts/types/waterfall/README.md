# Waterfall chart (`waterfall`)

Floating bars that walk a running total from a start to an end through increases and
decreases; totals stand on zero.

## When to use
- Explaining how one number became another: a revenue bridge, an income statement from
  revenue to net income, headcount movements, a budget variance.
- Mixing changes with subtotals (gross profit, operating profit) along the way.

## When not to use
- The changes have no order or do not add up to anything: `diverging-bar`.
- A value over time rather than contributions to a change: `line` or `bar`.
- Parts of one total with no start and end: `stacked-bar` or `treemap`.

## Input
Rows of `{ label, value, kind? }`. `kind` is `'delta'` (the default, a change) or `'total'`.
```json
[
  { "label": "Opening", "value": 500, "kind": "total" },
  { "label": "New customers", "value": 180 },
  { "label": "Churn", "value": -120 }
]
```
Arithmetic: each change floats from the previous running total to the new one. A `total`
row is always drawn at **the running total at that point** — its `value` may be left
blank. The one exception is a total that comes before any change: its value is the
opening balance. A later total whose given value disagrees with the running total is
drawn at the running total and noted (the tooltip shows the given value). With
`showTotal`, a closing total is appended unless the last row already is one.

Also accepted: CSV text (`kind` may be blank), column arrays, and any column names through
a field mapping: `{ fields: { label: 'line_item', value: 'amount_usd_k', kind: 'row_type' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `showTotal` | boolean | `true` | append a closing total when the last row is not a total |
| `totalLabel` | string | `Total` | label of the appended total |
| `connectors` | boolean | `true` | dashed lines carrying each running total to the next bar |
| `showValues` | boolean | `true` | signed change on each change bar, the total on each total (dropped when bars are too narrow) |
| `xTitle`, `yTitle` | string | — | axis titles |

Colours: increases `theme.positive`, decreases `theme.negative`, totals `theme.neutral`.
The legend does not toggle: hiding a step would silently change every total after it.
Every chart also takes the base options (see `core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('waterfall', el, csv, { fields: { label: 'line_item', value: 'amount_usd_k', kind: 'row_type' }, valueFormat: 'si' })
```

## Edge cases
Empty (or no values at all) → empty state. Single (an opening total) → one bar. Zeros →
1px stubs on the running total. Negatives → the walk dips below zero; labels go under
falling bars. Nulls → treated as no change + note. Duplicates → kept as separate steps +
note. 5,201 bars → canvas. Long and RTL labels rotate, truncate, full text on hover.
Outliers → a tall domain.
