# Roadmap (`roadmap`)

Items as rounded bars in lanes (themes, teams, products) on a quarter axis, coloured by status.

## When to use
- Quarter-level product or portfolio plans: what ships in which quarter, and how it is going.
- Fiscal calendars (`fiscalYearStart`).

## When not to use
- Day-level schedules with dependencies: `gantt`.
- Who is busy when, at hour/day resolution: `swimlane-timeline`.
- Key dates only: `milestone-timeline`.

## Input
Rows of `{ item, lane, start, end?, status? }`; `phase` and `theme` are accepted as `item`
and `lane`.
```json
[{ "item": "Billing v2", "lane": "Platform", "start": "2026-Q1", "end": "2026-Q2", "status": "done" },
 { "item": "SSO", "lane": "Platform", "start": "2026-05-01", "end": "2026-08-31", "status": "at risk" }]
```
- `start`/`end`: a date, or a quarter — `2026-Q1`, `2026 Q1`, `Q1 2026`, `FY27 Q2`, `FY2027-Q2`.
  A start quarter means its first day; an end quarter means *through* that quarter. With
  `fiscalYearStart: 4`, `FY27 Q1` / `2027-Q1` is April–June 2026. Read in `timeZone`.
- Missing `end` → the end of the start quarter (noted).
- `status`: `done`, `in progress`, `at risk`, `blocked`, `planned` (and common synonyms:
  shipped, wip, delayed, on hold, backlog, exploring…) get the positive / accent / warning /
  negative / neutral colours; planned work is dashed. Any other word gets a categorical colour.
  Each spelling is a legend entry (toggle to hide).
- `end` before `start` is rejected with a message naming the row; `2026-Q5` is not a date.

Also accepted: CSV text, column arrays, field mapping.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `fiscalYearStart` | 1–12 | `1` | month the fiscal year starts; header reads `FY27` when not 1 |
| `showLabels` | boolean | `true` | item names inside bars that are wide enough |

Base options (`timeZone`, `zoom`, `patterns` — each status has its own pattern, …) — see `core/fields.ts`.

## Example
```ts
chart('roadmap', el, csv, { fiscalYearStart: 4 })   // CSV with theme,phase,start,end,status columns
```

## Edge cases
Empty → empty state. One item → one quarter. Same-day items → short stubs. Nulls → end of
quarter (noted), "(no status)", "(blank)" lane. Duplicates → kept on their own sub-row.
5,200 items → canvas, quarter labels give way to years. Long/RTL names truncate with full
text on hover. A ten-year item widens the axis to years. Unsorted rows keep lane order;
mixed offsets compare as instants; quarters start at midnight in `timeZone`.
