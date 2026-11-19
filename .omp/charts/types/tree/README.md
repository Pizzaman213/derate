# Tree (`tree`)

A tidy tree (Reingold–Tilford): every node placed by depth, curved links from parent to child.

## When to use
- Showing the structure of a hierarchy: who contains what, how deep, how bushy.
- Up to a few hundred named nodes.

## When not to use
- Sizes matter more than structure: `treemap`, `circle-packing` or `sunburst`.
- Many levels in little width: `radial-tree`.
- Clustering results with merge distances: `dendrogram`.
- People and titles: `org-chart`.

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
| `orientation` | `horizontal \| vertical` | `horizontal` | root at the left, or at the top |
| `maxDepth` | integer ≥ 1 | — | collapse everything deeper; collapsed nodes say how many they hide |
| `colorBy` | `single \| branch` | `single` | one colour, or one per top-level branch |
| `separator` | string | `/` | path separator for path rows |

Every chart also takes the base options (`valueFormat`, `locale`, `theme`, `patterns`, `title`,
`onClick`, ... — see `core/fields.ts`).

## Example
```ts
chart('tree', el, 'emp,boss\nceo,\ncto,ceo', { fields: { id: 'emp', parent: 'boss' }, orientation: 'vertical' })
```

## Edge cases
Empty → empty state. Single → one node. Duplicates → sibling names kept apart (keys suffixed) + note.
Nulls → names fall back to the id. Zeros/outliers → the layout ignores values (tooltips show them).
5,200 nodes → canvas. Long and RTL names → truncated, full text on hover. Deep → `maxDepth` collapses
with a count on the collapsed node. Sixty siblings → labels thinned so none collide.
