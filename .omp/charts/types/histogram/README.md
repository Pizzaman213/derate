# Histogram (`histogram`)

Counts of values falling into contiguous ranges (bins), as touching bars.

## When to use
- The shape of one numeric variable: where values cluster, skew, gaps, a second mode.
- Two or three series on the same bins (`layout: 'overlay'`), or more as small multiples (`layout: 'facet'`).

## When not to use
- Comparing many groups' distributions: `box-plot`, `violin` or `ridgeline`.
- A smooth shape rather than counts: `density`.
- Categories rather than numbers: `bar`.
- Very few values (under ~20): `strip` or `beeswarm` show every point.

## Input
Rows of `{ value, series? }`, or simply a list of numbers. `value` may be `null` (dropped, noted).
```json
[{ "value": 42 }, { "value": 47 }, { "value": 51 }]
[42, 47, 51, 55]
```
Also accepted: CSV text, column arrays (`{ "value": [...] }`) and a field mapping:
`{ fields: { value: 'minutes', series: 'region' } }`. Every series is binned on the same edges.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `bins.count` | integer | Sturges | target bin count (edges are rounded to nice numbers) |
| `bins.thresholds` | number[] | — | explicit inner bin edges |
| `bins.method` | `sturges \| freedman-diaconis \| scott` | `sturges` | rule for the bin count |
| `bins.domain` | `[min, max]` | data extent | fixed binned range; values outside are left out and noted |
| `layout` | `overlay \| facet` | `overlay` | several series: semi-transparent overlay, or one panel each with shared scales |
| `normalize` | `count \| percent \| density` | `count` | bar height: count, share of the series, or density (total area 1) |
| `xTitle`, `yTitle` | string | — / by `normalize` | axis titles |

Every chart also takes the base options (`valueFormat`, `locale`, `theme`, `patterns`, ... — see `core/fields.ts`).

## Example
```ts
chart('histogram', el, csv, { fields: { value: 'minutes', series: 'region' }, bins: { method: 'freedman-diaconis' }, xTitle: 'Delivery time (minutes)' })
```

## Edge cases
Empty → empty state. One value, or all values equal → one bin centred on the value, in a padded axis.
Nulls → dropped and noted. Duplicates → each counts. Negatives → bins below zero as usual.
5,199 one-millisecond bins → canvas. Long and RTL series names → truncated legend and facet titles
with the full name on hover. Far outliers → a long sparse tail (set `bins.domain` to focus; what is
left out is noted).
