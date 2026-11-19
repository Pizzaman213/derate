# Radial tree (`radial-tree`)

A tidy tree in polar coordinates: the root in the centre, each depth a ring.

## When to use
- Large or bushy hierarchies that would be too tall as a `tree`.
- When the overall shape (balance, depth per branch) matters more than reading every label.

## When not to use
- Exact reading of labels or comparing depths: `tree`.
- Sizes: `sunburst` or `circle-packing`.
- Merge distances: `dendrogram`.

## Input
A nested tree, `{ name, value?, children? }`:
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
| `maxDepth` | integer ≥ 1 | — | collapse everything deeper |
| `colorBy` | `single \| branch` | `branch` | one colour, or one per top-level branch |
| `separator` | string | `/` | path separator for path rows |

Every chart also takes the base options (`valueFormat`, `locale`, `theme`, `patterns`, `title`,
`onClick`, ... — see `core/fields.ts`).

## Example
```ts
chart('radial-tree', el, [{ path: 'a/b', value: 1 }, { path: 'a/c', value: 2 }])
```

## Edge cases
Empty → empty state. Single → one node. Duplicates → sibling names kept apart (keys suffixed) + note.
Nulls → names fall back to the id. Zeros/outliers → the layout ignores values (tooltips show them).
5,200 nodes → canvas. Long and RTL names → truncated, full text on hover. Deep → `maxDepth` collapses
with a count on the collapsed node. Leaf labels point outward and are truncated to the distance to the edge.
