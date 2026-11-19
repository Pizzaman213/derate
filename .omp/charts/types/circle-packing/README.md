# Circle packing (`circle-packing`)

Nested circles: a leaf's area is proportional to its value; a group's circle encloses its children.

## When to use
- Part-to-whole across a hierarchy where the grouping matters as much as the sizes.
- A friendlier, less exact alternative to a treemap.

## When not to use
- Precise size comparison: `treemap` (rectangles use space better) or `horizontal-bar`.
- Structure without sizes: `tree`.
- Negative values: they have no area.

## Input
A nested tree, `{ name, value?, children? }`:
```json
{ "name": "Animals", "children": [{ "name": "Mammals", "children": [{ "name": "Cat", "value": 12 }] }] }
```
Also accepted: id/parent rows (`[{ id, parent, name?, value? }]`, the root has an empty parent),
path rows (`[{ path: "Animals/Mammals/Cat", value: 12 }]`, split on `separator`), as CSV text or
column arrays, and any column names through a field mapping: `{ fields: { id: 'emp', parent: 'boss' } }`.
Cycles and unknown parents are errors that name the row.
Leaves carry `value`; inner values are the sum of their leaves.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `colorBy` | `branch \| depth` | `branch` | one hue per top-level group, or a ramp by depth |
| `showValues` | boolean | `true` | value under the name in circles big enough |
| `maxDepth` | integer ≥ 1 | — | collapse everything deeper; a collapsed circle keeps its total |
| `separator` | string | `/` | path separator for path rows |

Every chart also takes the base options (`valueFormat`, `locale`, `theme`, `patterns`, `title`,
`onClick`, ... — see `core/fields.ts`).

## Example
```ts
chart('circle-packing', el, [{ path: 'eng/api', value: 40 }, { path: 'eng/ui', value: 25 }])
```

## Edge cases
Empty → empty state. All zeros → empty state ("All values are zero"); no positive values at all → "No positive values to size the circles". Negatives and nulls → left
out with a note. Duplicates → kept as separate circles + note. Outliers → small circles stay
hoverable. 5,200 nodes → canvas. Long/RTL names → truncated inside their circle, full text on hover. Deep → `maxDepth` collapses (dashed outline, keeps its total). With 2-12 top-level groups the legend toggles a group and the rest re-pack.
