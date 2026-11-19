# Gantt chart (`gantt`)

Tasks as bars on a time axis (top), grouped under header rows, with progress fills,
milestone diamonds, finish-to-start dependency arrows and an optional "today" line.

## When to use
- A project schedule: who/what runs when, what is done, what waits on what.
- Tens of tasks (up to ~60 stay readable at default height; more thin the labels).

## When not to use
- Many parallel streams with no dependencies: `swimlane-timeline`.
- Quarter-level planning by theme: `roadmap`.
- Only dates that matter, no durations: `milestone-timeline`.
- Things that happened (not a plan): `event-timeline`.

## Input
Rows of `{ id, name, start, end | duration, progress?, dependsOn?, milestone?, group? }`.
```json
[{ "id": "req", "name": "Requirements", "start": "2026-03-02", "end": "2026-03-09", "progress": 1, "group": "Plan" },
 { "id": "spec", "name": "Tech spec", "start": "2026-03-09", "duration": 7, "progress": 0.5, "dependsOn": "req", "group": "Plan" },
 { "id": "kick", "name": "Kick-off", "start": "2026-03-16", "milestone": true, "dependsOn": ["spec"] }]
```
- `id` defaults to `name`. `end` is exclusive; without it, `duration` (days, wall-clock in
  `timeZone`) is used, and with neither the task is a milestone. `start == end` is a milestone too.
- `progress` is 0–1, or 0–100 when any value in the data is above 1.
- `dependsOn`: an id, a comma/semicolon list (`"a; b"`), or an array.
- `milestone`: boolean, `yes/no`, `1/0`.
- Rejected with a message: `end` before `start` (*"field 'end': end 2026-03-01 is before start
  2026-03-05 at row 3"*), an unknown dependency (*"unknown task id 'zzz'"*), a task that depends
  on itself, negative durations, progress outside 0–100.

Also accepted: CSV text, column arrays, and a field mapping
(`{ fields: { id: 'wbs', name: 'task', duration: 'days', dependsOn: 'after' } }`).
Date strings without an offset are read in `timeZone`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `today` | date \| `'now'` \| `false` | `false` | dashed "Today" line (noted when outside the schedule) |
| `showProgress` | boolean | `true` | percent after each bar, where it fits |
| `arrows` | boolean | `true` | dependency arrows (hidden, with a note, when rows are under 4px) |

Base options (`timeZone`, `zoom`, `locale`, `theme`, `patterns`, …) — see `core/fields.ts`.
`zoom: true` zooms and pans the time axis.

## Example
```ts
chart('gantt', el, csv, { fields: { id: 'wbs', name: 'task', group: 'phase', start: 'begin', duration: 'days', progress: 'pct', dependsOn: 'after' }, today: 'now' })
```

## Edge cases
Empty → empty state. One task → a bar on a padded axis. All zero-length → all milestones.
Nulls → `end` falls back to `duration`, then milestone; blank progress draws no fill.
Duplicate ids → drawn as separate tasks, noted; dependencies resolve to the first.
5,200 tasks → canvas, labels thin out. Long and RTL names truncate with the full text on hover.
A three-year outlier task compresses the rest but keeps every bar. Unsorted rows keep input
order within groups; mixed offsets compare as instants.
