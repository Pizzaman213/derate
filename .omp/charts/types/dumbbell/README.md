# Dumbbell chart (`dumbbell`)

Two values per category — a hollow dot (start) and a filled dot (end) joined by a line,
coloured by the direction of the change.

## When to use
- Before/after, plan/actual, two years: how much each category moved and which way.
- Ranking categories by change (`sort: 'change'`).

## When not to use
- More than two points in time: `slope` for a few categories, `line` for many points.
- One value per category: `lollipop` or `bar`. Several unordered series: `dot-plot`.
- The change itself is the only thing that matters: `diverging-bar` of the differences.

## Input
Rows of `{ label, start, end }`. Either value may be `null`: the other dot is still drawn
(no connector) and the row is noted.
```json
[{ "label": "Alpha", "start": 62, "end": 74 }, { "label": "Bravo", "start": 71, "end": 68 }]
```
Also accepted: CSV text, column arrays, and any column names through a field mapping:
`{ fields: { label: 'country', start: 'share_2015', end: 'share_2023' } }`.
Repeated labels stay separate rows (noted).

**Marks:** the data marks are the two endpoints — a complete row counts 2, a row with one
value missing counts 1. Each announces its own value; the end dot also announces the change.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `startLabel`, `endLabel` | string | `Start`, `End` | names of the two values (legend, tooltip, table columns) |
| `sort` | `none \| start \| end \| change` | `none` | descending by the chosen measure; rows missing it go last |
| `higherIsBetter` | boolean | `true` | `false` colours increases as bad (costs, error rates) |
| `zero` | boolean | `false` | force zero into the value axis |
| `xTitle`, `yTitle` | string | — | axis titles |

Colours: improvement `theme.positive`, deterioration `theme.negative`, no change
`theme.neutral`. The legend toggles each direction. Every chart also takes the base
options (see `core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('dumbbell', el, csv, { fields: { label: 'country', start: 'share_2015', end: 'share_2023' }, startLabel: '2015', endLabel: '2023', sort: 'change' })
```

## Edge cases
Empty (or no values at all) → empty state. Single → one dumbbell. Zeros → both dots at 0,
"No change". Negatives → drawn left of the zero line. Nulls → one dot + note. Duplicates →
separate rows + note. 2,600 rows (5,200 dots) → canvas. Long and RTL labels truncate with
the full text on hover. Outliers → a tall linear domain.
