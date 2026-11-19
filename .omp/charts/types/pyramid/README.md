# Pyramid chart (`pyramid`)

A triangle sliced into horizontal bands, one per level, apex first. Each band's **area** is
proportional to its value (not its height).

## When to use
- Levels of a hierarchy whose sizes widen toward the base: staff by grade, wealth bands,
  a population of tiers.

## When not to use
- Exact comparison: `bar` (areas of trapezoids are hard to compare).
- Two-sided age/sex pyramids: use a back-to-back bar chart.
- Stages people drop out of: `funnel`.

## Input
Rows of `{ label, value }`, apex first (or `order: 'base-first'`).
```json
[{ "label": "Executives", "value": 12 }, { "label": "Staff", "value": 940 }]
```
Also accepted: CSV text, column arrays, and a field mapping: `{ fields: { label: 'band', value: 'adults_m' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `order` | `apex-first \| base-first` | `apex-first` | whether the first row is the top or the bottom band |

Every chart also takes the base options (see `core/fields.ts`).

## Geometry
The area of a triangle above depth `t` (0 at the apex, 1 at the base) is `t²` of the whole,
so a band whose cumulative share ends at `c` ends at depth `√c`. Band areas are therefore
exactly the shares; equal values near the apex give taller bands than near the base.

## Example
```ts
chart('pyramid', el, csv, { fields: { label: 'band', value: 'adults_m' }, order: 'base-first' })
```

## Edge cases
Empty / all zeros → empty state. Negatives, nulls, zeros → omitted, noted. Duplicates → summed, noted.
40 levels → labels spread apart; the thinnest bands lose theirs (tooltips keep them); no legend above 12 levels.
Long labels → truncated (full on hover). The legend toggle removes a level and re-normalises the areas.
