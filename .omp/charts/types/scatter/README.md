# Scatter plot (`scatter`)

Dots at (x, y) showing how two measures move together, optionally grouped by series.

## When to use
- Looking for a relationship, clusters or outliers between two numeric measures.
- Comparing that relationship across a few groups (`series`, one colour each).
- Up to tens of thousands of points (canvas above 5,000).

## When not to use
- You want the fitted trend and its strength stated: `regression-scatter`.
- A third measure matters (population, revenue): `bubble`.
- The points are an ordered path through time: `connected-scatter`.
- So many points that dots become a smear: `hexbin` or `contour-density`.
- One measure over time: `line`.

## Input
Rows of `{ x, y, series?, label? }`. `x`/`y` may be `null` (the row is skipped and noted).
```json
[{ "x": 1, "y": 52, "label": "Ana" }, { "x": 2, "y": 58, "series": "Evening" }]
```
Also accepted: CSV text (`"x,y\n1,52"`), column arrays (`{ "x": [...], "y": [...] }`),
and any column names through a field mapping:
`{ fields: { x: 'gdp_per_capita_usd', y: 'life_expectancy_years', series: 'region', label: 'country' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `xScale`, `yScale` | `linear \| log` | `linear` | log axes tick at powers of ten over 2+ decades; values ≤ 0 are omitted and noted |
| `pointRadius` | number (px, ≤ 20) | auto | 4.5 for a handful of points, down to 2 for thousands |
| `labels` | `auto \| none` | `auto` | direct labels from `label` (≤ 60 labelled points); a label that would collide or leave the plot is dropped, the tooltip keeps it |
| `xTitle`, `yTitle` | string | — | axis titles, also used in tooltips and the summary |

Zoom and brush: `zoom: true` (wheel/drag, both axes; double-click resets), `brush: true`
with `onBrush`. Every chart also takes the base options (`valueFormat`, `locale`, `theme`,
`patterns`, `title`, `onClick`, ... — see `core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('scatter', el, csvText, {
  fields: { x: 'gdp_per_capita_usd', y: 'life_expectancy_years', series: 'region', label: 'country' },
  xScale: 'log', xTitle: 'GDP per capita (USD, log)', yTitle: 'Life expectancy (years)',
})
```

## Edge cases
Empty → empty state. Single point → padded axes around it. All zeros → one stack of dots,
noted as overlapping. Negatives → axes cross zero with a zero rule. Nulls → skipped + note.
Duplicates → kept as distinct dots and noted as overlapping. 6,000 points → canvas. Long and
RTL labels/series → truncated direct labels with the full text on hover, legend truncates.
Outliers → the domain stretches (use `yScale: 'log'` when the spread is multiplicative).
