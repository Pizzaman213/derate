# Range area chart (`range-area`)

A band between low and high over time — min–max, confidence or forecast interval — with an optional centre line.

## When to use
- Uncertainty around an estimate (forecast ± 95%), daily low–high, percentile bands.

## When not to use
- Full distributions per period: `box-plot` / `violin`. A single value: `line`.

## Input
Rows of `{ x, low, high, y? }`. `x` is a date (Date, ISO 8601, epoch, written forms like `Mar 2026` or `2026-Q1`) or a plain number. Rows are sorted by x, so order does not matter, and instants written with different offsets compare correctly. `high` must be ≥ `low` ("field 'high' must be >= low (5), got 3 at row 2"). `y` (mean, median, forecast) draws the centre line; without it, hover targets sit at the band's middle. Duplicate x → first row kept (noted).
```json
[{ "x": "2026-01-01", "low": -3, "high": 5, "y": 1 }, { "x": "2026-02-01", "low": -2, "high": 7, "y": 2.5 }]
```
Also accepted: CSV text, column arrays (`{ "x": [...], "y": [...] }`) and any column names through a field mapping (`fields: { x: "date", y: "revenue" }`).

## Options
| Option | Type | Default | |
|---|---|---|---|
| `curve` | `linear \| monotone` | `linear` | |
| `bandLabel` | string | `Range` | what the band is (legend, tooltips): `95% interval`, `Min–max` |
| `lineLabel` | string | `Value` | what the centre line is |
| `gaps` | `gap \| zero \| interpolate` | `'gap'` | missing values: lift the pen, draw 0, or join the neighbours (ends stay gaps) |
| `xType` | `auto \| time \| number` | `auto` | numbers stay numeric unless they look like epoch ms |
| `points` | `auto \| always \| never` | `'auto'` | dots on each value (`auto`: while sparse); hover and keyboard work either way |
| `xTitle`, `yTitle` | string | — | axis titles |

Every chart also takes the base options (`valueFormat`, `locale`, `timeZone`, `zoom`, `brush`, `onBrush`, ... — see `core/fields.ts`). `zoom: true` enables wheel/drag zoom on x (double-click resets); `brush: true` reports the selected x range (Dates) through `onBrush`.

## Example
```ts
import { chart } from '.omp/charts'
chart('range-area', el, csv, { fields: { x: 'date', low: 'lower_95', high: 'upper_95', y: 'forecast' }, bandLabel: '95% interval', lineLabel: 'Forecast' })
```

## Edge cases
Empty → empty state. Single row → one point. Zeros → a flat line on a padded domain. A band crossing zero → zero rule. Missing bounds → band split + note. Duplicate x → first kept + note. 5,200 rows → canvas. Unsorted / mixed-tz dates → sorted. Long / RTL titles → truncated, full text on hover.
