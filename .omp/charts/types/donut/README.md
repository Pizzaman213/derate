# Donut chart (`donut`)

A ring of slices with the total of the visible slices printed in the hole.

## When to use
- Two to six parts of one total, when the total itself matters too.

## When not to use
- More than six parts or precise comparison: `bar`, `horizontal-bar`.
- Little vertical room: `semi-donut`.
- Composition over time or across groups: `stacked-bar-100`.

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
| `centerLabel` | string | `Total` | caption under the centre total; `''` hides the total |

Every chart also takes the base options (`valueFormat`, `locale`, `currency`, `theme`,
`patterns`, `title`, `legend`, ... — see `core/fields.ts`).

## Example
```ts
chart('donut', el, { label: services, value: costs }, { valueFormat: 'currency', centerLabel: 'This month' })
```

## Edge cases
Empty → empty state. All zeros → empty state "All values are zero". Negatives → omitted, noted.
Nulls → omitted, noted. Zeros among positives → not drawn, noted. Duplicates → summed, noted.
Large (240 categories) → the smallest grouped as "Other (n)", noted; the tooltip lists members.
Long labels → names truncated before the share (full text on hover); RTL/CJK render as given.
Outliers → the tiny slices stay hoverable; labels that cannot fit are dropped (tooltips keep them).
Legend toggle hides a slice and re-normalises the shares of the rest.
The centre total shrinks from the readout size to fit the hole (it fits at 320px) and follows the legend toggle.
