# Pie chart (`pie`)

Slices of one whole, largest first from 12 o'clock; each angle is the part's share of 2π.

## When to use
- Two to six parts of a single total, where "about a third" is the message.

## When not to use
- More than six parts, or parts that need precise comparison: `bar` / `horizontal-bar`.
- A total you also want to read: `donut` (total in the hole).
- Composition across several groups or over time: `stacked-bar-100`.
- Many parts in a hierarchy: `treemap`.

## Input
Rows of `{ label, value }`.
```json
[{ "label": "Alpha", "value": 120 }, { "label": "Bravo", "value": 340 }]
```
Also accepted: CSV text (`"label,value\nAlpha,120"`), column arrays (`{ "label": [...], "value": [...] }`),
and any column names through a field mapping: `{ fields: { label: 'browser', value: 'share_pct' } }`.
Only positive values are drawn.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `maxSlices` | integer 2–24 | `6` | above this many slices the smallest are grouped into one "Other (n)" slice (noted) |
| `sort` | `none \| descending \| ascending` | `descending` | slice order, clockwise from 12 o'clock; "Other" stays last |
| `labels` | `auto \| outside \| inside \| none` | `auto` | `auto`: names + shares outside with leader lines when the width allows (≥ 400px), else shares inside slices that have room; the legend always names every slice |

Every chart also takes the base options (`valueFormat`, `locale`, `currency`, `theme`,
`patterns`, `title`, `legend`, ... — see `core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('pie', el, csv, { fields: { label: 'browser', value: 'share_pct' } })
```

## Edge cases
Empty → empty state. All zeros → empty state "All values are zero". Negatives → omitted, noted.
Nulls → omitted, noted. Zeros among positives → not drawn, noted. Duplicates → summed, noted.
Large (240 categories) → the smallest grouped as "Other (n)", noted; the tooltip lists members.
Long labels → names truncated before the share (full text on hover); RTL/CJK render as given.
Outliers → the tiny slices stay hoverable; labels that cannot fit are dropped (tooltips keep them).
Legend toggle hides a slice and re-normalises the shares of the rest.
