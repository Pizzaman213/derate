# Arc diagram (`arc-diagram`)

Nodes in a row along a baseline, every connection an arc above it: arc thickness is the
link weight, node size is the number of links, the order of the row is yours to choose.

## When to use
- Networks of tens of nodes where an order means something (time, module layers, groups):
  co-occurrence, dependencies, citations.
- Showing whether links stay local (short arcs) or jump across the order (tall arcs).
- Labels matter and must stay readable: every node has a slot on the line.

## When not to use
- The structure (clusters, hubs) matters more than any order: `force-network`.
- Quantities flowing through stages: `sankey`. Two-way volumes between groups: `chord`.
- Hundreds of nodes with dense links: `adjacency-matrix` scales better.

## Input
Any graph; links are undirected (a→b and b→a become one arc, summed and noted).
```json
{ "nodes": [{ "id": "Valjean", "group": "Protagonists" }],
  "links": [{ "source": "Valjean", "target": "Cosette", "value": 31 }] }
```
`nodes` is optional (ids named only by a link are added); `label` is the display name,
`group` colours nodes and their arcs and adds a legend. Also accepted: link rows, CSV text
(`source,target[,value]`), column arrays, any column names through
`fields: { source: 'module', target: 'imports' }`, and `{ names, matrix }`. Without values
the graph is unweighted (every arc the same thin stroke).

## Options
| Option | Type | Default | |
|---|---|---|---|
| `order` | `input \| degree \| group` | `input` | node order along the line: as given, most connected first, or grouped |

Base options: see `core/fields.ts`.

## Example
```ts
chart('arc-diagram', el, csvText, { fields: { source: 'module', target: 'imports' }, order: 'degree' })
```

## Edge cases
Empty → empty state. One link → two nodes and one arc. All weights zero → nodes only, links
left out with a note. Negative or missing weights → left out, noted. Reversed duplicates
→ merged into one arc, noted. Self-loops → left out, noted. 5,200 links → canvas. Labels
hang below the line rotated, truncate (full text on hover) and are thinned, most connected
first, where they would collide. The data table lists every kept link, then every node no
kept link touches (no target or value), so an all-zero graph still has rows.
