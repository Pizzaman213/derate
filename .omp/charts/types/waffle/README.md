# Waffle chart (`waffle`)

A grid of squares (10×10 by default) coloured by part, each part getting its
largest-remainder share of the squares so the counts always add up to the grid exactly.

## When to use
- Shares people should be able to count: "41 in 100 commute by car".
- A handful of parts, including small ones that would vanish as a pie slice.

## When not to use
- Precise comparison of similar parts: `bar`.
- Parts that change over time: `stacked-bar-100`.

## Input
Rows of `{ label, value }`.
```json
[{ "label": "Alpha", "value": 120 }, { "label": "Bravo", "value": 340 }]
```
Also accepted: CSV text (`"label,value\nAlpha,120"`), column arrays (`{ "label": [...], "value": [...] }`),
and any column names through a field mapping: `{ fields: { label: 'browser', value: 'share_pct' } }`.
Only positive values get squares.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `cells` | integer 4–10,000 | `100` | squares in the grid (100 = one square per percent) |
| `columns` | integer | √cells, rounded | squares per row |
| `maxSlices` | integer 2–24 | `8` | above this many parts the smallest are grouped as "Other (n)" |
| `sort` | `none \| descending \| ascending` | `descending` | fill order, row by row from the top left; "Other" last |

Every chart also takes the base options (`valueFormat`, `locale`, `currency`, `theme`,
`patterns`, `title`, `legend`, ... — see `core/fields.ts`).

## Allocation
Each part's exact quota is `value / total × cells`. Every part gets the floor of its quota;
the leftover squares go one each to the largest remainders (ties: the larger value, then the
earlier part). The counts therefore always sum to `cells`, and each one is its quota rounded
down or up. A part whose quota rounds to zero gets no square and is noted.

## Example
```ts
chart('waffle', el, csv, { fields: { label: 'mode', value: 'respondents' } })
```

## Edge cases
Empty / all zeros → empty state. Negatives, nulls, zeros → omitted, noted. Duplicates → summed, noted.
Large (300 parts, `cells: 6400`) → tail grouped as Other; 6,400 squares draw on canvas.
Long/RTL labels → legend ellipsises (full text on hover). Legend toggle re-allocates all squares among the visible parts.
