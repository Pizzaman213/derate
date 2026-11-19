# Sankey diagram (`sankey`)

Flows between stages as bands whose width is the quantity moved; each node's bar is as
tall as everything passing through it.

## When to use
- Where a quantity goes: energy from sources to uses, budget lines to departments,
  sessions from channel to page to outcome.
- Multi-step flows where conservation matters (what comes in goes out).

## When not to use
- The flow loops back (A → B → A): use `chord` or `force-network`. A cycle is rejected
  with the loop named.
- Fixed categorical stages over the same population (survey answers, cohorts): `alluvial`.
- Connections without a quantity: `arc-diagram` or `force-network`.
- One step from a few sources to a few targets: `stacked-bar` or `table-heatmap` reads faster.

## Input
A directed acyclic graph. `nodes` is optional: ids only named by a link are added. `label`
is the display name, `group` colours nodes and their outgoing links and adds a legend.
```json
{ "nodes": [{ "id": "Coal", "group": "Fossil" }],
  "links": [{ "source": "Coal", "target": "Electricity", "value": 120 }] }
```
Also accepted: link rows `[{ source, target, value }]`, CSV text with those columns, column
arrays, any column names through `fields: { source: 'from', target: 'to', value: 'sessions' }`,
and an adjacency matrix `{ names, matrix }` (row = source). Without any `value`, every link
counts 1.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `nodeAlign` | `justify \| left \| right \| center` | `justify` | column of each node: `justify` pushes sinks to the right edge, `left`/`right` pack toward a side, `center` balances |
| `showValues` | boolean | `false` | node total after each label |

Base options (`valueFormat`, `locale`, `theme`, `patterns`, `title`, `onClick`, ...): see `core/fields.ts`.

## Example
```ts
chart('sankey', el, csvText, { fields: { source: 'from', target: 'to', value: 'sessions' }, valueFormat: 'si' })
```

## Edge cases
Empty → empty state. All flows zero → empty state "All flows are zero". One link → two
nodes and a band. Negative or missing values → left out with a note. Duplicate links →
summed with a note. Nodes no link touches → left out with a note. 5,400 flows → canvas.
Labels sit beside their node inside the gap to the next column, truncate (full text on
hover) and are dropped where they would collide; the tooltip always has the name.
