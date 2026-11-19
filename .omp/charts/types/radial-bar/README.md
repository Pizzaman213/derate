# Radial bar chart (`radial-bar`)

Concentric arcs, one ring per category, each swept clockwise from 12 o'clock in proportion
to its value; the labels sit at the start of each ring.

## When to use
- A handful of values against a common maximum, where a compact, round form suits the
  layout: quota attainment by region (`max: 100`), goal completion per team.
- Dashboards where a circle fits the space better than a bar chart.

## When not to use
- Precise comparison: arc length on rings of different radii is hard to judge — outer rings
  look longer for the same value. Use `bar`, `horizontal-bar` or `lollipop`.
- One progress value: `progress-ring` or `gauge`. Parts of a whole: `pie` or `donut`.
- More than about 15 categories: rings get too thin (see Edge cases).

## Input
Rows of `{ label, value }`, outermost ring first. `value` may be `null` (an empty track, noted).
```json
[{ "label": "Alpha", "value": 120 }, { "label": "Bravo", "value": 340 }]
```
Also accepted: CSV text, column arrays, and any column names through a field mapping:
`{ fields: { label: 'region', value: 'quota_attained_pct' } }`. Duplicate labels are summed
and noted.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `max` | number | largest value | value of a full sweep; larger values draw a full ring (noted) |
| `sweep` | number (90–270) | `270` | degrees of a full ring; the free top-left quarter holds the labels |
| `sort` | `none \| ascending \| descending` | `none` | ring order, outermost first |
| `showValues` | boolean | `true` | value beside each label |
| `colorBy` | `category \| single` | `category` | one colour per ring (pattern fills follow), or one for all |

Every chart also takes the base options (see `core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('radial-bar', el, csv, { fields: { label: 'region', value: 'quota_attained_pct' }, max: 100, sort: 'descending' })
```

## Edge cases
Empty → empty state; all negative → empty state with a message. Single → one half-width
ring. Zeros → a sliver at 12 o'clock (still hoverable). Negatives → omitted + note.
Nulls → empty tracks + note. Duplicates → summed + note. **Large** (60 rows) → rings keep a
pitch of at least 6px and 4.5% of the radius (about 17 rings at most); the first rows in
order are drawn and the rest are noted, with every row in the table — a summed "Other"
ring would dwarf the rest against `max`. Labels thin to every k-th ring when the pitch is
below the text height. Long and RTL labels truncate with the full text on hover. Outliers
→ set `max` (the sample uses `max: 400`): the outlier is a full ring, noted.
The circle always fits the box: its radius is bounded by both the height and the width.
