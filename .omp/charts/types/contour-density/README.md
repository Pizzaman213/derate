# Contour density plot (`contour-density`)

A kernel density estimate of points on the x/y plane, drawn as filled contour bands (darker = denser).

## When to use
- The shape of a 2-D distribution: peaks, clusters, ridges.
- Many overlapping points, where a smooth summary reads better than bins.

## When not to use
- Exact counts per region: `hexbin`.
- Few points (< ~30): the estimate is mostly the kernel; use `scatter`.
- One measure: `density` or `histogram`.

## Input
Rows of `{ x, y }`.
```json
[{ "x": 2.1, "y": 54 }, { "x": 4.4, "y": 80 }]
```
Also accepted: CSV text, column arrays, and a field mapping: `{ fields: { x: 'eruption_minutes', y: 'waiting_minutes' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `bandwidth` | number (px, 2-80) | `20` | Gaussian kernel bandwidth; larger is smoother |
| `thresholds` | integer (2-40) | `10` | approximate number of contour levels (round density values) |
| `showPoints` | `auto \| always \| never` | `auto` | the points on top; auto draws up to 2,000 |
| `xTitle`, `yTitle` | string | — | axis titles |

Each level is a data mark announcing its density (points per x·y unit²) and the share of
points it encloses. The axes are widened by about two bandwidths so the tails are not cut.
Zoom and brush: `zoom: true` (wheel/drag on both axes, double-click resets), `brush: true` with `onBrush`. Every chart also takes the base options (`valueFormat`, `locale`, `theme`, `patterns`, `title`, `onClick`, ... — see `core/fields.ts`).

## Example
```ts
chart('contour-density', el, csvText, { fields: { x: 'eruption_minutes', y: 'waiting_minutes' }, bandwidth: 24 })
```

## Edge cases
Empty → empty state. Single / all zeros → one circular blob. Negatives → axes cross zero.
Nulls → skipped + note. Duplicates → weigh twice in the estimate. Large → density only unless
`showPoints: 'always'` (5,200 points: canvas). Long/RTL titles → truncated, full text on hover.
Outliers → the bulk compresses into a corner; use `zoom`.
