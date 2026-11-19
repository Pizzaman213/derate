# Population pyramid (`population-pyramid`)

Back-to-back horizontal bars: one group (usually male) to the left of a shared zero, the other
(usually female) to the right, one row per age band, on a symmetric value axis so equal values
have equal lengths on both sides.

## When to use
- The age (or any ordered band) structure of a population, split two ways.
- Comparing the two sides band by band: ageing, baby booms, migration bulges, sex ratios.

## When not to use
- More than two groups: `grouped-bar` or `stacked-bar` (a third group's rows are omitted here).
- Unordered categories: `diverging-bar` or `grouped-bar`.
- Totals rather than the shape: `bar` or `stacked-bar`.

## Input
Rows of `{ age, sex, value }`. `value` may be `null` (left blank); negatives are omitted;
repeated `(age, sex)` pairs are summed — each is noted under the chart.
```json
[{ "age": "0–14", "sex": "male", "value": 620 }, { "age": "0–14", "sex": "female", "value": 590 }]
```
Also accepted: CSV text, column arrays and a field mapping:
`{ fields: { age: 'age_group', sex: 'sex', value: 'population' } }`.

**Sides.** By default a male-like group (`male`, `M`, `men`, `boys`, ...) goes left and a
female-like one right; otherwise the first two groups seen. `left` / `right` pick them by name
(case-insensitive).

**Band order.** With `order: 'youngest-bottom'` (default) bands are ordered by the first number
in their label when every label has one ("Under 5", "5 to 9", ..., "85 and over"; input listed
oldest first is flipped), else taken as given, youngest first; the youngest sits at the bottom.
`order: 'as-given'` puts the first-seen band at the top.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `left`, `right` | string | male / female, else first two | group drawn on each side |
| `percentages` | boolean | `false` | each bar as a share of the whole population (both sides) |
| `order` | `youngest-bottom \| as-given` | `youngest-bottom` | band order (see above) |
| `valueTitle`, `ageTitle` | string | — | axis titles |

The hidden table and CSV are one row per band (youngest first): `Age, <left>, <right>`. Every
chart also takes the base options — see `core/fields.ts`.

## Example
```ts
import { chart } from '.omp/charts'
chart('population-pyramid', el, csv, { fields: { age: 'age_group', sex: 'sex', value: 'population' }, valueFormat: 'si' })
```

## Edge cases
Empty → empty state; all zeros → "All values are zero". One row → one bar (thickness capped)
with the other side still named. Negatives → omitted + note. Nulls → blank + note. Duplicates →
summed + note. A third group → omitted + note. 101 single-year bands (202 bars) → thin bars,
age labels thinned. Long band names truncate with the full text on hover; RTL names render.
An outlier band stretches the symmetric axis on both sides.
