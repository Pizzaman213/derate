# Connected scatter plot (`connected-scatter`)

Points at (x, y) joined in input order, so the path shows how two measures moved together over time.

## When to use
- Two measures over the same periods, when their joint path matters (unemployment vs inflation by year).
- Up to a few dozen labelled steps; thousands also draw (canvas), labels then limited to the ends.

## When not to use
- The order carries no meaning: `scatter`.
- Each measure over time on its own: `multi-line` (easier for most readers).
- Many entities' paths at once: small multiples of this chart, or `slope`.

## Input
Rows of `{ x, y, label? }`, **in order** (the chart never sorts). `label` names the step, usually the period.
```json
[{ "x": 9.6, "y": 1.6, "label": "2010" }, { "x": 8.9, "y": 3.2, "label": "2011" }]
```
Also accepted: CSV text, column arrays, and a field mapping: `{ fields: { x: 'miles', y: 'price', label: 'year' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `curve` | `linear \| smooth` | `linear` | straight segments, or a uniform Catmull-Rom curve through every point |
| `arrows` | boolean | `true` | direction arrows spaced along the path (about two dozen at most) |
| `labels` | `ends \| auto \| none` | `auto` | first and last step always; `auto` adds the other steps where they fit |
| `xTitle`, `yTitle` | string | — | axis titles |

The first step is a hollow ring, the last a larger filled dot; both are announced as
"(start)" / "(end)". Zoom and brush: `zoom: true` (wheel/drag on both axes, double-click resets), `brush: true` with `onBrush`. Every chart also takes the base options (`valueFormat`, `locale`, `theme`, `patterns`, `title`, `onClick`, ... — see `core/fields.ts`).

## Example
```ts
chart('connected-scatter', el, rows, { fields: { x: 'unemployment', y: 'inflation', label: 'year' }, curve: 'smooth' })
```

## Edge cases
Empty → empty state. Single → one dot, no path. Zeros → every step at the origin. Negatives →
axes cross zero. Nulls → the path breaks at the missing row (noted). Duplicates → kept as
consecutive steps. 5,200 steps → canvas, arrows thinned by distance. Long/RTL labels → end labels
truncated, full text on hover. Outliers → stretched domain.
