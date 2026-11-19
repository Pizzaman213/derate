# UpSet plot (`upset`)

Exact sizes of every set intersection as bars over a dot matrix that spells out each combination, with set totals beside it.

## When to use
- Overlaps of more than three sets, or whenever the overlap sizes must be read exactly.
- Ranking which combinations are most common (feature adoption, gene sets, survey multi-selects).

## When not to use
- Two or three sets for a general audience: `venn` is more familiar.
- A quick picture of which sets touch: `euler`.

## Input
**Memberships** -- one row per item, `sets` naming every set it is in:
```json
[{ "item": "Ada", "sets": "Python,SQL" }, { "item": "Ben", "sets": "Python" }]
```
`item` is optional. Rows that share an `item` are one item (so a long-format export with one
row per item and set works as is); a row that repeats a membership already seen is ignored
and noted. An empty `sets` means "in no set" (counted in the note, not drawn).

**Aggregated sizes** -- when any row has a `size`: `{ sets: 'A,B', size }` rows, or venn.js's
`{ sets: [{ sets: ['A'], size }] }`. The `sizes` option says what they mean.

`sets` may be `'A,B'`, `'A;B'`, `'A|B'` or a list. CSV text, column arrays and a field mapping
(`{ fields: { item: 'user_id', sets: 'feature_used' } }`) are accepted.

Each bar is an **exclusive** intersection: items in exactly those sets and no other. Set
totals are the sum of the intersections each set is in.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `sortBy` | `size \| degree` | `size` | largest first, or by number of sets then size |
| `sizes` | `inclusive \| exclusive` | `inclusive` | aggregated input: inclusive (venn.js: `'A,B'` counts everything in both; converted by inclusion-exclusion) or exclusive (in exactly these sets) |
| `showValues` | boolean | `true` | each size above its bar, when it fits the column |

Base options as for every chart (`core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('upset', el, csvExport, { fields: { item: 'user_id', sets: 'feature_used' }, sortBy: 'degree' })
```

## Edge cases
Empty → empty state; aggregated sizes all zero → "All sizes are zero". Null and negative
sizes → left out, noted. Duplicate aggregated rows → summed, noted. Inclusive sizes that
contradict their listed overlaps → counted as zero, noted. More intersections than columns
fit → the smallest are left out and counted in the note (the table keeps all of them); more
sets than rows fit → the smallest sets are left out, noted. Long and RTL set names →
truncated beside the matrix with the full name on hover. An intersection 1,000× the others →
linear scale, the small bars a sliver with their values printed.
