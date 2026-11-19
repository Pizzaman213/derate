# Cumulative flow diagram (`cumulative-flow`)

How many items sit in each workflow state over time, stacked in workflow order with the last state ("Done") at the bottom.

## When to use
- Kanban/flow metrics: a band that widens is a growing queue (a bottleneck); the vertical
  distance across the in-progress bands is WIP; the horizontal distance between the top of
  a band and the top of "Done" is roughly lead time.
- Checking that arrivals (top edge) and departures (top of Done) run in parallel.

## When not to use
- Fixed-scope progress toward a date: `burndown` or `burnup`.
- Categories that are not stages of one workflow: `stacked-area` (or `stacked-area-100` for shares).
- One measure's stability over time: `control-chart`.

## Input
Long rows of `{ date, state, count }` — one row per (date, state):
```json
[{ "date": "2026-03-02", "state": "Backlog", "count": 25 }, { "date": "2026-03-02", "state": "Done", "count": 0 }]
```
or **wide** rows — one per date with a numeric column per state (how most trackers export it):
```csv
Day,Done,Testing,In Progress,Ready,Backlog
2026-09-01,13,4,6,5,38
```
Wide input is detected when no row has a `state` or `count` column: every column other than
`date` (and the columns named in the field mapping) whose values are all numeric becomes a
state. So `{ fields: { date: 'Day' } }` is all the CSV above needs.

Also accepted: column arrays (`{ date: [...], Backlog: [...], Done: [...] }`) and field mapping of
the long form (`{ fields: { date: 'day', state: 'status', count: 'n' } }`).

## Options
| Option | Type | Default | |
|---|---|---|---|
| `states` | string[] | first-seen order | workflow order, first stage first; the last one is stacked at the bottom; states not listed follow in first-seen order |
| `labels` | boolean | `true` | name each band at the right edge when it is tall enough (truncated, full name on hover) |
| `xTitle`, `yTitle` | string | — | axis titles |

Base options (`valueFormat`, `timeZone`, `zoom`, `brush`, `patterns`, `theme`, ...) are in
`core/fields.ts`. Toggling a state in the legend removes it and restacks the rest.

## Example
```ts
import { chart } from '.omp/charts'
chart('cumulative-flow', el, csvText, {
  fields: { date: 'Day' },
  states: ['Backlog', 'Ready', 'In Progress', 'Testing', 'Done'],
  yTitle: 'Work items',
})
```
Hovering anywhere inside a band shows that date's count for every state and the total; the
arrow keys walk the (date, state) segments. A state with no (or almost no) items on a date
still has a 6 px hover strip on its band edge.

## Edge cases
Empty → empty state; all counts zero → empty state "No items in any state". One date → a
single stacked column. Repeated (date, state) pairs are summed; a state missing on a date,
a null count and a negative count are each taken as 0 — all noted. 1,300 dates × 4 states
(given wide, as column arrays) → 5,200 segments on canvas. Long and RTL state names truncate
in band labels and legend, full text on hover. Unsorted dates and mixed UTC offsets / epoch ms
→ sorted by instant.
