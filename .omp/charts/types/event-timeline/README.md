# Event timeline (`event-timeline`)

Point events (dots) and range events (bars) on one time axis, packed into lanes so that no
two marks — and no two labels — overlap.

## When to use
- A history or an incident log: what happened when, and what lasted how long.
- A few to a few hundred events; thousands still draw (canvas), labels give way.

## When not to use
- Items that belong to a team/system/person: `swimlane-timeline`.
- Planned work with progress and dependencies: `gantt`.
- A handful of key dates on one line: `milestone-timeline`.
- Counts over time: `line` or `bar`.

## Input
Rows of `{ date, end?, label, category? }`. Missing or equal `end` = a point event.
```json
[{ "date": "2026-01-12", "label": "v3.0 release", "category": "Release" },
 { "date": "2026-02-02", "end": "2026-03-15", "label": "Spring campaign", "category": "Marketing" }]
```
`end` before `date` is rejected (*"field 'end': end … is before date … at row 2"*).
Also accepted: CSV text, column arrays, field mapping (`{ fields: { date: 'ts', end: 'until', label: 'what', category: 'team' } }`).

## Layout
Each event's pixel extent includes its label (inside a wide bar, else after the mark, else
before it); events are packed greedily by start into the first lane with room (optimal for
intervals). Lane 0 sits on the axis. When the labelled lanes would not fit, labels are first
truncated harder, then the marks alone are packed and each event keeps its label only if it
has room in its own lane — the rest are counted in a note (tooltips keep them).

## Options
| Option | Type | Default | |
|---|---|---|---|
| `labels` | boolean | `true` | print labels beside marks |
| `maxLabelWidth` | number (px) | `160` | longer labels truncate, full text on hover |

Base options (`timeZone`, `zoom`, `legend`, `patterns`, …) — see `core/fields.ts`. More than one
category shows a legend; toggling a category re-packs the lanes.

## Example
```ts
chart('event-timeline', el, csv, { fields: { date: 'ts', end: 'until', label: 'what', category: 'team' }, timeZone: 'Europe/Berlin' })
```

## Edge cases
Empty → empty state. One event → a two-day axis. Zero-length ranges → points.
Nulls → point events, uncategorised. Duplicates → kept, packed into separate lanes.
5,200 events → canvas, thin lanes, labels hidden and noted. Long/RTL labels truncate with
the full text on hover. A decades-old outlier widens the axis; the cluster packs tightly.
Unsorted rows are sorted; mixed offsets compare as instants.
