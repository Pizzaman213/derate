# Treemap (`treemap`)

Nested rectangles whose areas are proportional to the values of a hierarchy (squarified layout), coloured by first-level branch.

## When to use
- Parts of a whole with many parts, or a two-to-three level hierarchy (budget by department and team, disk usage by folder).
- Spotting the few large items among many small ones.

## When not to use
- Precise comparison of similar values: `bar` or `horizontal-bar` (length reads better than area).
- The levels themselves matter more than the leaf sizes: `sunburst` or `icicle` show every level.
- A handful of parts with no hierarchy: `pie`, `donut` or `stacked-bar-100`.
- Negative values: they have no area (omitted and noted) — use `waterfall` or `diverging-bar`.

## Input
A tree `{ name, value?, children? }`; leaves carry values, every inner node is the sum of its children (its own value is ignored).
```json
{ "name": "Budget", "children": [
  { "name": "Engineering", "children": [{ "name": "Platform", "value": 180 }, { "name": "Mobile", "value": 120 }] },
  { "name": "Operations", "value": 55 } ] }
```
Also accepted, as a list of rows, CSV text or column arrays, and through a field mapping:
- path rows `{ path: 'Engineering/Platform', value }` (`separator` option, default `/`);
- id/parent rows `{ id, parent, value, name? }`;
- label rows `{ label, value, parent? }` (no parent: one flat level).

Several roots share a synthetic root named `All`. Sibling names that repeat (and repeated paths/labels) are summed and noted.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `separator` | string | `/` | separator inside `path` values |
| `showValues` | boolean | `true` | value under each name where it fits |
| `groupLabels` | boolean | `true` | first-level group names in their top-left corner (trees deeper than one level) |

Every chart also takes the base options (`valueFormat`, `locale`, `currency`, `theme`, `patterns`, `title`, `onClick`, ... — see `core/fields.ts`). The legend toggles first-level branches; the rest re-lays out to fill the area.

## Example
```ts
import { chart } from '.omp/charts'
chart('treemap', el, csvText, { fields: { path: 'folder', value: 'size_mb' } })
```

## Edge cases
Empty → empty state. All zeros → "All values are zero". Negatives and zeros → omitted with a note.
Missing leaf values → left out, noted. Duplicate paths → summed, noted. One node → one full rectangle.
5,200 leaves → canvas. Long and RTL names → truncated in the cell, in full in the tooltip; cells too
small for a label carry it in the tooltip only. An outlier leaves the rest as slivers (hover them).
