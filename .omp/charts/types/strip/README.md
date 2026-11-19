# Strip plot (`strip`)

Every value as a point in its group's band, jittered across the band (seeded, so the same data
and `seed` always draw the same picture), with a rule at each group's median.

## When to use
- Showing every observation per group when there are tens to thousands of them.
- A quick, honest look at spread, clusters and outliers before summarising with a box plot.

## When not to use
- You want points packed without any overlap: `beeswarm`.
- Only the summary matters, or groups hold tens of thousands of values: `box-plot`, `violin`, `histogram`.
- Two numeric measures per row: `scatter`.

## Input
Rows of `{ group, value }`. `value` may be `null` (skipped, noted). Groups keep first-seen order.
```json
[{ "group": "Mon", "value": 21 }, { "group": "Tue", "value": 25 }]
```
Also accepted: CSV text (`"carrier,days\nParcelo,2.4"`), column arrays
(`{ "group": [...], "value": [...] }`) and a field mapping:
`{ fields: { group: 'carrier', value: 'days_to_deliver' } }`. Duplicate rows are distinct points.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `orientation` | `vertical \| horizontal` | `vertical` | groups along x and values up y, or groups down y and values along x |
| `jitter` | number 0–1 | `0.6` | share of the band the points spread across; `0` lines them up |
| `radius` | number (px) | auto | point radius; by default from the point count |
| `showMedian` | boolean | `true` | a rule at each group's median |
| `valueTitle`, `groupTitle` | string | — | axis titles |

The jitter is drawn from the chart's seeded random source: set the base option `seed` to pick a
different (still reproducible) arrangement. Every chart also takes the base options — see `core/fields.ts`.

## Example
```ts
import { chart } from '.omp/charts'
chart('strip', el, csv, { fields: { group: 'carrier', value: 'days_to_deliver' }, valueTitle: 'Days', seed: 3 })
```

## Edge cases
Empty → empty state (also when every value is missing). One value → one point, no median rule.
Zeros → a flat strip on a padded axis. Negatives → below the zero line. Nulls → skipped + note.
Duplicates → distinct points, separated by the jitter. 5,400 points → canvas. Long and RTL
group names rotate and truncate with the full text on hover. Outliers stretch the value axis.
