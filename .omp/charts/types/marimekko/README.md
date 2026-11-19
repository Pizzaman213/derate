# Marimekko chart (`marimekko`)

A mosaic: each column is as wide as its share of the grand total, and is split into segments as tall as each series' share of that column.

## When to use
- Two categorical dimensions of one total, where both the size of each column and its mix matter (market size by region × vendor share).

## When not to use
- Only the mix matters, not the column sizes: `stacked-bar-100`.
- Exact values per series: `grouped-bar`.
- Many series (more than ~8): segments become unreadable — `heatmap`.
- Negative values: they have no area (omitted and noted).

## Input
Rows of `{ label, series, value }`: `label` is the column, `series` the segment.
```json
[{ "label": "North", "series": "Acme", "value": 120 }, { "label": "North", "series": "Globex", "value": 80 }]
```
Also accepted: CSV text, column arrays, and any column names through a field mapping:
`{ fields: { label: 'region', series: 'vendor', value: 'units_m' } }`.
Repeated label/series pairs are summed and noted; nulls and negatives are left out and noted.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `sort` | `none \| descending \| ascending` | `none` | column order by column total |
| `showValues` | boolean | `true` | each segment's share of its column, where it fits |
| `yTitle` | string | — | y axis title |

Every chart also takes the base options (`valueFormat`, `locale`, `currency`, `theme`, `patterns`, `title`, `onClick`, ... — see `core/fields.ts`). The legend toggles series; columns are re-measured without them.

## Example
```ts
import { chart } from '.omp/charts'
chart('marimekko', el, csvText, { fields: { label: 'region', series: 'vendor', value: 'units_m' }, sort: 'descending' })
```

## Edge cases
Empty → empty state. All zeros → "All values are zero". Negatives and nulls → omitted, noted.
Duplicates → summed, noted. One row → one full-plot segment. 650 × 8 = 5,200 segments → canvas.
Long and RTL column names → truncated to the column width, in full on hover; columns too narrow
for a name carry it in the tooltip. An outlier column takes most of the width; the rest stay hoverable.
