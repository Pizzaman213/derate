# Q-Q plot (`qq-plot`)

Each sorted observation against the normal quantile of its plotting position `(i − 0.5) / n`,
with a dashed reference line through the sample and normal quartiles. Points on the line mean
"normally distributed"; how the ends bend away names the departure (the summary says which:
close to normal, heavy tails, light tails, skewed right or left, with the plot's correlation r).

## When to use
- Checking a normality assumption before a t-test, a control chart or a regression.
- Seeing *how* a sample departs from normal: tails, skew, gaps, a step from rounding.

## When not to use
- A general audience who wants the shape: `histogram` or `violin`.
- Comparing two samples' distributions: `ecdf` (one curve per series) or `box-plot`.
- Percentiles at a glance: `ecdf`.

## Input
Rows of `{ value, label? }`. `value` may be `null` (skipped, noted); `label` names the point in
its tooltip.
```json
[{ "value": 171.2 }, { "value": 165.8 }, { "value": 180.4 }]
```
Also accepted: CSV text, column arrays (`{ "value": [...] }`) and a field mapping:
`{ fields: { value: 'diameter_mm', label: 'part_id' } }`. Repeated values are distinct points.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `standardize` | boolean | `false` | plot z-scores ((value − mean) / sd) instead of raw values |
| `showLine` | boolean | `true` | the dashed reference line (needs two values) |
| `xTitle` | string | `Normal quantile` | x-axis title |
| `yTitle` | string | `Sample quantile` / `Sample z-score` | y-axis title |

The hidden table and CSV list each value in sorted order with its normal quantile. Every chart
also takes the base options — see `core/fields.ts`.

## Example
```ts
import { chart } from '.omp/charts'
chart('qq-plot', el, csv, { fields: { value: 'diameter_mm', label: 'part_id' }, yTitle: 'Diameter (mm)' })
```

## Edge cases
Empty → empty state (also when every value is missing). One value → one point at quantile 0,
no line. All equal / zeros → a flat row of points with a flat line. Negatives → drawn below
zero. Nulls → skipped + note. Duplicates → distinct points (a flat run). 5,400 values → canvas.
Long and RTL labels only appear in tooltips. An outlier sits far off the line at the end it
belongs to; the axes stretch to keep it.
