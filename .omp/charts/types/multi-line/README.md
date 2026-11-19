# Multi-line chart (`multi-line`)

Several series over the same x, one line each, named directly at their right ends.

## When to use
- Comparing how a handful (≤ 8) of series move over time.

## When not to use
- One series: `line`. Parts of a total: `stacked-area` / `stacked-area-100`.
- Dozens of series: small multiples, or highlight one and grey the rest.

## Input
Long rows of `{ x, series, y }`. `x` is a date (Date, ISO 8601, epoch, written forms like `Mar 2026` or `2026-Q1`) or a plain number. Rows are sorted by x, so order does not matter, and instants written with different offsets compare correctly. Duplicate (x, series) pairs are summed.
```json
[{ "x": "2026-01-01", "series": "North", "y": 42000 }, { "x": "2026-01-01", "series": "South", "y": 30000 }]
```
Also accepted: CSV text, column arrays (`{ "x": [...], "y": [...] }`) and any column names through a field mapping (`fields: { x: "date", y: "revenue" }`). Wide tables: `unpivot` them first (adapters).

## Options
| Option | Type | Default | |
|---|---|---|---|
| `curve` | `linear \| monotone` | `linear` | |
| `endLabels` | boolean | `true` | series names at the line ends when they fit (≥ 420px wide, no overlaps after nudging); the legend stays and toggles series (y rescales) |
| `gaps` | `gap \| zero \| interpolate` | `'gap'` | missing values: lift the pen, draw 0, or join the neighbours (ends stay gaps) |
| `xType` | `auto \| time \| number` | `auto` | numbers stay numeric unless they look like epoch ms |
| `points` | `auto \| always \| never` | `'auto'` | dots on each value (`auto`: while sparse); hover and keyboard work either way |
| `xTitle`, `yTitle` | string | — | axis titles |

Every chart also takes the base options (`valueFormat`, `locale`, `timeZone`, `zoom`, `brush`, `onBrush`, ... — see `core/fields.ts`). `zoom: true` enables wheel/drag zoom on x (double-click resets); `brush: true` reports the selected x range (Dates) through `onBrush`.

## Example
```ts
import { chart } from '.omp/charts'
chart('multi-line', el, { week, ticker, close }, { fields: { x: 'week', series: 'ticker', y: 'close' }, valueFormat: 'currency' })
```

## Edge cases
Empty → empty state. Single row → one dot on a padded axis. Zeros / all-equal → a flat line on a padded domain. Negatives → below the zero rule. Nulls → gaps (or per `gaps`) + note. Duplicate x → summed + note. 5,200 rows → canvas. Unsorted dates and mixed time zones → sorted by instant. Long / RTL axis titles → truncated with the full text on hover. Outliers → a tall linear domain (the spike stays readable). Long / RTL series names → truncated end labels (full text on hover) and ellipsised legend items.
