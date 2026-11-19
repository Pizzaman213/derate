# Area chart (`area`)

One value over time, filled down to zero to stress volume.

## When to use
- One measure whose magnitude matters: active users, load, cumulative totals.

## When not to use
- Several series: `stacked-area` (parts of a total) or `multi-line` (comparison) — overlapping areas hide each other.
- Values far from zero: `line` (the fill would exaggerate a small change).

## Input
Rows of `{ x, y }`. `x` is a date (Date, ISO 8601, epoch, written forms like `Mar 2026` or `2026-Q1`) or a plain number. Rows are sorted by x, so order does not matter, and instants written with different offsets compare correctly.
```json
[{ "x": "2026-01-01", "y": 12000 }, { "x": "2026-02-01", "y": 14000 }]
```
Also accepted: CSV text, column arrays (`{ "x": [...], "y": [...] }`) and any column names through a field mapping (`fields: { x: "date", y: "revenue" }`).

## Options
| Option | Type | Default | |
|---|---|---|---|
| `curve` | `linear \| monotone` | `linear` | |
| `gaps` | `gap \| zero \| interpolate` | `'gap'` | missing values: lift the pen, draw 0, or join the neighbours (ends stay gaps) |
| `xType` | `auto \| time \| number` | `auto` | numbers stay numeric unless they look like epoch ms |
| `points` | `auto \| always \| never` | `'auto'` | dots on each value (`auto`: while sparse); hover and keyboard work either way |
| `xTitle`, `yTitle` | string | — | axis titles |

Every chart also takes the base options (`valueFormat`, `locale`, `timeZone`, `zoom`, `brush`, `onBrush`, ... — see `core/fields.ts`). `zoom: true` enables wheel/drag zoom on x (double-click resets); `brush: true` reports the selected x range (Dates) through `onBrush`.

## Example
```ts
import { chart } from '.omp/charts'
chart('area', el, csv, { fields: { x: 'timestamp', y: 'cpu_pct' }, yTitle: 'CPU %' })
```

## Edge cases
Empty → empty state. Single row → one dot on a padded axis. Zeros / all-equal → a flat line on a padded domain. Negatives → below the zero rule. Nulls → gaps (or per `gaps`) + note. Duplicate x → summed + note. 5,200 rows → canvas. Unsorted dates and mixed time zones → sorted by instant. Long / RTL axis titles → truncated with the full text on hover. Outliers → a tall linear domain (the spike stays readable). The fill always reaches the zero line; nulls split the fill.
