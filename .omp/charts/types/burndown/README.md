# Burndown chart (`burndown`)

Work remaining over time, against the ideal straight line to zero, with scope changes flagged along the top.

## When to use
- Tracking a sprint, iteration or release with a fixed end: "are we going to finish?".
- Showing where scope was added or removed and how that moved the line.

## When not to use
- Scope changes a lot: a burndown hides them inside the remaining line — use `burnup`, which draws completed work and total scope separately.
- Flow through several workflow states: `cumulative-flow`.
- Process stability of one measure: `control-chart`.
- A plain metric over time with no target: `line`.

## Input
Rows of `{ date, remaining, ideal?, scopeChange?, note? }`, one per reading.
```json
[{ "date": "2026-03-02", "remaining": 80 }, { "date": "2026-03-03", "remaining": 76, "scopeChange": 5, "note": "Login bug added" }]
```
- `remaining` may be `null` (a gap in the line, noted).
- `ideal`: when **no** row has one, the ideal line is computed: straight from the first non-null
  `remaining` (at its date) to zero at `end` (default: the last date). When any row has one, the
  given values are drawn as they are.
- `scopeChange` (+ added, − removed) and `note` put a flag on the date; the flag label is the
  note (prefixed with the change), truncated to the room before the next flag, full text on hover.

Also accepted: CSV text, column arrays, and any column names through a field mapping:
`{ fields: { date: 'day', remaining: 'hours_left', scopeChange: 'added', note: 'event' } }`.
Rows are sorted by date; the same date twice is merged (the last reading is kept, scope changes
add up) and noted.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `end` | date (ISO string, epoch, Date) | last date | sprint/release end: the computed ideal reaches zero there and the x axis runs to it |
| `area` | boolean | `true` | shade under the remaining line |
| `xTitle`, `yTitle` | string | — | axis titles |

Base options (`valueFormat`, `timeZone`, `locale`, `zoom`, `brush`, `theme`, `title`, `onClick`, ...)
are in `core/fields.ts`. `zoom: true` zooms and pans the time axis.

## Example
```ts
import { chart } from '.omp/charts'
chart('burndown', el, csvText, {
  fields: { date: 'day', remaining: 'hours_left', scopeChange: 'added', note: 'event' },
  end: '2026-05-08', yTitle: 'Hours remaining',
})
```

## Edge cases
Empty (or no `remaining` at all) → empty state. One reading → one dot, axis ±1 day. All zeros →
flat line on zero. Negatives (over-burn) are drawn below zero. Nulls → gaps + note. Duplicate
dates → merged + note. 5,200 readings → canvas, dots become invisible hit targets. Long and
RTL notes → flag labels truncate, full text in the tooltip. Unsorted dates and mixed UTC
offsets / epoch ms → sorted by instant.
