# Hexbin plot (`hexbin`)

Points counted into hexagonal bins on the x/y plane, each hexagon coloured by its count.

## When to use
- Thousands to millions of points where a scatter plot becomes a solid smear.
- Seeing where the mass of a 2-D distribution is, with exact counts per bin.

## When not to use
- Fewer than ~100 points: `scatter` (every point is readable).
- A smooth estimate of shape: `contour-density`.
- One measure's distribution: `histogram`.

## Input
Rows of `{ x, y }`.
```json
[{ "x": 0.42, "y": -1.3 }, { "x": 1.1, "y": 0.2 }]
```
Also accepted: CSV text, column arrays (`{ "x": [...], "y": [...] }`), and a field mapping: `{ fields: { x: 'eruption_minutes', y: 'waiting_minutes' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `radius` | number (px, 3-60) | auto | hexagon radius; default about 1/40 of the plot width, 5-16px |
| `colorScale` | `linear \| log` | `linear` | count → colour; log keeps sparse bins visible next to very dense ones |
| `xTitle`, `yTitle` | string | — | axis titles |

Bins are computed in screen space after zoom, so zooming in gives finer bins. Colour runs
along the theme's sequential ramp with a gradient legend (1 to the densest bin). Zoom and brush: `zoom: true` (wheel/drag on both axes, double-click resets), `brush: true` with `onBrush`. Every chart also takes the base options (`valueFormat`, `locale`, `theme`, `patterns`, `title`, `onClick`, ... — see `core/fields.ts`).

## Example
```ts
chart('hexbin', el, { x: xs, y: ys }, { radius: 8, colorScale: 'log' })
```

## Edge cases
Empty → empty state. Single / all zeros → one hexagon. Negatives → axes cross zero. Nulls →
skipped + note. Duplicates → all counted (noted). Large → hundreds of small bins; counts always
sum to the number of drawable points. Long/RTL axis titles → truncated, full text on hover.
Outliers → most points share a few bins; use `zoom` to look inside.
