# Meter (`meter`)

HTML `<meter>` as a chart: a bar split into low, middle and high zones, each tinted by how good it is relative to the optimum, with the reading drawn in its verdict's colour (optimum / suboptimum / poor).

## When to use
- A reading whose meaning is its zone: disk usage, temperature, signal strength, battery charge.
- Several such readings in a list.

## When not to use
- Progress toward a goal: `linear-progress`. A dial with a target: `gauge`.
- Comparing values across categories: `horizontal-bar`.

## Input
`{ value, min?, max?, low?, high?, optimum?, label?, unit? }` with the HTML defaults and clamping: `min` 0, `max` 1, `low` = min, `high` = max, `optimum` = the midpoint; `low` is clamped to [min, max], `high` to [low, max].
Zones: below `low`, above `high`, or between (boundaries are in the middle zone). The zone holding the optimum is *optimum*, the next *suboptimum*, the far one *poor* (HTML “even less good”); with the optimum in the middle both outer zones are suboptimum.
```json
{ "label": "Disk /data", "value": 0.72, "low": 0.6, "high": 0.85, "optimum": 0.2 }
```
Also accepted: a list of them, CSV text or column arrays (one item per row), and any column names through a field mapping, e.g. `{ fields: { label: 'node', value: 'mem_used_pct' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `showZone` | boolean | `true` | append the verdict to the readout (“0.72 · suboptimum”) |

Every chart also takes the base options (`valueFormat`, `locale`, `currency`, `theme`, `patterns`, `title`, `onClick`, ... — see `core/fields.ts`).

## Example
```ts
chart('meter', el, csv, { fields: { label: 'metric', value: 'reading', low: 'lo', high: 'hi', optimum: 'best' } })
```

## Edge cases
Empty → empty state. Zero at the bottom of the scale → an empty fill, still hoverable. Negative scales work (dBm).
Null → a dashed empty meter reading “No data”, noted. Out of range → drawn at the end, noted. No low/high → one optimum zone.
Duplicates → distinct rows. Many meters → as many rows as fit, then “+N more”. Long labels truncate with the full text on hover.
