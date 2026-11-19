# Milestone timeline (`milestone-timeline`)

Milestones as diamonds on one horizontal line, names alternating above and below; done,
upcoming and overdue styled apart, with an optional "today" marker.

## When to use
- A handful to a few dozen key dates: programme gates, releases, deadlines.
- Status at a glance: what is behind us, what is next, what slipped.

## When not to use
- Durations matter: `gantt` or `swimlane-timeline`.
- Many events of different kinds: `event-timeline`.
- Quarter plans by theme: `roadmap`.

## Input
Rows of `{ date, label, done? }`. `done` accepts booleans, `1/0`, `yes/no`, `done/open`;
missing = upcoming.
```json
[{ "date": "2026-01-15", "label": "Kick-off", "done": true },
 { "date": "2026-05-22", "label": "Beta" }]
```
Also accepted: CSV text, column arrays, field mapping (`{ fields: { date: 'due', label: 'milestone', done: 'status' } }`).

## Layout
Labels (name, then date) are placed left to right, alternating above and below the line.
A label that would touch its neighbour climbs to the next tier on its side, then tries the
other side; only when no tier has room is it hidden (counted in a note, kept in the tooltip).
Done milestones are filled; upcoming ones hollow; with `today`, upcoming milestones before it
are overdue (negative colour). The line is drawn in the positive colour up to today (or the
last done milestone).

## Options
| Option | Type | Default | |
|---|---|---|---|
| `today` | date \| `'now'` \| `false` | `false` | dashed "Today" line; enables "overdue" |
| `maxLabelWidth` | number (px) | `140` | longer names truncate, full text on hover |
| `showDates` | boolean | `true` | date under each name |

Base options (`timeZone`, `zoom`, `legend`, …) — see `core/fields.ts`.

## Example
```ts
chart('milestone-timeline', el, csv, { fields: { date: 'due', label: 'milestone', done: 'status' }, today: 'now' })
```

## Edge cases
Empty → empty state. One milestone → a two-day axis. All on one day → labels stack in
tiers, the rest are hidden and noted. Nulls → upcoming, "(blank)" name. Duplicates → kept,
the copy takes the other side. 5,200 milestones → canvas, labels hidden where they crowd.
Long/RTL names truncate with full text on hover. A fifty-year outlier widens the axis.
Unsorted rows are sorted; mixed offsets compare as instants.
