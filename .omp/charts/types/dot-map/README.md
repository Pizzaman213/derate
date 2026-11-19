# Dot map (`dot-map`)

One dot per row at its longitude/latitude on a world or US map, coloured by category.

## When to use
- Where things are: stores, incidents, sensors, epicentres — density and clustering.
- Comparing the spatial spread of a few categories.

## When not to use
- A magnitude per point: `bubble-map`.
- A value per region: `choropleth` or `hex-tile-map`.
- Tens of thousands of overlapping points where density is the message: consider binning
  (`hexbin`) first.

## Input
Rows of `{ lon, lat, category? }`.
```json
[{ "lon": -0.13, "lat": 51.51, "category": "Europe" }, { "lon": 139.69, "lat": 35.69, "category": "Asia" }]
```
Rows with a missing or out-of-range coordinate, and points off the projection (Paris on the
US map), are skipped and noted. Duplicate rows are kept as distinct dots.

Also accepted: CSV text, column arrays and a field mapping:
`{ fields: { lon: 'longitude', lat: 'latitude', category: 'charger_type' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `map` | `world \| us-states` | `world` | base map |
| `radius` | number | 3.5 → 1.25 px as the count grows | dot radius |
| `maxCategories` | integer 1–12 | `8` | beyond this, the least frequent categories merge into "Other" (noted) |

The legend lists categories (when there are two or more) and toggles them. Above 5,000
dots core paints them on a canvas. Base options — see `core/fields.ts`.

## Example
```ts
import { chart } from '.omp/charts'
chart('dot-map', el, csvText, { map: 'us-states', fields: { lon: 'longitude', lat: 'latitude', category: 'type' } })
```

## Edge cases
Empty → empty state. One dot → no legend. All at 0,0 → stacked on one spot. Negative
coordinates (southern/western hemispheres) → drawn. Nulls/out of range → skipped + note.
Duplicates → distinct dots. 5,100 dots → canvas. Long category names → ellipsised in the
legend with the full text on hover. Unicode categories render. Outlying points → the map
extent is fixed, nothing stretches. Twelve categories → seven + Other, noted.
