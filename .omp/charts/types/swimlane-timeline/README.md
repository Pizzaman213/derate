# Swimlane timeline (`swimlane-timeline`)

One horizontal lane per team, room, machine or person, with its items as bars on a shared
time axis. Items that overlap inside a lane are packed into sub-rows.

## When to use
- Who/what is busy when: conference rooms, on-call rotas, machine schedules, team plans.
- Comparing load across lanes (a busy lane is taller).

## When not to use
- Tasks with dependencies and progress: `gantt`.
- Quarter-level plans with status: `roadmap`.
- Events without an owner: `event-timeline`.

## Input
Rows of `{ lane, start, end, label, category? }`. A missing `end` (or `end == start`) is an
instant, drawn as a diamond.
```json
[{ "lane": "Design", "start": "2026-01-05", "end": "2026-01-23", "label": "Research", "category": "Discovery" },
 { "lane": "QA", "start": "2026-04-03", "label": "Go / no-go" }]
```
`end` before `start` is rejected with a message naming the row.
Also accepted: CSV text, column arrays, field mapping (`{ fields: { lane: 'room', start: 'begins', end: 'ends', label: 'session', category: 'track' } }`).

## Layout
Lanes keep first-seen order. Within a lane, items are packed greedily by start into the first
sub-row they fit (touching items share a row, with a 1px seam); each lane is as tall as its
sub-rows. Colour follows `category` (with a legend and toggling) or, without categories, the lane.
Labels print inside bars that are wide and tall enough, truncated with the full text on hover.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `showLabels` | boolean | `true` | item labels inside bars |

Base options (`timeZone`, `zoom`, `patterns`, …) — see `core/fields.ts`.

## Example
```ts
chart('swimlane-timeline', el, csv, { fields: { lane: 'room', start: 'begins', end: 'ends', label: 'session', category: 'track' }, timeZone: 'America/Chicago' })
```

## Edge cases
Empty → empty state. One item → one lane. Zero-length items → diamonds. Nulls → instants,
uncategorised, a null lane is "(blank)". Duplicates → kept, the copy takes a sub-row.
5,200 items → canvas, sub-rows thin to hairlines. Long/RTL names truncate with full text on
hover. A five-year outlier widens the axis. Unsorted rows are packed by time; mixed offsets
compare as instants.
