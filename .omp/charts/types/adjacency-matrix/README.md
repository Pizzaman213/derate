# Adjacency matrix (`adjacency-matrix`)

Links between the members of one set as an n × n grid: row = source, column = target, the
cell's colour = the link's weight.

## When to use
- Dense networks (who emails whom, service-to-service calls, co-authorship) where a
  node-link diagram turns into a hairball.
- Finding clusters: with `order: 'cluster'` (or `degree`) blocks along the diagonal appear.
- Directed relationships where a → b and b → a differ.

## When not to use
- Sparse graphs where paths matter: `network` / `force-graph` show routes, a matrix hides them.
- Flows between stages: `sankey`. Flows between groups around a circle: `chord`.
- One measure across two *different* sets of categories: `heatmap`.
- Correlations between numeric columns: `correlation-matrix`.

## Input
`{ nodes?, links }`. `links` are `{ source, target, value? }` (no `value` → weight 1;
`null` → a link of unknown weight, drawn hatched). `nodes` are optional `{ id, label?, group? }`:
they add isolated nodes, display names and the groups used by `order: 'cluster'` and
`color: 'group'`. Endpoints not listed in `nodes` are added in first-seen order.
```json
{ "links": [{ "source": "Alice", "target": "Bob", "value": 12 }, { "source": "Bob", "target": "Alice", "value": 8 }] }
```
Also accepted: link rows on their own (`[{ source, target, value }]`), CSV text, column
arrays (`{ source: [...], target: [...], value: [...] }`), a field mapping for link columns
(`{ fields: { source: 'caller', target: 'callee', value: 'rpm' } }`), and an adjacency
matrix `{ names: [...], matrix: [[...]] }` (zero cells are no link).
Repeated links are summed and noted.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `order` | `input \| name \| degree \| cluster` | `input` | node order: as given, by name, by number of neighbours, or grouped by `group` (most connected first inside a group; boundaries drawn) |
| `symmetric` | boolean | `false` | undirected: a link fills (a, b) and (b, a); both directions are summed |
| `color` | `value \| group` | `value` | a ramp over the weight, or the group colour for links inside a group (grey between groups; opacity follows weight) |
| `colorScale` | `linear \| log` | `linear` | `log` keeps light links visible next to heavy ones |
| `showValues` | boolean | `true` | weights in cells when every one fits |

Base options are in `core/fields.ts`.

## Example
```ts
import { chart } from '.omp/charts'
chart('adjacency-matrix', el, csv, { fields: { source: 'caller', target: 'callee', value: 'rpm' }, order: 'degree', colorScale: 'log' })
```

## Edge cases
Empty → empty state. One link → 2 nodes, 4 cells. Zero weights → drawn as links (unlike "no
link"). Negative weights → diverging ramp. Unknown weights → hatched and noted. Duplicates →
summed and noted. 75 nodes (5,625 cells) → canvas. Long/RTL names → truncated, full name on hover.
Every one of the n² cells is a focusable mark: "Alice → Bob: 12" or "Bob → Carol: no link".
