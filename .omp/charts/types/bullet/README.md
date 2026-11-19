# Bullet chart (`bullet`)

Stephen Few's bullet graph: a measure as a bar, its target as a tick, against qualitative
bands (poor / satisfactory / good) — a gauge in a strip. One bullet per row, stacked.

## When to use
- KPIs against targets on a dashboard: revenue vs plan, margin vs goal, NPS vs benchmark.
- Several measures in different units at once (each bullet gets its own axis).

## When not to use
- A single headline number with no target or bands: `kpi-tile`.
- Comparing one measure across many categories: `bar` or `lollipop`.
- Progress toward one goal as a share: `linear-progress`, `progress-ring` or `radial-bar`.

## Input
Rows of `{ label, value, target?, ranges? }`. `ranges` are the **upper bounds** of the
bands, low to high (`[150, 225, 300]` → 0–150 poor, 150–225 satisfactory, 225–300 good).
```json
[{ "label": "Revenue", "value": 270, "target": 250, "ranges": [150, 225, 300] }]
```
Also accepted: CSV text — write the bands in one cell as `150;225;300` (or `150|225|300`,
`150 225 300`, `"[150, 225, 300]"`) — column arrays, and a field mapping:
`{ fields: { label: 'kpi', value: 'actual', target: 'goal', ranges: 'bands' } }`.

Missing parts degrade: no `ranges` → a plain track; no `target` → no tick; no `value` → the
bands and tick only (noted). The axis always includes zero, the value, the target and
every band edge.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `scale` | `independent \| shared` | `independent` | each bullet its own axis (different units), or one axis under all |
| `showValues` | boolean | `true` | `value / target` under each label (when the row is tall enough) |

Paints: bands from `theme.grid` (lowest, leaning toward `theme.muted`) to `theme.track`
(highest), the measure in `theme.cat(0)`, the target in `theme.ink`. Every chart also takes
the base options (see `core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('bullet', el, csv, { fields: { label: 'kpi', value: 'actual', target: 'goal', ranges: 'bands' } })
```

## Edge cases
Empty → empty state. Single → one bullet. Zeros → a 1px measure on zero. Negatives → the
axis extends below zero, the bar grows left from a zero rule. Nulls → bands/target only,
plain track, or no tick, as above, with a note. Duplicates → separate bullets.
**Large** (200 rows) → only the rows that fit at a readable height are drawn (about 36px a
row with its own axis, 18px shared); the rest are noted ("N more rows did not fit") and
every row stays in the table — collapsing KPIs into an "Other" bullet would be meaningless.
Long and RTL labels truncate with the full text on hover. Outliers → that row's own axis.
