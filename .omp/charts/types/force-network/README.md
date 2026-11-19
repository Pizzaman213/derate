# Network graph (`force-network`)

Nodes and links laid out by a force simulation, so clusters, hubs and bridges show up as
shape. Node area is the number of links (or each node's value), colour is the group.

## When to use
- The structure of a network: communities, central people, bridges between teams,
  route networks, dependency clusters.
- Tens to a few thousand nodes where the overall shape matters more than any single link.

## When not to use
- Reading individual links or labels exactly: `arc-diagram` (every node has a labelled
  slot) or `adjacency-matrix`.
- Quantities flowing through stages: `sankey`. Two-way volumes between a few groups: `chord`.
- A tree: `tree` or `radial-tree`.

## Input
Any graph; links are undirected (a→b and b→a merge, summed and noted).
```json
{ "nodes": [{ "id": "Ana", "group": "Engineering", "value": 3 }],
  "links": [{ "source": "Ana", "target": "Ben", "value": 12 }] }
```
`nodes` is optional (ids named only by a link are added); `label` is the display name,
`group` colours nodes and adds a legend (ungrouped graphs use one colour), `value` sizes
nodes with `sizeBy: 'value'`. Also accepted: link rows, CSV text (`source,target[,value]`),
column arrays, any column names through `fields: { source: 'origin', target: 'destination', value: 'flights' }`,
and `{ names, matrix }`. Link width follows the weight when there is one.

## Determinism
The simulation runs synchronously for a fixed number of ticks (`iterations`, 300) from
starting positions drawn from the chart's seeded random source, in its own unit space, and
is then scaled uniformly into the box. Above ~320 nodes the tick count shrinks as 96,000 / n
(never below 40), with less friction and a coarser far-field charge, so 1,200 nodes still
lay out in well under 500 ms with their communities separated. Same data + same `seed` →
the same picture, at every size; a resize rescales rather than re-simulates. Change `seed`
for a different arrangement.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `sizeBy` | `degree \| value` | `degree` | node area: number of links, or `nodes[].value` |
| `labels` | integer ≥ 0 | `10` | label at most this many nodes, largest first |
| `iterations` | integer 1–1000 | `300` | simulation ticks (scaled down for large graphs, see above) |

`seed` (base option) picks the arrangement. Other base options: see `core/fields.ts`.

## Example
```ts
chart('force-network', el, csvText, { fields: { source: 'origin', target: 'destination', value: 'weekly_flights' }, labels: 14, seed: 3 })
```

## Edge cases
Empty → empty state. One node → one dot in the middle. All weights zero, negative or
missing → those links left out with a note (nodes still drawn). Duplicate and reversed
links → summed, noted. Self-loops → left out, noted. 1,200 nodes and 4,000 links → canvas.
The data table lists every kept link, then every node no kept link touches (no target or
value), so a lone node or an all-zero graph still has rows.
Labels go right, left, above or below their node, truncate (full text on hover) and are
dropped where they would collide; the summary counts separate components.
