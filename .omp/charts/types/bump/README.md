# Bump chart (`bump`)

How the ranking of several series changes over time; rank 1 at the top, smooth curves between time points.

## When to use
- League tables, popularity rankings, market-share positions over a few to a few dozen periods.

## When not to use
- When the size of the gaps matters, not just the order: `line`.
- Two periods only: `slope`.

## Input
Rows of `{ x, series, rank?, value? }`. Where every row at an x has a rank, it is used as is; otherwise ranks are derived from `value` per x, highest first, ties sharing the better rank (1, 2, 2, 4). `x` may be a date (sorted by instant; the same instant written differently is one x), a number (sorted) or an ordinal label (kept in order of appearance).
```json
[{ "x": "2024", "series": "Alpha", "value": 50 }, { "x": "2024", "series": "Bravo", "value": 40 }]
```
Also accepted: CSV text, column arrays, a field mapping: `{ fields: { x: 'week', series: 'club', rank: 'position' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `showRanks` | boolean | `true` | the rank number inside each point when points are large enough |

Every chart also takes the base options (`valueFormat`, `locale`, `timeZone`, `theme`, `patterns`, `title`, ...; see `core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('bump', el, csv, { fields: { x: 'week', series: 'club', rank: 'position' } })
```

## Edge cases
Duplicate (x, series) rows are merged (values summed, best rank kept) and noted; rows with neither rank nor value are left out (noted); a series missing at an x breaks its line. End labels sit at the last x, nudged apart, thinned with a note when there are too many; legend shown up to 12 series. 5,200 points → canvas.
