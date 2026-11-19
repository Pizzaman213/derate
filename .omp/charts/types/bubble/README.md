# Bubble chart (`bubble`)

A scatter plot whose dots are sized by a third measure: bubble **area** is proportional to `size`.

## When to use
- Two measures plus a magnitude per item (GDP, life expectancy and population).
- A few dozen to a few hundred items; groups as `series`.

## When not to use
- No third measure: `scatter`. Thousands of overlapping items: `hexbin` or `contour-density`.
- Sizes must be compared precisely: `bar` (areas are read less accurately than lengths).
- Negative magnitudes (profit/loss): `diverging-bar` — a bubble cannot have negative area.

## Input
Rows of `{ x, y, size, series?, label? }`.
```json
[{ "x": 12, "y": 4.1, "size": 320, "label": "Alpha" }]
```
Also accepted: CSV text, column arrays, and a field mapping:
`{ fields: { x: 'gdp_per_capita_usd', y: 'life_expectancy_years', size: 'population_m', series: 'region', label: 'country' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `maxRadius` | number (px, ≤ 80) | `28` | radius of the largest bubble; shrunk to 1/12 of the chart's smaller side |
| `xScale`, `yScale` | `linear \| log` | `linear` | log drops values ≤ 0 (noted) |
| `sizeLegend` | boolean | `true` | nested-circle key (1-2-5 values) above the plot, right-aligned |
| `labels` | `auto \| none` | `auto` | names inside bubbles that fit them, beside the rest when there is room |
| `xTitle`, `yTitle`, `sizeTitle` | string | — | axis titles; `sizeTitle` names the size key and tooltip row |

Radius is a square-root scale through zero (`r = maxRadius·√(size/max)`), so area ∝ size.
Bubbles are drawn largest first so small ones stay on top, and the axes are widened so no
bubble is cut by the plot edge. Zoom and brush: `zoom: true` (wheel/drag on both axes, double-click resets), `brush: true` with `onBrush`. Every chart also takes the base options (`valueFormat`, `locale`, `theme`, `patterns`, `title`, `onClick`, ... — see `core/fields.ts`).

## Example
```ts
chart('bubble', el, csvText, {
  fields: { x: 'gdp_per_capita_usd', y: 'life_expectancy_years', size: 'population_m', series: 'region', label: 'country' },
  xScale: 'log', sizeTitle: 'Population (millions)',
})
```

## Edge cases
Empty → empty state. Single → one bubble, padded axes. Zeros → size 0 is an empty ring (noted).
Negatives → negative x/y plot normally; negative sizes are omitted and noted. Nulls (x, y or
size) → skipped + note. Duplicates → distinct, overlapping bubbles. 5,200 bubbles → canvas.
Long/RTL labels and series → truncated, full text on hover. Outliers → one huge bubble, the rest
small but present (consider a log axis).
