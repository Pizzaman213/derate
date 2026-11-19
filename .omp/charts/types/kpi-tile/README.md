# KPI tile (`kpi-tile`)

A big number with its change against a previous value — absolute and percent, coloured by which direction is good, with an arrow drawn as a shape so the direction reads without colour — and an optional sparkline. A list becomes a grid of tiles that wraps to the width.

## When to use
- Dashboard headline numbers: revenue, throughput, latency, error rate.
- When the change since last period matters as much as the value.

## When not to use
- The history itself is the point: `line` or `sparkline`.
- Progress toward a goal: `progress-ring` or `linear-progress`.
- A reading against limits: `gauge`.

## Input
`{ label, value, previous?, unit?, format?, series?, goodDirection? }`.
`format` is this tile's number format (`auto | si | currency | percent | integer | number` or a d3-format specifier; default the chart's `valueFormat`). `auto` and `currency` keep full digits below a million.
`series` is the sparkline: numbers, or `{ x, y }` points sorted by x (numbers or dates). `goodDirection: 'down'` for latency, cost, errors.
```json
{ "label": "MRR", "value": 48200, "previous": 45100, "format": "currency", "series": [31200, 35800, 40200, 45100, 48200] }
```
Also accepted: a list of them, CSV text or column arrays (one item per row), and any column names through a field mapping, e.g. `{ fields: { label: 'node', value: 'mem_used_pct' } }`.
Percent-format changes are shown in percentage points (“−0.2 pp (−31%)”).

## Options
| Option | Type | Default | |
|---|---|---|---|
| `compareLabel` | string | `vs previous` | words after the change, e.g. “vs yesterday” (dropped when it does not fit) |

Every chart also takes the base options (`valueFormat`, `locale`, `currency`, `theme`, `patterns`, `title`, `onClick`, ... — see `core/fields.ts`).

## Example
```ts
chart('kpi-tile', el, [{ label: 'p95 latency', value: 412, previous: 388, unit: 'ms', goodDirection: 'down' }], { compareLabel: 'vs yesterday' })
```

## Edge cases
Empty → empty state. A change from 0 has no percentage (noted); 0 → 0 is “no change” with a flat bar glyph.
Negatives compare against |previous| (−12k → −4k is up 66.7%). Null value → “—”, noted; nulls in the series break the sparkline.
Duplicates → distinct tiles. Many tiles → as many as fit, then “+N more”. Long labels and values truncate with the full text on hover.
