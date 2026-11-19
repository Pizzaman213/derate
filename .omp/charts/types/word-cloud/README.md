# Word cloud (`word-cloud`)

Words sized by frequency or weight, packed around the centre.

## When to use
- A first impression of which terms dominate free text (survey answers, reviews, tickets).
- Decorative summaries where the exact values do not matter.

## When not to use
- Comparing values precisely: `horizontal-bar` (sorted), which reads length instead of font size.
- Fewer than ten terms: a bar chart says the same thing more clearly.

## Input
Rows of `{ text, value, group? }`; duplicate texts are summed (noted). Or prose: a plain
string, or `{ text: "..." }`, is tokenised into words, folded to lower case (shown in the
commonest spelling), stripped of a small English stopword list, one-letter tokens and bare
numbers, and counted.
```json
[{ "text": "latency", "value": 96 }, { "text": "throughput", "value": 71 }]
```
CSV text with a `text` column (or any columns through a field mapping:
`{ fields: { text: 'term', value: 'count' } }`) and column arrays are accepted. An optional
`group` column colours words by group with a legend (toggling a group re-lays the cloud).

## Layout
Font size follows a square-root scale from `minFontSize` (never below the tick size) to
`maxFontSize`, in half-pixel steps (every distinct size is a font the browser must load
before it can measure). Words are placed largest first along a deterministic Archimedean
spiral (stretched to the frame's aspect) from the centre, each at the first point where its
measured box is inside the frame and overlaps no placed word; the spiral steps about a
quarter of the word's smaller side, and collision tests only visit placed boxes in nearby
grid cells. Without a `maxFontSize` the scale is fitted so the words cover about 40% of the
frame and is shrunk while more than 5% of the words find no room -- an attempt stops as soon
as it is over that. Words that still do not fit are left out and counted in the
note; the table always has every word. d3-cloud is not used: its pixel collision needs a
canvas that jsdom lacks, so the tests could not check the real layout.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `maxWords` | integer | `150` | largest words kept; the rest are noted |
| `minFontSize` | px | tick size | smallest word |
| `maxFontSize` | px | fitted | largest word |
| `rotate` | `none \| mixed` | `none` | `mixed` turns about a third of the words 90° (never the largest) |
| `colorBy` | `cycle \| single` | `cycle` | palette cycle or one colour (a `group` column always colours by group) |
| `stopwords` | string[] | `[]` | extra words to drop when counting prose |

Base options as for every chart (`core/fields.ts`); `seed` changes the spiral start angles.

## Example
```ts
import { chart } from '.omp/charts'
chart('word-cloud', el, surveyAnswersText, { maxWords: 60, stopwords: ['product'] })
```

## Edge cases
Empty → empty state; all zero → "All values are zero". Nulls, zeros and negatives → left
out, noted. Duplicates → summed, noted. 5,200 terms → the 150 largest placed, the rest
noted. Long phrases → truncated to the frame, full text on hover. RTL/CJK/emoji words render.
One weight 1,000× the rest → the square-root scale keeps the others at a legible minimum.
