# Sunburst (`sunburst`)

Concentric rings of a hierarchy: the root is the centre disc, each ring is one level, and every arc spans its share of its parent's angle.

## When to use
- A hierarchy where every level matters (region → country, channel → source → campaign).
- Showing how a whole breaks down in two or three steps, with the path to each part visible.

## When not to use
- Comparing leaf sizes precisely: `treemap` (area) or `bar` (length).
- Deep trees with long names: `icicle` has straight bands with more room for labels.
- A single level: `donut` or `pie`.
- Negative values: they have no angle (omitted and noted).

## Input
A tree `{ name, value?, children? }`; leaves carry values and inner nodes are the sums of their children.
```json
{ "name": "World", "children": [
  { "name": "Europe", "children": [{ "name": "Germany", "value": 4080 }, { "name": "France", "value": 2780 }] },
  { "name": "Japan", "value": 4230 } ] }
```
Also accepted, as a list of rows, CSV text or column arrays, and through a field mapping:
path rows `{ path: 'Europe/Germany', value }`, id/parent rows `{ id, parent, value, name? }`,
label rows `{ label, value, parent? }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `separator` | string | `/` | separator inside `path` values |
| `labels` | boolean | `true` | name the arcs with room for it (along the arc where it is wide, along the radius where it is narrow) |

Every chart also takes the base options (`valueFormat`, `locale`, `currency`, `theme`, `patterns`, `title`, `onClick`, ... — see `core/fields.ts`). The legend toggles first-level branches; the turn is re-divided among the rest.

## Example
```ts
import { chart } from '.omp/charts'
chart('sunburst', el, { id: [...], parent: [...], value: [...] }, { valueFormat: 'si' })
```

## Edge cases
Empty → empty state. All zeros → "All values are zero". Negatives and zeros → omitted, noted.
Missing leaf values → left out, noted. Duplicate paths → summed, noted. One node → one full disc.
5,417 arcs → canvas. Long and RTL names → truncated to the arc, in full in the tooltip; arcs too thin
for a label carry it in the tooltip only.
