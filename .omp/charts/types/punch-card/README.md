# Punch card (`punch-card`)

Activity by weekday and hour of day: a 7 × 24 grid of circles whose area is proportional to
the value.

## When to use
- Weekly rhythm of events: commits, support tickets, orders, logins, page views.
- Raw timestamps you have not bucketed yet -- the chart buckets them in `timeZone`.

## When not to use
- One value per calendar day over months: `calendar-heatmap`.
- Two arbitrary categorical dimensions: `heatmap` (colour reads better than area when the grid is dense).
- The trend over the whole period: `line` or `area`.

## Input
Either pre-bucketed rows `{ day, hour, value }` -- `day` a weekday name (`Mon`, `Tuesday`),
`0`-`6` with Sunday = 0, or a date; `hour` 0-23 -- or raw timestamps `{ date, value? }`.
Timestamps are bucketed by weekday and hour in `timeZone` (default UTC); a timestamp
without an offset is wall-clock time in that zone. A row without `value` counts once.
```json
[{ "day": "Mon", "hour": 9, "value": 14 }, { "day": "Mon", "hour": 10, "value": 22 }]
[{ "date": "2026-09-08T16:04:11Z" }, { "date": "2026-09-08T17:30:02Z" }]
```
Also accepted: CSV text, column arrays (`{ "date": [1767225600, ...] }`), and a field mapping
(`{ fields: { date: 'committed_at' } }`).

## Options
| Option | Type | Default | |
|---|---|---|---|
| `weekStart` | `monday \| sunday` | `monday` | first row |
| `dayFormat` | `short \| long` | `short` | weekday names in the chart `locale` |
| `color` | `single \| value` | `single` | one colour (area carries the value), or a colour ramp as well, with a gradient legend |
| `xTitle` | string | — | title under the hour axis |

Base options (`timeZone`, `locale`, `valueFormat`, ...) are in `core/fields.ts`.

## Example
```ts
import { chart } from '.omp/charts'
chart('punch-card', el, gitLogCsv, { fields: { date: 'committed_at' }, timeZone: 'America/Los_Angeles', color: 'value' })
```

## Edge cases
Empty → empty state. One bucket → one circle. Zeros → 1 px dots (still focusable). Negatives
→ omitted and noted (an area cannot be negative). Nulls → skipped and noted. Duplicate
day/hour rows → summed and noted (summing timestamps is the point, so no note there).
6,000 timestamps → at most 168 circles. Long (`pt-BR`) and Arabic (`ar-EG`) weekday names size
the axis. Outliers → area scaling keeps the rest visible as dots. Unsorted timestamps and
mixed UTC offsets land in the same buckets as their instants.
