# Beeswarm chart (`beeswarm`)

Every value as its own circle along a value axis, dodged sideways so no two overlap.

## When to use
- Showing the shape of a distribution *and* every individual observation (tens to a few thousand).
- Comparing a handful of groups while keeping each point hoverable (who is the outlier?).

## When not to use
- Tens of thousands of points: `histogram`, `violin` or `box-plot` summarise without drawing each one.
- Only the summary matters: `box-plot`.
- Exact position along the cross axis is meaningful (two measures): `scatter`.
- Quick, cheap overview with jitter rather than packing: `strip`.

## Input
Rows of `{ value, group?, label? }`. `value` may be `null` (skipped, noted). `group` gives one
swarm per group (first-seen order); `label` names the point in its tooltip.
```json
[{ "group": "North", "label": "Ada", "value": 62 }, { "group": "South", "label": "Ben", "value": 71 }]
```
Also accepted: CSV text (`"region,latency_ms\nus-east-1,38"`), column arrays
(`{ "value": [...], "group": [...] }`) and a field mapping:
`{ fields: { value: 'latency_ms', group: 'region', label: 'request_id' } }`.
Duplicate values are distinct points: they stack side by side.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `orientation` | `horizontal \| vertical` | `horizontal` | values along x with groups stacked, or values up y with groups side by side |
| `radius` | number (px) | auto | fixed circle radius; by default circles shrink until each swarm fits its band |
| `scale` | `linear \| log` | `linear` | log skips values ≤ 0 (noted) |
| `valueTitle`, `groupTitle` | string | — | axis titles |

Every chart also takes the base options (`valueFormat`, `locale`, `theme`, `seed`, ... — see `core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('beeswarm', el, csv, { fields: { value: 'latency_ms', group: 'region', label: 'request_id' }, valueTitle: 'Latency (ms)' })
```

## Edge cases
Empty → empty state (also when every value is missing). One value → one circle on a padded
axis. All equal / zeros → one tight swarm. Nulls → skipped + note. Duplicates → distinct
circles. 5,400 values → canvas, circles shrink to ~1px. Above 4,000 values in one swarm the
exact dodge (which tries tangent positions against every near neighbour) gives way to stacking
in columns one diameter wide, a Wilkinson dot plot: still no overlaps, near-linear, so 50,000
values render in well under 500 ms; positions are snapped to the column. Long and RTL group names truncate with
the full text on hover. Outliers stretch the axis (use `scale: 'log'`). If a swarm cannot fit
even at the smallest radius, edge points are pressed into the band and a note says so.
