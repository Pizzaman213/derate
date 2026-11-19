# Parallel coordinates (`parallel-coordinates`)

One vertical axis per numeric column, each with its own scale and ticks, and one line per
row crossing them all. Hovering a line highlights it; series colour the lines.

## When to use
- Comparing many rows across 3–12 numeric attributes at once (profiles, configurations, hosts).
- Finding clusters and trade-offs: crossing lines between two axes mean a negative relationship.

## When not to use
- Two or three variables: `scatter` or `splom` read more precisely.
- A few items over a few attributes on a common scale: `radar` or `grouped-bar`.
- Values over time: `line` (axes here are not ordered in time).

## Input
Wide rows, one numeric column per axis, plus optional `series` (line colour, legend toggle)
and `label` (names the row in tooltips and announcements). Text columns are ignored.
```json
[{ "label": "Civic", "series": "Japan", "mpg": 36, "horsepower": 158, "weight": 2900 }]
```
Also accepted: CSV text, column arrays, and a field mapping for the reserved columns:
`{ fields: { label: 'name', series: 'mfr' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `columns` | string[] | every numeric column (up to 12) | axes, in order (an unknown or text column is an input error) |
| `zero` | boolean | `false` | start every axis at zero instead of its minimum |

Every chart also takes the base options (`valueFormat`, `locale`, `theme`, `onClick`, ... — see `core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('parallel-coordinates', el, cerealCsv, { fields: { label: 'name', series: 'mfr' }, columns: ['calories', 'sugars', 'fiber', 'rating'] })
```

## Edge cases
Empty → empty state; no numeric column → "No numeric columns to plot". One row → one line
(axes widen around a single value). All zeros → every line runs flat through 0. Negatives
sit below zero on their axis. Nulls → the line breaks at the missing axis, an isolated value
becomes a short tick (noted). Identical rows are drawn once, with the count in the tooltip
(noted). 5,200 rows → canvas; the axes and ticks stay in SVG. Long and RTL axis names
truncate with the full name on hover. Outliers stretch only their own axis.

The legend toggles a series. Hover highlights one line, so pointing at a legend entry dims
every line rather than picking out that series (core highlights by one key per mark). Each
line is hovered through a transparent 4 px stroke over it: wide enough that a 1.5 px line
needs no pixel-hunting, narrow enough that later lines do not bury earlier ones.
