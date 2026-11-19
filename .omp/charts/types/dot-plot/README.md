# Dot plot (`dot-plot`)

Cleveland dot plot: one line per category, a dot per value on a shared value axis, several
series as coloured dots.

## When to use
- Comparing values across categories where the differences are small relative to the
  values (life expectancy, pay): the axis spans the data instead of starting at zero.
- Two or three series per category (men/women, 2000/2022): the faint span between a
  category's dots makes the gap readable.

## When not to use
- Values whose meaning is their length from zero (counts, totals): `bar` or `lollipop`.
- Exactly two values per category where the direction of change matters: `dumbbell`.
- Many series per category: `grouped-bar` or `heatmap`.

## Input
Rows of `{ label, value, series? }`. Rows sharing a `label` share a line; `series` picks
the colour and adds a legend when there is more than one. `value` may be `null` (skipped, noted).
```json
[{ "label": "Japan", "series": "2000", "value": 81.1 }, { "label": "Japan", "series": "2022", "value": 84.1 }]
```
Also accepted: CSV text, column arrays, and any column names through a field mapping:
`{ fields: { label: 'industry', series: 'sex', value: 'median_hourly_gbp' } }`.
A repeated category/series pair is kept as separate dots and noted.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `sort` | `none \| ascending \| descending` | `none` | category order, by the mean of each category's visible values |
| `zero` | boolean | `false` | force zero into the value axis |
| `guides` | boolean | `true` | faint dotted guide across each category line |
| `xTitle`, `yTitle` | string | — | axis titles |

Every chart also takes the base options (`valueFormat`, `locale`, `currency`, `theme`,
`patterns`, `title`, `onClick`, ... — see `core/fields.ts`). Legend items toggle series.

## Example
```ts
import { chart } from '.omp/charts'
chart('dot-plot', el, csv, { fields: { label: 'industry', series: 'sex', value: 'median_hourly_gbp' }, valueFormat: 'currency', currency: 'GBP', sort: 'descending' })
```

## Edge cases
Empty → empty state. Single → one dot in a padded domain. Zeros → every dot at 0.
Negatives → drawn left of the zero line. Nulls → skipped + note. Duplicates → separate
dots + note. 5,202 dots → canvas; guides and spans drop when the lines get too thin.
Long and RTL labels truncate with the full text on hover. Outliers → a tall linear domain.
