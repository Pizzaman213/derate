# Density plot (`density`)

Smoothed distribution of one or more series: a kernel density estimate per series, filled.
Every curve integrates to 1, so series of different sizes compare by shape.

## When to use
- The shape of a numeric variable without the arbitrariness of bin edges.
- Comparing two to four series' shapes on one axis.

## When not to use
- Readers who need counts: `histogram`.
- Many series: `ridgeline`. Groups with summary statistics: `violin` or `box-plot`.
- Small samples (under ~20): `strip`, `beeswarm` or `ecdf`.

## Input
Rows of `{ value, series? }`, or a list of numbers. `value` may be `null` (dropped, noted).
```json
[{ "value": 2.1 }, { "value": 4.4 }]
[2.1, 4.4, 3.9]
```
Also accepted: CSV text, column arrays and a field mapping: `{ fields: { value: 'minutes', series: 'platform' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `bandwidth` | number \| `silverman \| scott` | `silverman` | KDE bandwidth in data units, or a rule (per series) |
| `kernel` | `gaussian \| epanechnikov` | `gaussian` | smoothing kernel |
| `fill` | boolean | `true` | fill the area under each curve |
| `rug` | boolean | `false` | a tick per observation along the x axis (up to 2,000 values) |
| `focus` | `auto \| all` | `auto` | a few far outliers: axis covers the bulk, outliers noted |
| `xTitle`, `yTitle` | string | — / `Density` | axis titles |

Base options: see `core/fields.ts`.

## Example
```ts
chart('density', el, csv, { fields: { value: 'minutes', series: 'platform' }, bandwidth: 'scott' })
```

## Edge cases
Empty → empty state. A single value or all equal → a bell of the fallback bandwidth. Nulls → dropped and
noted. Negatives → as usual. 16,000 values → one smooth curve (the estimate is windowed, so it stays fast).
Long and RTL series names → truncated legend, full name on hover. Far outliers → focused axis, noted; the
grid is refined around every cluster so a narrow peak next to a far outlier is still resolved.
