# Euler diagram (`euler`)

Up to eight sets as circles sized by count, overlapping where they share members and apart where they do not.

## When to use
- An overview of how four to eight groups relate: which overlap, which are nested, which
  are disjoint.

## When not to use
- Exact overlap sizes, or more than eight sets: `upset`.
- Two or three sets where every pairwise overlap must be exact: `venn`.

## Input
Rows of `{ sets, size }` with **inclusive** sizes (venn.js convention: the `'A,B'` row counts
everything in both A and B). Overlaps that are not listed are empty. Every set in an overlap
needs a row of its own.
```json
[{ "sets": "SQL", "size": 1420 }, { "sets": "Python", "size": 1260 }, { "sets": "SQL,Python", "size": 1010 }]
```
`sets` may be `'A,B'`, `'A;B'`, `'A|B'` or a list. Also accepted: venn.js's
`{ sets: [{ sets: ['A'], size }, ...] }`, CSV text, column arrays and a field mapping.
More than eight sets is a validation error that recommends `upset`.

## Layout
`@upsetjs/venn.js` lays the circles out (pure layout functions only: `venn()` with its greedy
initial layout -- the default switches to a randomly restarted MDS at 8+ areas, and a chart
must draw the same way every time -- then `normalizeSolution`, which packs disjoint clusters).
Circle areas are exact; overlaps are a best fit. The chart measures the drawn pairwise
overlaps against the requested ones, and when they miss by more than 5% of the total area
the note says so. Overlaps the layout could not draw at all are counted in the note.

Regions are labelled with exclusive counts when the listed sizes pin them down; when they do
not (higher-order overlaps not listed), each listed size is shown in its own region instead,
with a note. Labels are dropped where they would not fit; tooltips and the table keep every value.

## Options
No chart-specific options; base options as for every chart (`core/fields.ts`). The legend
toggles a set and re-lays out the rest.

## Example
```ts
import { chart } from '.omp/charts'
chart('euler', el, { sets: [{ sets: ['SQL'], size: 1420 }, { sets: ['Python'], size: 1260 }, { sets: ['SQL', 'Python'], size: 1010 }] })
```

## Edge cases
Empty → empty state; all zeros → "All set sizes are zero". Negatives and nulls → left out,
noted. Duplicate combinations → summed, noted. Disjoint sets → separate circles, never
overlapping. Nested sets → circles inside each other. Eight sets → approximate layout, loss
noted when high. Long names → truncated inside their region, full text in the legend.
RTL/CJK/emoji names render. A set 1,000× the others → the small circles are tiny but drawn.
