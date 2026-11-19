# Burnup chart (`burnup`)

Completed work climbing toward the total scope — both drawn, so scope changes stay visible — with a projected completion date.

## When to use
- Tracking a release or project whose scope moves: the gap between the two lines is the work left, and a step in the scope line is a decision someone made.
- Answering "when will it be done at this rate?" (the projection).

## When not to use
- A fixed-scope sprint where only "remaining" matters: `burndown`.
- Work moving through several states (backlog → in progress → done): `cumulative-flow`.
- Many projects compared at once: `multi-line` or small multiples.

## Input
Rows of `{ date, completed, scope, note? }`, one per reading. `completed` is cumulative; `scope`
is the total committed work at that date (completed + remaining).
```json
[{ "date": "2026-01-05", "completed": 0, "scope": 120 }, { "date": "2026-01-12", "completed": 9, "scope": 120, "note": "Kick-off" }]
```
- `completed` / `scope` may be `null` (gaps, noted).
- `note` flags the date along the top (milestones, scope decisions); labels truncate to the room before the next flag, full text on hover.

Also accepted: CSV text, column arrays, and any column names through a field mapping:
`{ fields: { date: 'Week', completed: 'Tasks done', scope: 'Total tasks', note: 'Milestone' } }`.
Rows are sorted by date; the same date twice keeps the last reading (noted).

### The projection
A least-squares line through the completed readings (x in days; the last `projectionWindow`
readings, or all) is extended until it meets the **latest** scope. That point is a focusable
diamond labelled `Done ~<date>`, and the latest scope is carried on (dotted) to meet it. If the
fitted line has already passed the scope while the work is not done, the fitted rate is carried
on from the last reading instead. Completed work at or above the latest scope → no projection
(the summary says when the scope was completed); a flat or falling trend, fewer than two
readings, or no scope → no projection, with a note saying why. A projection further out than
three times the data's span is drawn to the chart edge and its date given in the note.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `projection` | boolean | `true` | draw the projected completion |
| `projectionWindow` | integer ≥ 2 | all | fit only the last N readings (recent velocity) |
| `area` | boolean | `true` | shade under the completed line |
| `xTitle`, `yTitle` | string | — | axis titles |

Base options (`valueFormat`, `timeZone`, `locale`, `zoom`, `brush`, `theme`, `title`, ...) are in
`core/fields.ts`. Legend entries toggle the completed, scope, projection and note layers.

## Example
```ts
import { chart } from '.omp/charts'
chart('burnup', el, csvText, {
  fields: { date: 'Week', completed: 'Tasks done', scope: 'Total tasks', note: 'Milestone' },
  projectionWindow: 6, yTitle: 'Tasks',
})
```

## Edge cases
Empty (or no values at all) → empty state. One reading → two dots, no projection (noted). All
zeros → completed already meets the (zero) scope, so no projection. Negatives are drawn below zero. Nulls → gaps + note.
Duplicate dates → merged + note. 2,600 readings × 2 series → canvas, dots become invisible
hit targets. Long and RTL notes → truncated flag labels with the full text on hover. Unsorted
dates and mixed UTC offsets / epoch ms → sorted by instant. Outliers → a tall axis.
