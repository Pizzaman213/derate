# Control chart (`control-chart`)

Measurements in time order around their mean, with control limits at mean ± k·σ; points outside the limits are flagged as red diamonds.

## When to use
- Is a process stable? One value per period (daily defects, cycle time per ticket, fill weight
  per batch, p95 latency per deploy) — the classic XmR / individuals chart.
- Separating real signals (outside the limits) from routine variation (inside them) before
  anybody reacts to a single high reading.

## When not to use
- A trend toward a target or deadline: `burndown` / `burnup`.
- The shape of the distribution rather than its order: `histogram`, `box-plot`.
- Many series compared: `multi-line`.
- Strongly seasonal or trending data: the limits assume a stable mean — detrend first, or use `line`.

## Input
Rows of `{ x?, value, label? }`, one per measurement:
```json
[{ "x": "2026-03-02", "value": 50.2 }, { "x": "2026-03-03", "value": 49.1, "label": "Night shift" }]
```
- `x` is a **date** (ISO 8601, epoch, Date — any date in the column makes the whole column
  dates, so epoch numbers mixed with ISO strings compare correctly) or a **number** (sample /
  batch number). Leave it out everywhere to use the row order (`#1`, `#2`, ...). Rows are
  sorted by x; a row without x when others have one is skipped (noted); repeated x values are
  kept as separate measurements (noted).
- `value` may be `null`: skipped (noted). The moving range runs over the values that remain.
- `label` names the measurement in tooltips and announcements.

Also accepted: CSV text, column arrays (`{ value: [...] }` alone is enough), and field mapping:
`{ fields: { x: 'batch', value: 'fill_weight_g', label: 'lot' } }`.

### The limits
Computed by `core/stats.controlLimits` over the values in x order:
- `method: 'individuals'` (default, XmR): σ = mean moving range / 1.128.
- `method: 'sigma'`: σ = sample standard deviation.

UCL = mean + `sigmas`·σ, LCL = mean − `sigmas`·σ. A value strictly outside them is a signal:
filled with the theme's negative colour **and** drawn as a diamond, so it reads without colour;
its tooltip and announcement say which limit it crossed. Fewer than two values, or no variation
at all, gives the mean only, with a note.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `method` | `individuals \| sigma` | `individuals` | how σ is estimated |
| `sigmas` | number (0–6] | `3` | limit distance in σ |
| `zones` | boolean | `false` | shade the 1σ/2σ/3σ zones (C, B, A) |
| `line` | boolean | `true` | connect the measurements |
| `xTitle`, `yTitle` | string | — | axis titles |

Base options (`valueFormat`, `timeZone`, `zoom`, `brush`, `theme`, ...) are in `core/fields.ts`.

## Example
```ts
import { chart } from '.omp/charts'
chart('control-chart', el, csvText, {
  fields: { x: 'batch', value: 'fill_weight_g', label: 'lot' },
  zones: true, xTitle: 'Batch', yTitle: 'Fill weight (g)',
})
```

## Edge cases
Empty (or every value null) → empty state. One value → one dot, mean only, "control limits
need at least 2 values". All zeros → mean only, "no variation". Negatives and outliers stretch
the axis; an outlier is usually the signal. Nulls → skipped + note. Duplicates → kept + note.
5,200 values with no x → canvas, row-order axis. Long / RTL labels only appear in tooltips;
long axis titles truncate. Unsorted dates and mixed UTC offsets / epoch ms → sorted by instant.
