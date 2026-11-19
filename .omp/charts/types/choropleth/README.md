# Choropleth map (`choropleth`)

Countries of the world, or US states, filled by a value on a sequential colour ramp
(diverging around zero when the values span it).

## When to use
- A rate, share or per-capita figure that varies by country or state.
- Spotting regional patterns: clusters, gradients, outliers next to their neighbours.

## When not to use
- Raw counts or totals: big regions dominate. Normalise first, or use `bubble-map`.
- US states where small states matter as much as large ones: `hex-tile-map`.
- Exact comparison or ranking: `horizontal-bar` (sorted).
- Points rather than regions: `dot-map`; movement between places: `flow-map`.

## Input
Rows of `{ region, value }`. `value` may be `null` (the region stays grey, noted).
```json
[{ "region": "BRA", "value": 71.2 }, { "region": "France", "value": 85.3 }]
```
Region keys on the world map: ISO 3166-1 alpha-3 (`FRA`), alpha-2 (`FR`), numeric
(`250` or `4` for `004`) or name -- case-, accent- and punctuation-insensitive, with common
aliases (`United States`, `DR Congo`, `Czech Republic`, `Türkiye`) and several names in their
own script (`日本`, `مصر`, `Россия`). On `map: 'us-states'`: 2-digit FIPS (`06` or `6`), USPS
postal code (`CA`) or name. Keys that match nothing are listed in a note
(`2 regions not found: Atlantis, Narnia`); regions this map does not draw (Puerto Rico under
Albers USA, microstates on the 110m world map) are noted separately.

Rows naming the same region twice (`USA` and `United States`) are summed and noted.

Also accepted: CSV text, column arrays and a field mapping:
`{ fields: { region: 'country', value: 'life_expectancy_years' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `map` | `world \| us-states` | `world` | base map; world uses Natural Earth, US uses Albers USA (Alaska and Hawaii inset) |
| `scale` | `linear \| log \| quantile` | `linear` | value -> colour. `log` tames outliers (positive values only, else linear and noted); `quantile` uses 5 equal-count classes |
| `valueLabel` | string | — | names the value in the legend, tooltips and summary |

Every chart also takes the base options (`valueFormat`, `locale`, `theme`, `title`,
`onClick`, ... — see `core/fields.ts`). Both projections are fitted to the box.

## Example
```ts
import { chart } from '.omp/charts'
chart('choropleth', el, csvText, { fields: { region: 'country', value: 'rate' }, valueLabel: 'Unemployment (%)' })
chart('choropleth', el, rows, { map: 'us-states', scale: 'quantile' })
```

## Edge cases
Empty → empty state. One region → one filled region, legend min = max. All zeros → one
colour. Values spanning zero → diverging scale centred on 0. Nulls → grey + note. Duplicate
spellings → summed + note. All 177 countries plus microstates → every country filled,
microstates noted. Long official names and names in Arabic, Hebrew, CJK, Hangul, Greek,
Devanagari, Cyrillic resolve, and the tooltip keeps the spelling given. Outliers → use
`scale: 'log'` (the sample does). Unknown keys → listed in the note.
