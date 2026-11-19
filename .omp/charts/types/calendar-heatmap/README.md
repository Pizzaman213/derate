# Calendar heatmap (`calendar-heatmap`)

Days laid out GitHub-style — weeks as columns, weekdays as rows, one block per year — shaded by value.

## When to use
- Daily activity over one or a few years: weekly rhythm, seasonal pushes, gaps.

## When not to use
- Sub-daily data or exact comparisons: `line` or `heatmap`.
- Many years: blocks get small; aggregate to weeks and use `heatmap`.

## Input
Rows of `{ date, value }`. Each instant is placed on its calendar day in `options.timeZone` (so 23:30 in New York is that New York day, across DST). Rows on the same day are summed (count events by giving each row value 1).
```json
[{ "date": "2026-03-03", "value": 12 }]
```
Also accepted: CSV text, column arrays, a field mapping: `{ fields: { date: 'committed_at', value: 'commits' }, timeZone: 'America/New_York' }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `weekStart` | `0 \| 1` | `0` | first row: Sunday (GitHub) or Monday (ISO) |

Every chart also takes the base options (`valueFormat`, `locale`, `timeZone`, `theme`, `patterns`, `title`, ...; see `core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('calendar-heatmap', el, csv, { fields: { date: 'committed_at', value: 'commits' }, timeZone: 'America/New_York', weekStart: 1 })
```

## Edge cases
Every day of each year from the first to the last data year is drawn; days without data or with a null value are empty track squares (nulls noted). Leap years get 366 cells; a year can span 54 columns. Duplicates summed + note. One-day data → one coloured square. Month labels skip when they would collide; weekday labels hide when rows are too short. Linear sequential scale (min…max, gradient legend). 5,200 days → canvas.
