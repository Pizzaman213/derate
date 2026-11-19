# Box plot (`box-plot`)

Median, quartiles, Tukey whiskers and outliers of a value, one box per group.

## When to use
- Comparing the centre and spread of a value across a handful to a few dozen groups.
- Spotting outliers (`points: 'outliers'`), or showing every value over the box (`points: 'all'`).

## When not to use
- The shape matters (two modes hide inside a box): `violin` or `ridgeline`.
- One variable on its own: `histogram` or `density`.
- Very few values per group: `strip` or `beeswarm`.

## Input
Rows of `{ group, value }`. `value` may be `null` (dropped, noted).
```json
[{ "group": "North", "value": 42 }, { "group": "North", "value": 47 }, { "group": "South", "value": 31 }]
```
Also accepted: CSV text, column arrays and a field mapping: `{ fields: { group: 'department', value: 'salary_k' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `orientation` | `vertical \| horizontal` | `vertical` | horizontal puts groups on y (better for long names) |
| `whisker` | number | `1.5` | whiskers reach the furthest value within this many IQRs of the box |
| `showMean` | boolean | `false` | mean as a diamond |
| `points` | `outliers \| all \| none` | `outliers` | outlier dots, every value jittered, or none |
| `sort` | `none \| ascending \| descending` | `none` | group order by median |
| `maxGroups` | integer | `30` | beyond this, the smallest groups are pooled into "Other" (noted) |
| `xTitle`, `yTitle` | string | — | axis titles |

Quartiles are R type 7 (`core/stats.ts` `boxStats`). Base options: see `core/fields.ts`.

## Example
```ts
chart('box-plot', el, csv, { fields: { group: 'department', value: 'salary_k' }, orientation: 'horizontal', sort: 'descending', showMean: true })
```

## Edge cases
Empty → empty state. A single value or a zero IQR → a 2px box on the median, still hoverable. Nulls → dropped
and noted. Duplicates → each counts. Negatives → drawn below zero. 6,000 values with `points: 'all'` → canvas.
Long labels → rotated and truncated (full on hover). Far outliers → dots on a tall axis, the boxes stay readable.
