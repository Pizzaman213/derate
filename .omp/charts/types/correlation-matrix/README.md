# Correlation matrix (`correlation-matrix`)

Pairwise correlation coefficients between numeric variables, one cell per pair, on a
diverging −1…+1 colour scale with the coefficient printed where the cell is big enough.

## When to use
- Screening which of a handful to a few dozen numeric variables move together.
- Showing a coefficient matrix you already have (factor loadings, a model's feature correlations).

## When not to use
- Two variables: show the points with `scatter` -- one coefficient hides the shape.
- You need the shape of every pair, not a number: `splom`.
- Non-linear or clustered relationships: r near 0 can hide a strong curve; use `splom`.
- Counts of co-occurrence between categories: `heatmap` or `chord`.

## Input
Wide rows, one numeric column per variable. Text columns are ignored (and noted);
`null`, `''` and absent cells are skipped pairwise.
```json
[{ "height": 162, "weight": 61, "age": 27 }, { "height": 171, "weight": 70, "age": 34 }]
```
Also accepted: CSV text, column arrays (`{ "height": [...], "weight": [...] }`), or a
ready matrix: `{ "names": ["a", "b"], "matrix": [[1, 0.4], [0.4, 1]] }` (square, every
value in [−1, 1], `null` = not defined).

## Options
| Option | Type | Default | |
|---|---|---|---|
| `method` | `pearson \| spearman` | `pearson` | coefficient computed from rows (ignored for a given matrix) |
| `columns` | string[] | every numeric column | which columns to correlate, in this order |
| `triangle` | `full \| lower` | `full` | `lower` shows each pair once, plus the diagonal |
| `showValues` | boolean | `true` | print r in each cell (dropped when cells are too small) |

Every chart also takes the base options (`locale`, `theme`, `title`, `onClick`, ... — see `core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('correlation-matrix', el, csvText, { method: 'spearman', columns: ['mpg', 'weight', 'horsepower'] })
```

## Edge cases
Empty → empty state ("No numeric columns to correlate"). One column → a single diagonal
cell. All zeros (no variation) → off-diagonal cells are "not defined" (track colour, noted).
Negatives → unchanged (r is scale-free). Nulls → pairwise-complete, noted. Duplicated rows
count twice. Outliers pull Pearson r (use `method: 'spearman'`). A 72 × 72 matrix (5,184
cells) → canvas, labels thinned. Long and RTL names truncate, full text on hover.
