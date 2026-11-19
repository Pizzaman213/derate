# Icicle chart (`icicle`)

Adjacent bands, one per level of a hierarchy: each node spans its share of its parent (rectangular partition). Levels run left to right by default.

## When to use
- Hierarchies where every level matters and names are long (straight bands give labels more room than a `sunburst`).
- Following a path from the whole down to a part (channel → source → campaign).

## When not to use
- Comparing leaf sizes: `treemap` uses the whole area for the leaves.
- One level only: `bar`, `stacked-bar-100` or `pie`.
- Negative values: they have no extent (omitted and noted).

## Input
A tree `{ name, value?, children? }`; leaves carry values and inner nodes are the sums of their children.
```json
{ "name": "Sessions", "children": [
  { "name": "Organic Search", "children": [{ "name": "Google", "value": 48210 }, { "name": "Bing", "value": 6120 }] },
  { "name": "Direct", "value": 22340 } ] }
```
Also accepted, as a list of rows, CSV text or column arrays, and through a field mapping:
path rows `{ path: 'Organic Search/Google', value }` (any `separator`, e.g. `' > '`), id/parent rows
`{ id, parent, value, name? }`, label rows `{ label, value, parent? }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `separator` | string | `/` | separator inside `path` values |
| `orientation` | `horizontal \| vertical` | `horizontal` | levels as columns (left to right) or rows (top to bottom) |
| `showValues` | boolean | `true` | value under each name where it fits |

Every chart also takes the base options (`valueFormat`, `locale`, `currency`, `theme`, `patterns`, `title`, `onClick`, ... — see `core/fields.ts`). The legend toggles first-level branches.

## Example
```ts
import { chart } from '.omp/charts'
chart('icicle', el, csvText, { fields: { path: 'channel_path', value: 'sessions' }, separator: ' > ', valueFormat: 'si' })
```

## Edge cases
Empty → empty state. All zeros → "All values are zero". Negatives and zeros → omitted, noted.
Missing leaf values → left out, noted. Duplicate paths → summed, noted. One node → one full band.
5,417 nodes → canvas. Long and RTL names → truncated in the band, in full in the tooltip.
