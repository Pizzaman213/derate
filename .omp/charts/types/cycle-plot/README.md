# Cycle plot (`cycle-plot`)

Seasonal cycle plot: one mini line per season position (month, weekday or quarter) across years or weeks, with that position's mean as a horizontal rule.

## When to use
- Separating seasonality from trend: is every January higher than the last? Are Mondays growing faster than Fridays?

## When not to use
- One cycle of data: a plain `line` or `bar` by month.
- Irregular, non-seasonal series: `line`.

## Input
Rows of `{ date, value }`. Readings are grouped by position (month / quarter / weekday in `options.timeZone`) and cycle (the year; the week for `day-of-week`); several readings in the same position and cycle are averaged (noted).
```json
[{ "date": "2024-01-01", "value": 126 }]
```
Also accepted: CSV text, column arrays, a field mapping: `{ fields: { date: 'day', value: 'sessions' }, period: 'day-of-week' }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `period` | `month-of-year \| day-of-week \| quarter` | `month-of-year` | the season positions |
| `weekStart` | `0 \| 1` | `1` | weekday order and week grouping for `day-of-week` |
| `yTitle` | string | — | value axis title |

Every chart also takes the base options (`valueFormat`, `locale`, `timeZone`, `theme`, `patterns`, `title`, ...; see `core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('cycle-plot', el, csv, { fields: { date: 'day', value: 'sessions' }, period: 'day-of-week' })
```

## Edge cases
A cycle sits at the same x inside every panel; a missing cycle breaks that panel's line; positions without data stay empty (noted). Nulls skipped + note; duplicates averaged + note; unsorted and mixed-offset dates are grouped by instant. 5,200 daily readings by weekday → canvas.
