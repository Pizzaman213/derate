# Table heatmap (`table-heatmap`)

A data table drawn as a grid: a header row, text cells, and numeric columns shaded by value --
each column on its own range, or every number on one shared range.

## When to use
- A small-to-medium table where the reader needs both the exact numbers and the pattern:
  KPIs by region, monthly temperatures by city, test scores by student.
- Columns on different units (revenue, margin, NPS): `scale: 'column'` shades each on its own range.
- Columns on one unit (months of the same measure): `scale: 'table'` makes them comparable.

## When not to use
- Two categorical dimensions and one value (long-format rows): `heatmap`.
- Only the numbers matter: a plain table (`showTable` on any chart).
- One or two numeric columns: `bar` / `grouped-bar` read faster than colour.
- Correlations between the numeric columns: `correlation-matrix`.

## Input
Rows of named cells (text, numbers, booleans or empty). A column is numeric when every
non-empty cell is a number; the first text column names the rows in labels and the summary.
```json
[{ "Region": "North", "Revenue": 1240, "Margin": 0.18 }, { "Region": "South", "Revenue": 980, "Margin": 0.22 }]
```
Also accepted: CSV text (numbers coerced) and column arrays (`{ "Region": [...], "Revenue": [...] }`).

## Options
| Option | Type | Default | |
|---|---|---|---|
| `columns` | string[] | every column | columns to show, in order (an unknown name is an error that lists the real ones) |
| `scale` | `column \| table` | `column` | shade each numeric column on its own range, or all numbers on one |
| `diverging` | boolean | auto | force or forbid the diverging ramp; default: diverging where values span zero |
| `showValues` | boolean | `true` | cell text; dropped when rows are too short, and numbers that do not fit are left to the tooltip rather than cut |

Base options (`valueFormat`, `locale`, `theme`, `renderer`, ...) are in `core/fields.ts`.

## Example
```ts
import { chart } from '.omp/charts'
chart('table-heatmap', el, csvText, { scale: 'table', valueFormat: '.1f' })
```

## Layout
Column widths follow their content (header and every cell). When the table is wider than the
chart, the row-name column keeps up to 35% of the width and the widest columns are capped first;
text that still does not fit ends in an ellipsis with the full text on hover. Rows are up to
2.4 lines tall and shrink to colour strips (down to 1 px, text dropped) before any are left out;
past that the first rows are shown with a note saying how many are not.

## Edge cases
Empty → empty state. One row → one row of cells. Zeros → every numeric cell at the ramp
midpoint. Negatives → diverging ramp per column. Nulls → hatched numeric cells with "–", noted.
Duplicate rows → kept as rows. 275 × 20 (5,500 cells) → canvas, strips without text. Long
headers and row names → ellipsis, full text on hover. RTL/CJK row names render. Outliers →
one dark cell, the rest of that column compressed (use `scale: 'column'` to keep other columns readable).
