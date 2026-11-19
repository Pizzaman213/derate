# Sparkline (`sparkline`)

A word-sized line with no axes: the shape of a series beside a number or inside a table cell.

## When to use
- The trend of a KPI next to its current value (dashboards, table cells).
- Many small series where only the shape matters.

## When not to use
- Reading exact values or dates: use `line` (it has axes).
- Comparing several series on one scale: `line` with series, or `small-multiples`.

## Input
Rows of `{ x?, y }`. `x` is a date (sorted by time) or a number (an index, sorted); without `x` the rows are taken in order. `y` may be null (a gap).
```json
[{ "x": "2026-01-01", "y": 10 }, { "x": "2026-02-01", "y": 8 }]
```
Also accepted: CSV text, column arrays (`{ "y": [3, 5, 4] }`), and a field mapping: `{ fields: { x: 'day', y: 'dau' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `showMinMax` | boolean | `true` | dots on the lowest (negative colour) and highest (positive colour) points |
| `showLastValue` | boolean | `false` | the last value printed at the right end |
| `area` | boolean | `false` | a light fill under the line |

Every chart also takes the base options (`valueFormat`, `locale`, `timeZone`, `theme`, `patterns`, `title`, ...; see `core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('sparkline', el, rows, { fields: { x: 'day', y: 'dau' }, showLastValue: true })
```

## Edge cases
Default height is word-sized: `aspect` 0.1 clamped to 32–48px (120px wide → 32px). Empty / all-null → empty state. One point → a centred dot. All equal → a flat midline, no min/max dots. Nulls → gaps + note. Repeated x → kept, noted. Unsorted and mixed-offset dates are sorted by instant. 5,200 points → canvas. `edge-index` has no x column.
