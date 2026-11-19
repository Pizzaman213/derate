# Violin plot (`violin`)

Kernel density per group, mirrored about the group's centre line, with a slim box plot inside.
Width at a value is proportional to the density there.

## When to use
- Comparing distribution shapes across groups: skew, a second mode, a long tail.
- Enough values per group (roughly 20+) for a density to mean something.

## When not to use
- Few values per group: `box-plot`, `strip` or `beeswarm`.
- Many groups on one axis: `ridgeline`.
- One distribution: `density` or `histogram`.

## Input
Rows of `{ group, value }`. `value` may be `null` (dropped, noted).
```json
[{ "group": "Control", "value": 48.2 }, { "group": "Dose A", "value": 61.0 }]
```
Also accepted: CSV text, column arrays and a field mapping: `{ fields: { group: 'species', value: 'flipper_mm' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `bandwidth` | number \| `silverman \| scott` | `silverman` | KDE bandwidth in data units, or a rule (per group) |
| `scale` | `area \| width \| count` | `area` | one density scale (equal areas); every violin full width; or area ∝ n |
| `inner` | `box \| quartiles \| none` | `box` | slim box plot, dashed quartile lines, or nothing |
| `trim` | boolean | `false` | end each violin at its data range (default: tails run 3 bandwidths on) |
| `focus` | `auto \| all` | `auto` | a few far outliers: axis covers the bulk, outliers noted |
| `sort` | `none \| ascending \| descending` | `none` | group order by median |
| `maxGroups` | integer | `24` | beyond this, the smallest groups are pooled into "Other" (noted) |
| `xTitle`, `yTitle` | string | — | axis titles |

Base options: see `core/fields.ts`.

## Example
```ts
chart('violin', el, csv, { fields: { group: 'species', value: 'flipper_mm' }, inner: 'quartiles' })
```

## Edge cases
Empty → empty state. A single value (or all equal) → a bump using the pooled bandwidth (or core's
fallback). Nulls → dropped and noted. Negatives → below zero. 60 groups → 23 largest + "Other", noted.
Long and RTL labels → rotated and truncated with the full name on hover. Far outliers → focused axis, noted.
