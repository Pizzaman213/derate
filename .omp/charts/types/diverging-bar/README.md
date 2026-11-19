# Diverging bar chart (`diverging-bar`)

Horizontal bars running left or right of a centre line — zero, a target, or a scale midpoint — coloured by side.

## When to use
- Signed change: year-over-year growth, profit/loss by unit.
- Deviation from a target or the neutral point of a scale (Likert mean vs 3): set `midpoint`.

## When not to use
- Unsigned magnitudes: `horizontal-bar`. Full Likert distributions per answer: `stacked-bar-100`.
- Change between two points per category: `slope` or `dumbbell`.

## Input
Rows of `{ label, value }`. `value` may be `null`.
```json
[{ "label": "Alpha", "value": 120 }, { "label": "Bravo", "value": 340 }]
```
Also accepted: CSV text (`"label,value\nAlpha,120"`), column arrays (`{ "label": [...], "value": [...] }`),
and any column names through a field mapping: `{ fields: { label: 'country', value: 'respondents' } }`.
Duplicate labels are summed and noted under the chart. `value` is signed; bars run from `midpoint` to the value.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `midpoint` | number | `0` | centre line the bars diverge from |
| `sort` | `none \| ascending \| descending` | `none` | bar order, top to bottom |
| `symmetric` | boolean | `true` | equal room either side of the midpoint |
| `showValues` | boolean | `false` | value at the outer end of each bar |
| `aboveLabel` | string | "Positive" / "Above <m>" | legend text for the right side |
| `belowLabel` | string | "Negative" / "Below <m>" | legend text for the left side |
| `xTitle`, `yTitle` | string | — | axis titles |

Every chart also takes the base options (`valueFormat`, `locale`, `currency`, `theme`,
`patterns`, `title`, `onClick`, ... — see `core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('diverging-bar', el, csv, { fields: { label: 'question', value: 'mean_score' }, midpoint: 3, sort: 'descending' })
```

## Edge cases
Empty → empty state. Values equal to the midpoint (zeros) → neutral 1px stubs on the centre line. Nulls → gaps + note.
Duplicates → summed + note. 5,200 bars → canvas. Colour is backed by pattern fills (`patterns: true`) and by the side of the line.
Long/RTL labels truncate horizontally with full text on hover. Outliers → the symmetric domain stretches both sides.
