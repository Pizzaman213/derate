# Pareto chart (`pareto`)

Causes as bars sorted largest first, with the cumulative share as a line on a right-hand 0–100% axis and a reference line at 80%. The second y axis is inherent to the chart: two views of one ordering.

## When to use
- Finding the vital few: defects by cause, tickets by reason, cost by supplier.
- Prioritising: how many categories cover 80% of the total.

## When not to use
- Values that can be negative or have no meaningful total: `horizontal-bar`.
- Unordered categories, or categories with a natural order (months): `bar`.
- Composition of a single whole: `treemap` or `pie`.

## Input
Rows of `{ label, value }`. `value` may be `null`.
```json
[{ "label": "Alpha", "value": 120 }, { "label": "Bravo", "value": 340 }]
```
Also accepted: CSV text (`"label,value\nAlpha,120"`), column arrays (`{ "label": [...], "value": [...] }`),
and any column names through a field mapping: `{ fields: { label: 'country', value: 'respondents' } }`.
Duplicate labels are summed and noted under the chart.
Negative and null values are omitted (noted): a cumulative share needs non-negative parts.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `maxBars` | integer ≥ 2 | `20` | categories beyond this combine into a final "Other" bar (noted) |
| `threshold` | number 0–1 | `0.8` | reference line on the cumulative axis |
| `showValues` | boolean | `false` | value above each bar |
| `cumulativeTitle` | string | `Cumulative %` | title of the right-hand axis |
| `xTitle`, `yTitle` | string | — | axis titles |

Every chart also takes the base options (`valueFormat`, `locale`, `currency`, `theme`,
`patterns`, `title`, `onClick`, ... — see `core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('pareto', el, csv, { fields: { label: 'reason', value: 'tickets' }, yTitle: 'Tickets', maxBars: 10 })
```

## Edge cases
Empty, and all-zero data → empty state ("All values are zero"). Negatives and nulls → omitted + note. Duplicates → summed + note.
Large (5,200 categories) → the top 19 plus one "Other" bar, noted. The cumulative line always ends at exactly 100%.
Long/RTL labels rotate and truncate with full text on hover.
