# Dendrogram (`dendrogram`)

Every leaf on one baseline, elbow links; inner nodes at their merge distance when the data has `height`.

## When to use
- Results of hierarchical clustering (merge heights on a distance axis).
- Taxonomies where all leaves should line up for comparison.

## When not to use
- General org/structure where depth is the point: `tree`.
- Sizes: `treemap`, `circle-packing`.

## Input
A nested tree, `{ name, value?, height?, children? }` (`height` = merge distance):
```json
{ "name": "Animals", "children": [{ "name": "Mammals", "children": [{ "name": "Cat", "value": 12 }] }] }
```
Also accepted: id/parent rows (`[{ id, parent, name?, value? }]`, the root has an empty parent),
path rows (`[{ path: "Animals/Mammals/Cat", value: 12 }]`, split on `separator`), as CSV text or
column arrays, and any column names through a field mapping: `{ fields: { id: 'emp', parent: 'boss' } }`.
Cycles and unknown parents are errors that name the row.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `useHeight` | boolean | `true` | place inner nodes by `height` when present; `false`: by depth |
| `distanceTitle` | string | — | title under the distance axis |
| `maxDepth` | integer ≥ 1 | — | collapse everything deeper |
| `colorBy` | `single \| branch` | `single` | one colour, or one per top-level cluster |
| `separator` | string | `/` | path separator for path rows |

Every chart also takes the base options (`valueFormat`, `locale`, `theme`, `patterns`, `title`,
`onClick`, ... — see `core/fields.ts`).

## Example
```ts
chart('dendrogram', el, { name: 'all', height: 4, children: [{ name: 'a' }, { name: 'b' }] }, { distanceTitle: 'Distance' })
```

## Edge cases
Empty → empty state. Single → one node. Duplicates → sibling names kept apart (keys suffixed) + note.
Nulls → names fall back to the id. Zeros/outliers → the layout ignores values (tooltips show them).
5,200 nodes → canvas. Long and RTL names → truncated, full text on hover. Deep → `maxDepth` collapses
with a count on the collapsed node. A parent lower than its child, or a missing height, is placed at the child, with a note.
Outlier heights → a tall distance axis.
