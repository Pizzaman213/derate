# Semi-donut chart (`semi-donut`)

A half ring (180°, 9 to 3 o'clock, flat side down) of parts with the total in the hollow.

## When to use
- Two to six parts in a short, wide space (dashboards, cards); parliament-style seat splits (`sort: 'none'`).

## When not to use
- More parts or precise comparison: `bar`, `horizontal-bar`.
- A single value against a target: a gauge chart.

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
| `innerRadius` | number 0.3–0.85 | `0.6` | hole size as a share of the outer radius |
| `centerLabel` | string | `Total` | caption above the centre total; `''` hides the total |

Every chart also takes the base options (`valueFormat`, `locale`, `currency`, `theme`,
`patterns`, `title`, `legend`, ... — see `core/fields.ts`).

## Example
```ts
chart('semi-donut', el, csv, { fields: { label: 'party', value: 'seats' }, sort: 'none', centerLabel: 'Seats' })
```

## Edge cases
Empty → empty state. All zeros → empty state "All values are zero". Negatives → omitted, noted.
Nulls → omitted, noted. Zeros among positives → not drawn, noted. Duplicates → summed, noted.
Large (240 categories) → the smallest grouped as "Other (n)", noted; the tooltip lists members.
Long labels → names truncated before the share (full text on hover); RTL/CJK render as given.
Outliers → the tiny slices stay hoverable; labels that cannot fit are dropped (tooltips keep them).
Legend toggle hides a slice and re-normalises the shares of the rest.
