# Flow map (`flow-map`)

Arcs between places on a world or US map, line width proportional to the volume, an
arrowhead at the destination.

## When to use
- Movement between places: trade, migration, shipments, flights, moves between states.
- Showing the few dominant routes among many.

## When not to use
- Flows where geography does not matter: `sankey` or `chord`-style views.
- A value per place: `bubble-map` or `choropleth`.
- Thousands of routes of similar size: the map becomes a mesh; aggregate first.

## Input
Rows of `{ from, to, value }` (region keys) or `{ fromLon, fromLat, toLon, toLat, value }`.
```json
[{ "from": "BRA", "to": "USA", "value": 480 }, { "fromLon": -73.78, "fromLat": 40.64, "toLon": -0.45, "toLat": 51.47, "value": 3.9 }]
```
Region keys are those of `choropleth` (ISO3/ISO2/numeric/name; FIPS/postal/name on
`map: 'us-states'`), anchored at the centroid of the region's largest polygon. Routes named
twice (in any spelling) are summed; A→B and B→A stay separate. Unmatched keys, missing values,
negative values, self-flows, rows without both ends and routes off the projection are
skipped, each noted.

Also accepted: CSV text, column arrays and a field mapping:
`{ fields: { from: 'origin_country', to: 'destination_country', value: 'migrants' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `map` | `world \| us-states` | `world` | base map |
| `arc` | `curved \| great-circle` | `curved` | bowed arcs (left of travel, so opposite directions separate) or great circles (`geoInterpolate`, split at the antimeridian) |
| `colorBy` | `single \| origin` | `single` | one colour, or one per origin with a legend that toggles origins |
| `maxWidth` | number | 2.5% of the shorter side, 3–12 px (thinner on dense maps) | width of the largest flow; the smallest is 1 px |
| `showLabels` | boolean | `true` | name up to 12 of the busiest named places, without overlaps |
| `valueLabel` | string | — | names the value in tooltips |

Above 100 flows the lines get fainter; above 1,000 arrowheads are dropped; above 5,000
core paints on a canvas. Base options — see `core/fields.ts`.

## Example
```ts
import { chart } from '.omp/charts'
chart('flow-map', el, csvText, { fields: { from: 'origin', to: 'destination', value: 'n' }, arc: 'great-circle' })
```

## Edge cases
Empty → empty state. One flow → full width. Zeros → 1 px arcs. Negatives → omitted + note.
Nulls, missing ends and self-flows → skipped + note. Duplicates → summed + note. 5,200
routes → canvas, thin translucent lines. Long names → place labels truncated with the full
name on hover; tooltips carry it. Names in their own scripts resolve and label as written.
Outliers → widths stay proportional, small routes keep 1 px. Coordinates crossing the
antimeridian → the great circle is split at the map edge; it is hovered on its longest piece.
Every flow is hovered through a transparent stroke at least 8 px wide drawn over all the
lines, so a 1 px route does not need pixel-hunting.
