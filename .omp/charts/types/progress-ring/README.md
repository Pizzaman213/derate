# Progress ring (`progress-ring`)

Rings that fill clockwise from 12 o'clock toward a goal, with the percentage in the middle.

## When to use
- Progress toward a goal: sprint points, downloads, quotas.
- A few related goals at once (`layout: 'concentric'` nests them with a key).

## When not to use
- Many items or precise comparison: `linear-progress` (lengths compare better than angles).
- A reading against bands or limits: `gauge` or `meter`.
- Parts of a whole: `donut` or `pie`.

## Input
`{ value, max?, label? }`; `max` defaults to 100 and must be positive.
```json
{ "value": 34, "max": 50, "label": "Sprint 42" }
```
Also accepted: a list of them, CSV text or column arrays (one item per row), and any column names through a field mapping, e.g. `{ fields: { label: 'node', value: 'mem_used_pct' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `layout` | `side-by-side \| concentric` | `side-by-side` | one ring each with its % in the middle, or nested rings with a key showing each % |

Every chart also takes the base options (`valueFormat`, `locale`, `currency`, `theme`, `patterns`, `title`, `onClick`, ... — see `core/fields.ts`).

## Example
```ts
chart('progress-ring', el, rows, { fields: { label: 'node', value: 'fetched_gb', max: 'size_gb' } })
```

## Edge cases
Empty → empty state. Zero → a sliver at 12 o'clock and “0%”. Negative → shown as 0%, noted.
Null → a dashed empty ring reading “—”, distinct from 0%, noted. Past 100% → a darker second lap (capped at 200%), the label keeps the true %, noted.
Duplicates → distinct rings. Many rings → as many as fit, then “+N more”. Long labels truncate with the full text on hover.
