# Radar chart (`radar`)

One spoke per axis and one polygon per series on a concentric grid; a vertex's distance
from the centre is linear in its value, from 0 (or the minimum, when values go negative)
at the centre to `max` at the outer ring.

## When to use
- Comparing a few items (≤ 5) across 4–12 attributes on a shared scale: skill ratings,
  review scores, benchmark profiles.
- Showing the overall "shape" of a profile against another.

## When not to use
- Attributes on different scales: normalise first, or use `parallel-coordinates` (one scale per axis).
- Precise comparison of values: `grouped-bar` reads lengths along a common baseline.
- Many series: polygons pile up; use `parallel-coordinates` or small multiples.
- Ordered or time-based axes: `line`.

## Input
Rows of `{ axis, series?, value }`; `value` may be `null` (the vertex is left out and the
outline breaks there). Duplicate axis/series pairs are summed and noted.
```json
[{ "axis": "Speed", "series": "Alice", "value": 8 }, { "axis": "Speed", "series": "Bruno", "value": 5 }]
```
Wide rows are also accepted: one row per series, one numeric column per axis, and the
series name in a `series` column (or the first text column):
`[{ "product": "Pixel 9", "Camera": 9.5, "Battery life": 7.5 }]`. So are CSV text, column
arrays and a field mapping: `{ fields: { series: 'product' } }` or `{ fields: { axis: 'skill', value: 'score' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `max` | number | data max, rounded up to a nice number | value at the outer ring; larger values are drawn on the ring (noted) |
| `levels` | integer 1–10 | `4` | approximate number of grid rings |
| `grid` | `polygon \| circle` | `polygon` | ring shape |
| `fill` | boolean | `true` | fill each complete polygon lightly as well as outlining it |

Every chart also takes the base options (`valueFormat`, `locale`, `theme`, `onClick`, ... — see `core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('radar', el, reviewCsv, { fields: { series: 'product' }, max: 10 })
```

## Edge cases
Empty → empty state; all null → "No values to plot". One row → one spoke with one vertex.
Zeros → every vertex at the centre. Negatives → the centre becomes the minimum (labelled on
the rings). Nulls → the outline breaks and is not filled (noted). Duplicates → summed (noted).
48 axes × 4 series → axis labels are thinned so none collide; every vertex keeps its
tooltip. Long and RTL axis names truncate to the room at their angle (full text on hover);
at 320px the radius shrinks to make room. Outliers → `max` grows to fit, or clamp with `max`.
Legend entries toggle a series.
