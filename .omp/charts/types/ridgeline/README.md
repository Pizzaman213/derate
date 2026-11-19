# Ridgeline plot (`ridgeline`)

One kernel density per group on a shared x axis, stacked in overlapping rows, names on the left.

## When to use
- Comparing distribution shapes across many groups (5–30), especially ordered ones (months, ages, versions).
- Showing a shift in a distribution over an ordered sequence.

## When not to use
- Two or three groups: `density` (overlaid) or `violin`.
- Precise statistics per group: `box-plot`.
- Counts rather than shapes: `histogram`.

## Input
Rows of `{ group, value }`. `value` may be `null` (dropped, noted). Rows appear in first-seen group order.
```json
[{ "group": "January", "value": 4.2 }, { "group": "February", "value": 5.1 }]
```
Also accepted: CSV text, column arrays and a field mapping: `{ fields: { group: 'month', value: 'high_c' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `bandwidth` | number \| `silverman \| scott` | `silverman` | KDE bandwidth in data units, or a rule (per group) |
| `overlap` | number 0–4 | `1.2` | how far the tallest ridge rises into the rows above, in rows |
| `scale` | `shared \| each` | `shared` | one density scale (peaks compare), or every ridge equally tall |
| `colorBy` | `single \| group` | `single` | one colour, or one per group |
| `focus` | `auto \| all` | `auto` | a few far outliers: axis covers the bulk, outliers noted |
| `sort` | `none \| ascending \| descending` | `none` | row order by median |
| `maxGroups` | integer | `30` | beyond this, the smallest groups are pooled into "Other" (noted) |
| `xTitle` | string | — | x axis title |

Base options: see `core/fields.ts`.

## Example
```ts
chart('ridgeline', el, csv, { fields: { group: 'month', value: 'high_c' }, colorBy: 'group', overlap: 1.5 })
```

## Edge cases
Empty → empty state. A single value → one bump of the fallback bandwidth. Nulls → dropped and noted.
60 groups → 29 largest + "Other"; when rows are too thin for every name, labels thin out (noted) and every
ridge still names its group on hover. Long and RTL names → truncated with the full name on hover.
Far outliers → focused axis, noted.
