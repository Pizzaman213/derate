# Horizon chart (`horizon`)

Many time series in little height: each row's value range is cut into bands that are folded onto the baseline and layered in darker shades.

## When to use
- Dozens of series where you want to spot when each one was unusually high or low (servers, cities, stocks).

## When not to use
- One or two series with room to spare: `line` or `area` are easier to read.
- Exact values matter: horizon trades precision for density.

## Input
Rows of `{ x, y, series? }`, `x` a date. One row per series (first-seen order). Repeated (series, x) readings are averaged and noted; null `y` leaves a gap.
```json
[{ "x": "2026-01-01", "y": 12.5, "series": "North" }]
```
Also accepted: CSV text, column arrays, a field mapping: `{ fields: { x: 'date', series: 'city', y: 'anomaly_c' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `bands` | 1–6 | `3` | how many times the range folds |
| `mode` | `offset \| mirror` | `offset` | negatives hang from the row's top, or are mirrored up from the baseline |
| `max` | number | largest \|y\| | the value a full stack of bands represents; set the same on several charts to compare them; values beyond it are clipped (noted) |

Zoom (`zoom: true`) and brush act on x.

Every chart also takes the base options (`valueFormat`, `locale`, `timeZone`, `theme`, `patterns`, `title`, ...; see `core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('horizon', el, csv, { fields: { x: 'date', series: 'city', y: 'anomaly_c' }, bands: 4 })
```

## Edge cases
Positives use the blue half of the diverging ramp, negatives the red half, darker per band; the legend gradient shows −max…max. Each reading has an invisible hover strip (the data mark). A lone reading draws a 2px sliver. 5,200 readings → canvas.
