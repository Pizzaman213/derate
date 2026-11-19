# Gauge (`gauge`)

A semicircular dial showing one value against its range, with status bands and a target tick.

## When to use
- One headline reading against known limits (memory in use, CPU, SLA attainment).
- A handful of such readings side by side (an array draws a grid of dials).

## When not to use
- Comparing many values precisely: `bar` or `linear-progress` (lengths read better than angles).
- Progress toward a goal with no bands: `progress-ring` or `linear-progress`.
- Zoned readings where only the zone matters: `meter`.
- Change over time: `kpi-tile` with a series, or `line`.

## Input
`{ value, min?, max?, target?, bands?, label?, unit? }`; `min` defaults to 0, `max` to 100; `max` must exceed `min`.
`bands` are `[{ to, status: 'good' | 'warning' | 'bad' }]`, each running from the previous band (or `min`) to `to`.
```json
{ "value": 72, "target": 80, "label": "GPU memory", "unit": "%",
  "bands": [{ "to": 60, "status": "good" }, { "to": 85, "status": "warning" }, { "to": 100, "status": "bad" }] }
```
Also accepted: a list of them, CSV text or column arrays (one item per row), and any column names through a field mapping, e.g. `{ fields: { label: 'node', value: 'mem_used_pct' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `display` | `fill \| needle` | `fill` | the arc fills to the value (bands tinted beneath), or a needle over solid bands |

Every chart also takes the base options (`valueFormat`, `locale`, `currency`, `theme`, `patterns`, `title`, `onClick`, ... — see `core/fields.ts`).

## Example
```ts
chart('gauge', el, { value: 72, target: 80, bands, label: 'GPU memory', unit: '%' })
```

## Edge cases
Empty → empty state. Zero → a sliver at the start (still hoverable), readout 0. Negative scales (min −40) work.
Null → a dashed empty dial reading “—”, noted. Out of range → clamped to the dial end, the readout keeps the true value, noted.
Duplicate labels → distinct dials. Many gauges → as many as fit, then a “+N more” cell (all are in the table).
Long labels and units truncate with the full text on hover; the readout shrinks to fit inside the arc.
