# Progress bar (`linear-progress`)

Horizontal progress bars with labels and percentages. A missing value is a dashed, empty track that says “No data” — never a 0% bar — so “not reported” and “nothing done” cannot be confused.

## When to use
- Progress of several items toward their goals (downloads, migrations, checklists).
- Breaking one bar's progress into parts (`segments` as parts) or steps (`segments` as a count).

## When not to use
- One headline number: `progress-ring` or `kpi-tile`.
- Values without a goal: `horizontal-bar`.
- Readings in good/bad zones: `meter`; against limits with a target: `gauge`.

## Input
`{ value, max?, label?, segments? }`; `max` defaults to 100. `segments` is either a step count (1–100: the track is cut into equal steps) or parts `[{ label, value }]` stacked in order — then `value` may be omitted and defaults to their sum; parts share a colour by label across bars and get a legend.
```json
[{ "label": "Platform", "value": 82 }, { "label": "Research", "value": null }]
```
Also accepted: a list of them, CSV text or column arrays (one item per row), and any column names through a field mapping, e.g. `{ fields: { label: 'node', value: 'mem_used_pct' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `showValues` | `percent \| value \| both` | `percent` | the right-hand readout: “68%”, “34 / 50”, or both |

Every chart also takes the base options (`valueFormat`, `locale`, `currency`, `theme`, `patterns`, `title`, `onClick`, ... — see `core/fields.ts`).

## Example
```ts
chart('linear-progress', el, csv, { fields: { label: 'model', value: 'fetched_gb', max: 'size_gb' }, showValues: 'both' })
```

## Edge cases
Empty → empty state. Zero → a solid empty track reading “0%”. Null → a dashed empty track reading “No data”, noted.
Negative → drawn empty, noted. Past the goal → drawn full in a darker shade, the readout keeps the true %, noted.
Duplicates → distinct rows. Many bars → as many rows as fit, then “+N more” (all are in the table).
Narrow widths put the label above the track. Long labels truncate with the full text on hover.
