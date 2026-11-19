# Hex-tile map (`hex-tile-map`)

Every US state and DC as one equal hexagon on a standard grid (`geo/us-hex.json`),
coloured by a value and labelled with its postal code.

## When to use
- A value per state where every state should weigh the same: Rhode Island is as visible
  as Texas, which a `choropleth` cannot offer.
- Compact layouts and small multiples: the tiles stay legible far smaller than state outlines.

## When not to use
- When real geography matters (distances, borders, regions inside states): `choropleth`.
- Countries or territories: the grid has the 50 states and DC only; use `choropleth`.
- Precise comparison or ranking: `horizontal-bar`.

## Input
Rows of `{ state, value }`. `value` may be `null` (grey tile, noted).
```json
[{ "state": "CA", "value": 35.9 }, { "state": "Texas", "value": 32.3 }, { "state": "36", "value": 39.9 }]
```
`state` is a USPS postal code, a 2-digit FIPS code (leading zero optional) or a name
(case- and accent-insensitive). Keys that match nothing are listed in a note; territories
(Puerto Rico, Guam, ...) resolve but have no tile, and are noted. Rows naming one state twice
are summed and noted. States without a row are drawn as grey tiles (not data marks).

Also accepted: CSV text, column arrays and a field mapping:
`{ fields: { state: 'state_name', value: 'median_household_income' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `scale` | `linear \| log \| quantile` | `linear` | value -> colour (see `choropleth`); diverging around zero when values span it |
| `valueLabel` | string | — | names the value in the legend, tooltips and summary |
| `showValues` | boolean | `false` | print the value under each postal code when tiles are big enough |

Base options (`valueFormat`, `locale`, `theme`, `title`, ...) — see `core/fields.ts`.

## Example
```ts
import { chart } from '.omp/charts'
chart('hex-tile-map', el, rows, { valueLabel: "Bachelor's degree (%)", valueFormat: '.1f', showValues: true })
```

## Edge cases
Empty → empty state. One state → one coloured tile among 50 grey ones. Zeros → one colour.
Negatives → diverging scale. Nulls → grey + note. Duplicates and 5,200 county-level rows →
summed into 51 tiles + note. Long names resolve (DC, Northern Mariana Islands → noted as
off-grid); an unrecognisable long key is shortened in the note. Accented names resolve;
Arabic/Hebrew/CJK keys match no state and are listed as written. Outliers → `scale: 'log'`.
Postal labels are dropped when tiles are too small to hold them (320 px); tooltips remain.
