# Regression scatter plot (`regression-scatter`)

A scatter plot with a least-squares fit, its 95% confidence band, and the equation and R².

## When to use
- Stating a relationship's direction and strength, not just showing it.
- A curved trend: `degree: 2` or `3`.

## When not to use
- Groups with different trends: `scatter` with `series` (one fit per group is not supported).
- Over-time data where the order matters: `line` with a trend, or `connected-scatter`.
- Very high degrees or extrapolation: polynomials beyond cubic swing wildly outside the data.

## Input
Rows of `{ x, y, label? }`.
```json
[{ "x": 1, "y": 3.2 }, { "x": 2, "y": 4.8 }]
```
Also accepted: CSV text, column arrays, and a field mapping: `{ fields: { x: 'weight', y: 'mpg', label: 'model' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `degree` | `1 \| 2 \| 3` | `1` | straight line (`stats.linearRegression`) or polynomial (`stats.polynomialRegression`) |
| `showEquation` | boolean | `true` | equation, R² and n in the emptiest corner of the plot |
| `band` | boolean | `true` | 95% confidence band for the fitted mean (Student's t) |
| `xTitle`, `yTitle` | string | — | axis titles |

The fit is computed on a centred, scaled x (so year-sized x values stay well conditioned) and
the coefficients are converted back to raw x for the equation. The fit line is itself a data
mark: focus or hover it for the equation. The table adds a "Fitted" column. Zoom and brush: `zoom: true` (wheel/drag on both axes, double-click resets), `brush: true` with `onBrush`. Every chart also takes the base options (`valueFormat`, `locale`, `theme`, `patterns`, `title`, `onClick`, ... — see `core/fields.ts`).

## Example
```ts
chart('regression-scatter', el, csvText, { fields: { x: 'weight_1000lb', y: 'mpg', label: 'model' }, degree: 2 })
```

## Edge cases
Empty → empty state. Single, or fewer distinct x values than degree + 1 → points only, with a
note naming what the fit needs. Zeros → points only (no x variance). Negatives → drawn normally.
Nulls → skipped + note. Duplicates → kept (they weigh in the fit) and noted as overlapping.
5,200 points → canvas. Long titles → truncated; the equation box truncates last. Outliers →
stretched domain; the fit shows their leverage.
