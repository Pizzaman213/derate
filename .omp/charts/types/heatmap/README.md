# Heatmap (`heatmap`)

A grid of cells across two categorical (or date) dimensions, each coloured by its value.

## When to use
- One measure across two categories: sales by region × quarter, errors by service × day.
- Spotting patterns, clusters and outliers in a dense grid (thousands of cells draw on canvas).
- Values that span zero (anomalies, changes): the ramp turns diverging automatically.

## When not to use
- Precise comparison of a few values: `bar` or `grouped-bar` (length beats colour).
- One value per calendar day: `calendar-heatmap`.
- Activity by weekday × hour from timestamps: `punch-card`.
- Pairwise correlations of numeric columns: `correlation-matrix`.
- Links between the members of one set: `adjacency-matrix`.
- A table with several numeric columns on different scales: `table-heatmap`.

## Input
Rows of `{ x, y, value }`. `x` and `y` are categories, or dates (ISO strings, `Date`s or
10/13-digit epoch numbers) -- a date axis sorts chronologically and compares instants, so
`2026-03-01T17:30+05:30` and `2026-03-01T12:00Z` are the same column. `value` may be `null`
(drawn hatched, noted).
```json
[{ "x": "Q1", "y": "North", "value": 42 }, { "x": "Q2", "y": "North", "value": 51 }]
```
Also accepted: CSV text, column arrays (`{ "x": [...], "y": [...], "value": [...] }`) and a
field mapping: `{ fields: { x: 'month', y: 'year', value: 'anomaly_c' } }`.
Rows sharing a cell are combined by `aggregate` and noted under the chart.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `xOrder`, `yOrder` | `input \| name \| value` | `input` | category order: first seen, natural name order, or by total (dates are always chronological) |
| `aggregate` | `sum \| mean \| max \| min \| count` | `sum` | how rows sharing a cell combine |
| `diverging` | boolean | auto | force or forbid the diverging ramp; default: diverging when values span `center` |
| `center` | number | `0` | midpoint of the diverging ramp |
| `colorScale` | `linear \| log` | `linear` | `log` (signed) compresses outliers so the rest stays readable |
| `showValues` | boolean | `true` | print values in cells when every value fits; ink flips to the surface colour on dark cells |
| `xTitle`, `yTitle` | string | — | axis titles (also the table's column names) |

Base options (`valueFormat`, `timeZone`, `locale`, `theme`, `renderer`, ...) are in `core/fields.ts`.

## Example
```ts
import { chart } from '.omp/charts'
chart('heatmap', el, csvText, { fields: { x: 'month', y: 'year', value: 'anomaly_c' }, valueFormat: '+.2f' })
```

## Edge cases
Empty → empty state. One row → one cell. All zeros → every cell at the ramp midpoint, summary
says "every cell at 0". Negatives → diverging ramp centred on 0. Nulls → hatched cells, still
focusable ("no value"), noted. Duplicates → combined by `aggregate`, noted. 5,408 cells →
canvas, values hidden. Long and RTL labels → truncated/rotated, full text on hover. Outliers →
linear by default; `colorScale: 'log'` keeps the rest distinguishable. Unsorted dates sort;
mixed time-zone offsets land on the same instant.
