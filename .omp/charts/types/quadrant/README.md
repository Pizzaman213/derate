# Quadrant chart (`quadrant`)

A scatter split into four named quadrants at a threshold on each axis (the medians by
default), with each quadrant's name in its corner and points labelled where they fit.

## When to use
- Prioritising items on two criteria: effort/impact ("Quick wins"), growth/share (BCG),
  usage/satisfaction, risk/likelihood.
- Sorting items into four actionable groups where the thresholds mean something.

## When not to use
- The relationship itself is the point (trend, correlation): `scatter` or `regression-scatter`.
- Thousands of unlabelled points: `hexbin` or `contour-density`.
- A third measure matters: `bubble` sizes points by it.

## Input
Rows of `{ label, x, y }`. `x` or `y` may be `null` (the row is left out and noted).
Duplicate labels are kept as separate points (noted).
```json
[{ "label": "SSO login", "x": 8, "y": 9 }, { "label": "Dark mode", "x": 2, "y": 8 }]
```
Also accepted: CSV text, column arrays, and any column names through a field mapping:
`{ fields: { label: 'account', x: 'monthly_active_seats', y: 'nps' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `xMid`, `yMid` | number | median of x / y | where the split lines sit (a point on a line counts as high) |
| `q1` … `q4` | string | "High x, high y" … | quadrant names: q1 top-right, q2 top-left, q3 bottom-left, q4 bottom-right |
| `showLabels` | boolean | `true` | label points where the label fits without overlapping (up to 300 points) |
| `colorBy` | `quadrant \| single` | `quadrant` | colour points (and tint the quadrants) by quadrant; the legend toggles them |
| `xTitle`, `yTitle` | string | — | axis titles, also used in the default quadrant names and announcements |

Every chart also takes the base options (`valueFormat`, `zoom`, `brush`, `theme`, `onClick`, ... — see `core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('quadrant', el, features, { xTitle: 'Effort', yTitle: 'Impact', q1: 'Major projects', q2: 'Quick wins', q3: 'Fill-ins', q4: 'Thankless tasks' })
```

## Edge cases
Empty → empty state; no row with both values → "No rows have both x and y". One row → one
point on both split lines (domains widen, never zero-width). Zeros → every point at the
origin. Negatives → axes extend below zero. Nulls → left out, noted. Duplicates → separate
points, noted. 5,200 points → canvas, no point labels. Long labels truncate (full text on
hover); RTL labels render. Outliers stretch the axis; zoom (`zoom: true`) to inspect the
cluster. The domain keeps a band free at the top and bottom so no point hides under a
quadrant name.
