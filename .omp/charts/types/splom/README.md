# Scatter-plot matrix (`splom`)

Every pair of numeric columns as a small scatter plot, each column's distribution as a
histogram on the diagonal. Scales are shared per column: a point keeps its x position all
the way down a column and its y position along a row.

## When to use
- Exploring how 3–6 numeric variables relate, before modelling.
- Spotting clusters, curves and outliers that a single coefficient hides.

## When not to use
- Two variables: `scatter` gives them the whole canvas.
- More than ~6 variables: the cells get too small; screen with `correlation-matrix` first.
- One observation per category: `parallel-coordinates` follows each row across the variables.

## Input
Wide rows, one numeric column per variable, plus optional `series` (colours the points and
stacks the histograms) and `label` (names the point in tooltips). Text columns are ignored.
```json
[{ "series": "setosa", "sepalLength": 5.1, "sepalWidth": 3.5, "petalLength": 1.4 }]
```
Also accepted: CSV text, column arrays, and a field mapping for the reserved columns:
`{ fields: { series: 'species', label: 'id' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `columns` | string[] | the first 6 numeric columns | columns to plot, in order (an unknown or text column is an input error) |
| `bins` | integer 2–60 | `10` | approximate number of histogram bins (nice thresholds) |

Every chart also takes the base options (`valueFormat`, `locale`, `theme`, `onClick`, ... — see `core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('splom', el, penguinsCsv, { fields: { series: 'species', label: 'id' }, columns: ['bill_length_mm', 'flipper_length_mm', 'body_mass_g'] })
```

## Edge cases
Empty → empty state; no numeric column → "No numeric columns to plot". One row → one
point per scatter and a single bar per histogram (domains are widened, never zero-width).
All zeros → points stack at zero. Nulls → a row is left out of every plot that needs the
missing value (noted). Duplicates are kept as distinct, overlapping points. 500 rows ×
4 columns → 6,000 points on canvas. Long and RTL column names truncate under the grid
and along the left, with the full name on hover; the legend ellipsises long series names.
Outliers stretch their column's scale (the other columns are unaffected).
