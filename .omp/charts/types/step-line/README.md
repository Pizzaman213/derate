# Step line chart (`step-line`)

Values that hold until they change — prices, rates, plan tiers, stock levels — drawn as horizontal steps.

## When to use
- A value that is constant between change events (policy rate, list price).

## When not to use
- Continuously varying measures: `line`. Discrete events without a level: `timeline`.

## Input
Rows of `{ x, y, series? }`. `x` is a date (Date, ISO 8601, epoch, written forms like `Mar 2026` or `2026-Q1`) or a plain number. Rows are sorted by x, so order does not matter, and instants written with different offsets compare correctly. Give `series` for one stepped line per series (legend + end labels).
```json
[{ "x": "2025-01-01", "y": 4.5 }, { "x": "2025-06-18", "y": 4.25 }]
```
Also accepted: CSV text, column arrays (`{ "x": [...], "y": [...] }`) and any column names through a field mapping (`fields: { x: "date", y: "revenue" }`).

## Options
| Option | Type | Default | |
|---|---|---|---|
| `step` | `after \| before \| center` | `after` | where the value changes: at its own x, at the previous x, or halfway |
| `gaps` | `gap \| zero \| interpolate` | `'gap'` | missing values: lift the pen, draw 0, or join the neighbours (ends stay gaps) |
| `xType` | `auto \| time \| number` | `auto` | numbers stay numeric unless they look like epoch ms |
| `points` | `auto \| always \| never` | `'auto'` | dots on each value (`auto`: while sparse); hover and keyboard work either way |
| `xTitle`, `yTitle` | string | — | axis titles |

Every chart also takes the base options (`valueFormat`, `locale`, `timeZone`, `zoom`, `brush`, `onBrush`, ... — see `core/fields.ts`). `zoom: true` enables wheel/drag zoom on x (double-click resets); `brush: true` reports the selected x range (Dates) through `onBrush`.

## Example
```ts
import { chart } from '.omp/charts'
chart('step-line', el, csv, { fields: { x: 'date', series: 'plan', y: 'price' }, valueFormat: 'currency' })
```

## Edge cases
Empty → empty state. Single row → one dot on a padded axis. Zeros / all-equal → a flat line on a padded domain. Negatives → below the zero rule. Nulls → gaps (or per `gaps`) + note. Duplicate x → summed + note. 5,200 rows → canvas. Unsorted dates and mixed time zones → sorted by instant. Long / RTL axis titles → truncated with the full text on hover. Outliers → a tall linear domain (the spike stays readable).
