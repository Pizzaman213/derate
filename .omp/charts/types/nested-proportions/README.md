# Nested proportions (`nested-proportions`)

A part drawn inside its whole: squares sharing a corner, area proportional to value — "of 100 applicants, 55 were screened, 40 interviewed, 12 hired".

## When to use
- One whole and a chain of successively smaller parts (a funnel read as subsets).
- A whole and a few parts of it, each shown against the whole (one panel per part).

## When not to use
- Parts that add up to the whole and should be compared to each other: `treemap`, `pie` or `stacked-bar-100`.
- Conversion between stages with drop-off counts: `funnel`.
- Many parts: beyond `maxParts` the tail is grouped as "Other" — `bar` reads better.

## Input
Rows of `{ label, value, parent? }`. Without a `parent` column each row is a part of the row before it:
```json
[{ "label": "Applicants", "value": 100 }, { "label": "Interviewed", "value": 40 }, { "label": "Hired", "value": 12 }]
```
With `parent`, or as a tree `{ name, value, children }`, each node is a part of its parent; a whole with
several parts gets one panel per part, the whole repeated as context. Every node's own `value` is drawn
(parts need not add up to the whole); a node without a value is the sum of its parts, and a root without a
value is a label over independent wholes. Also accepted: CSV text, column arrays, a field mapping,
`{ id, parent, value }` rows and `{ path, value }` rows.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `maxParts` | integer 2–24 | `8` | parts shown per whole; the rest are grouped as "Other" (or left out, noted, when they overlap and their sum would exceed the whole) |
| `separator` | string | `/` | separator inside `path` values |

Every chart also takes the base options (`valueFormat`, `locale`, `currency`, `theme`, `title`, `onClick`, ... — see `core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('nested-proportions', el, csvText, { fields: { label: 'stage', value: 'count', parent: 'of' } })
```

## Edge cases
Empty → empty state. All zeros → "All values are zero". A negative or zero stage is omitted and noted,
and its parts move up to the whole above it. A part without a value is left out and noted. Duplicate
names are summed and noted. 40 parts → 7 panels plus "Other (33)", noted. A part larger than its
whole is drawn past it and noted. Long and RTL names are truncated beside the squares, in full in the
tooltip. A tiny share stays at least one pixel, with its percentage in the text.
