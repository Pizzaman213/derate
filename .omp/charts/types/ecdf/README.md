# Empirical CDF (`ecdf`)

For every value x, the share of observations at or below x — a step curve per series. Every
step is hoverable ("75% of values ≤ 20 (3 of 4)").

## When to use
- Reading percentiles straight off a distribution ("90% of page loads finish under 2.5 s").
- Comparing distributions of different sizes: curves are normalised, so 40 and 4,000 values overlay.
- Avoiding the binning choices a histogram forces.

## When not to use
- The audience expects the shape (peaks, modality) rather than percentiles: `histogram`, `violin`.
- Comparing summaries only: `box-plot`.
- Checking against a theoretical distribution: `qq-plot`.

## Input
Rows of `{ value, series? }`. `value` may be `null` (skipped, noted). `series` draws one curve per
series (first-seen order) with a legend.
```json
[{ "series": "Desktop", "value": 1.42 }, { "series": "Mobile", "value": 3.18 }]
```
Also accepted: CSV text, column arrays (`{ "value": [...] }`) and a field mapping:
`{ fields: { value: 'lcp_seconds', series: 'device' } }`. Repeated values are one taller step
(ties collapse into a single step whose share counts all of them).

## Options
| Option | Type | Default | |
|---|---|---|---|
| `showPoints` | `auto \| always \| never` | `auto` | dots at each step (auto: up to 60 steps per curve); steps stay hoverable either way |
| `scale` | `linear \| log` | `linear` | value scale; log skips values ≤ 0 (noted) |
| `valueTitle` | string | — | x-axis title |
| `yTitle` | string | `Cumulative share` | y-axis title |

The hidden table and CSV export list each step: series, value, count ≤ value, share ≤ value.
Every chart also takes the base options — see `core/fields.ts`.

## Example
```ts
import { chart } from '.omp/charts'
chart('ecdf', el, csv, { fields: { value: 'lcp_seconds', series: 'device' }, valueTitle: 'LCP (s)' })
```

## Edge cases
Empty → empty state (also when every value is missing). One value → one step from 0 to 100%.
All equal / zeros → a single step on a padded axis. Negatives → drawn left of zero. Nulls →
skipped + note. Duplicates → same curve (ties collapse). 5,300 distinct values → 5,300 steps on
canvas. Long and RTL series names ellipsise in the legend and show in full in tooltips. Outliers
stretch the axis (try `scale: 'log'`).
