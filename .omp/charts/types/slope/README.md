# Slope chart (`slope`)

Lines between two time points, one per item, labelled at both ends.

## When to use
- Before/after comparisons of a handful to a few dozen items: who rose, who fell, did the ranking change.

## When not to use
- More than two time points: `bump` (ranks) or `line`.
- Hundreds of items: labels cannot fit; use `dumbbell` or `scatter` of start vs end.

## Input
Rows of `{ label, start, end }`. A row missing either value is left out (noted). Repeated labels are drawn as separate lines (noted).
```json
[{ "label": "Japan", "start": 81.1, "end": 84.0 }]
```
Also accepted: CSV text, column arrays, a field mapping: `{ fields: { label: 'country', start: 'le_2000', end: 'le_2022' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `startLabel` | string | `Start` | heading over the left axis |
| `endLabel` | string | `End` | heading over the right axis |
| `colorBy` | `direction \| category` | `direction` | rises in the positive colour, falls in the negative (legend toggles them), or one colour per line |
| `showValues` | boolean | `true` | the value beside each label |

Every chart also takes the base options (`valueFormat`, `locale`, `timeZone`, `theme`, `patterns`, `title`, ...; see `core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('slope', el, csv, { fields: { label: 'country', start: 'le_2000', end: 'le_2022' }, startLabel: '2000', endLabel: '2022' })
```

## Edge cases
Labels at each end are nudged apart (clusters centred on their lines, order kept) so they never overlap; when they cannot all fit, the biggest movers keep theirs and a note says how many were hidden (hover the line). Long and RTL labels truncate with the full text on hover. Zeros → flat lines. 5,200 lines → canvas, endpoint dots dropped.
