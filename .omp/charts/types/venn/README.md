# Venn diagram (`venn`)

Two or three overlapping circles whose areas are exactly proportional to the set sizes and pairwise overlaps.

## When to use
- Showing how two or three groups overlap (audiences, skills, feature users) where the
  overlap itself is the story.

## When not to use
- More than three sets: `upset` (exact, any number of sets) or `euler` (approximate circles, up to 8).
- Reading exact sizes or comparing many overlaps: `upset` puts them on a common baseline.
- Parts of a single whole with no overlap: `pie`, `treemap`.

## Input
Rows of `{ sets, size }`, venn.js's convention: `size` is **inclusive** -- the row for
`'A,B'` counts every item in both A and B, whatever else it is in. Overlaps that are not
listed are empty. Every set named in an overlap needs a row of its own.
```json
[{ "sets": "A", "size": 60 }, { "sets": "B", "size": 80 }, { "sets": "A,B", "size": 20 }]
```
`sets` may be `'A,B'`, `'A;B'`, `'A|B'` or `["A", "B"]`. Also accepted: venn.js's
`{ sets: [{ sets: ['A'], size: 60 }, ...] }`, CSV text, column arrays, and a field mapping
(`{ fields: { sets: 'combination', size: 'respondents' } }`).

More than three sets is a validation error that recommends `upset` or `euler`.

## Layout
Radii are `sqrt(size / π)`, so circle areas are exact. Each pairwise centre distance is
solved by bisection on the circle-intersection (lens) area, so every pairwise overlap is
exact to well under 1%. With three sets the three distances fix the triangle, and the
three-way region is whatever that triangle produces: it is **approximate**, and the note
says so when it is off by more than 5% of the smallest set. Pairwise sizes that cannot
form a triangle are drawn as close as possible and noted.

Each region is labelled with its exclusive count (items in exactly those sets), a set's
own region with its name too, when the label fits inside the region; otherwise the
legend and tooltips carry it. If the listed sizes cannot all be exclusive-consistent,
labels show each listed (inclusive) size instead, with a note.

## Options
No chart-specific options. Every chart also takes the base options (`valueFormat`,
`locale`, `theme`, `patterns`, `title`, `onClick`, ... -- see `core/fields.ts`). The legend
toggles a set off and re-lays out the rest.

## Example
```ts
import { chart } from '.omp/charts'
chart('venn', el, 'combination,respondents\nJavaScript,6240\nPython,4870\nJavaScript|Python,2310\n', { fields: { sets: 'combination', size: 'respondents' } })
```

## Edge cases
Empty → empty state. All sizes zero → "All set sizes are zero". Negative or null sizes →
left out, noted; overlaps of a left-out set are not drawn. Duplicate combinations (`A,B`
and `B,A`) → summed, noted. An overlap larger than one of its sets → clamped, noted.
Disjoint sets → circles apart with a small gap; a subset → drawn inside its superset.
Long names → truncated inside the region, full text in legend and tooltip. Arabic, Hebrew,
CJK names render. A set 1,000× another → the small circle is tiny but drawn and hoverable.
