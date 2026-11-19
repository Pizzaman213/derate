# Line chart (`line`)

One value over continuous time (or any numeric x), as a line through its points.

## When to use
- One measure changing over time: revenue by month, latency by minute.
- Many points (thousands draw on canvas, with zoom).

## When not to use
- Several series: `multi-line`. Values that hold until they change: `step-line`.
- Volume / cumulative totals: `area`. Uncertainty or min–max: `range-area`.
- Few unordered categories: `bar`.

## Input
Rows of `{ x, y }`. `x` is a date (Date, ISO 8601, epoch, written forms like `Mar 2026` or `2026-Q1`) or a plain number. Rows are sorted by x, so order does not matter, and instants written with different offsets compare correctly. `y` may be null.
```json
[{ "x": "2026-01-01", "y": 21000 }, { "x": "2026-02-01", "y": 24000 }]
```
Also accepted: CSV text, column arrays (`{ "x": [...], "y": [...] }`) and any column names through a field mapping (`fields: { x: "date", y: "revenue" }`).

## Options
| Option | Type | Default | |
|---|---|---|---|
| `curve` | `linear \| monotone` | `linear` | `monotone` smooths without overshooting |
| `gaps` | `gap \| zero \| interpolate` | `'gap'` | missing values: lift the pen, draw 0, or join the neighbours (ends stay gaps) |
| `xType` | `auto \| time \| number` | `auto` | numbers stay numeric unless they look like epoch ms |
| `points` | `auto \| always \| never` | `'auto'` | dots on each value (`auto`: while sparse); hover and keyboard work either way |
| `xTitle`, `yTitle` | string | — | axis titles |

Every chart also takes the base options (`valueFormat`, `locale`, `timeZone`, `zoom`, `brush`, `onBrush`, ... — see `core/fields.ts`). `zoom: true` enables wheel/drag zoom on x (double-click resets); `brush: true` reports the selected x range (Dates) through `onBrush`.

## Example
```ts
import { chart } from '.omp/charts'
chart('line', el, csv, { fields: { x: 'date', y: 'sessions' }, yTitle: 'Sessions', zoom: true })
```

## Edge cases
Empty → empty state. Single row → one dot on a padded axis. Zeros / all-equal → a flat line on a padded domain. Negatives → below the zero rule. Nulls → gaps (or per `gaps`) + note. Duplicate x → summed + note. 5,200 rows → canvas. Unsorted dates and mixed time zones → sorted by instant. Long / RTL axis titles → truncated with the full text on hover. Outliers → a tall linear domain (the spike stays readable).
