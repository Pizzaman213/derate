# Bubble map (`bubble-map`)

Proportional symbols on a world or US map: one circle per row, its area proportional to
the value, at a region's centroid or at an explicit longitude/latitude.

## When to use
- Counts and totals by place (population, sales, cases), where a `choropleth` would let big
  regions dominate.
- Values at points (cities, stores, stations) rather than regions.

## When not to use
- Rates and shares that belong to a region's whole area: `choropleth`.
- Many points without a magnitude: `dot-map`.
- Exact comparison: `horizontal-bar` (area is judged less precisely than length).
- Movement between places: `flow-map`.

## Input
Rows of `{ region, value, label? }` or `{ lon, lat, value, label? }`.
```json
[{ "region": "IND", "value": 1430 }, { "label": "Tokyo", "lon": 139.69, "lat": 35.69, "value": 37.1 }]
```
`region` accepts the same keys as `choropleth` (ISO3/ISO2/numeric/name; FIPS/postal/name on
`map: 'us-states'`); the bubble sits at the centroid of the region's largest polygon (the US
in Kansas, France in France). Unmatched keys, points off the projection (Paris on the US
map), missing values, negative values and rows with no location are skipped, each noted.
Rows naming one region twice are summed.

Also accepted: CSV text, column arrays and a field mapping:
`{ fields: { label: 'city', lon: 'lng', lat: 'lat', value: 'population_m' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `map` | `world \| us-states` | `world` | base map |
| `maxRadius` | number | 7% of the shorter side, 6–36 px | radius of the largest bubble |
| `showLabels` | boolean | `true` | label bubbles big enough to hold their name (no overlaps) |
| `valueLabel` | string | — | names the value in tooltips and the summary |

A size key (the largest value and a round smaller one) sits in a bottom corner. Base
options — see `core/fields.ts`.

## Example
```ts
import { chart } from '.omp/charts'
chart('bubble-map', el, csvText, { fields: { label: 'city', lon: 'lng', lat: 'lat', value: 'pop' }, valueFormat: '.1f' })
```

## Edge cases
Empty → empty state. One bubble → full size. Zeros → small dots, no size key. Negatives →
omitted + note. Nulls and missing locations → skipped + note. Duplicates → summed + note.
5,200 points → canvas. Long labels → only drawn inside bubbles that fit; full text in the
tooltip. Unicode labels render. Outliers → areas stay proportional (smallest drawn at
1.5 px so they stay hoverable).
