# Funnel chart (`funnel`)

Ordered stages as centred bars whose widths are proportional to how many reached each
stage, joined by narrowing necks, with conversion from the first stage and from the previous one.

## When to use
- A process people drop out of: visits → cart → checkout → purchase; signup → activation → paid.

## When not to use
- Unordered categories: `bar` (a funnel implies sequence).
- Parts of one total: `pie`, `waffle` (funnel stages are not shares of anything).
- Flows that branch: `sankey`.

## Input
Rows of `{ label, value }` **in stage order** (never re-sorted).
```json
[{ "label": "Visits", "value": 12000 }, { "label": "Purchase", "value": 640 }]
```
Also accepted: CSV text, column arrays, and a field mapping: `{ fields: { label: 'stage', value: 'users' } }`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `showConversion` | `both \| first \| previous \| none` | `both` | conversion printed beside each stage |

Every chart also takes the base options (`valueFormat`, `locale`, `theme`, `patterns`, ... — see `core/fields.ts`).

## Conversion
`of first = value / first visible stage`, `of previous = value / previous visible stage`;
undefined (shown as "–") when the denominator is zero. Values may rise between stages
(conversion above 100% is shown as is). Bar widths are proportional to value over the largest stage.

## Example
```ts
chart('funnel', el, { stage, users }, { fields: { label: 'stage', value: 'users' } })
```

## Edge cases
Empty / all zeros → empty state. Negatives and nulls → omitted, noted. Zero stages → 1px stubs.
Duplicates → summed, noted. 60 stages → thin rows, stage labels thinned (every k-th), no legend
above 12 stages. Long labels → truncated (full text on hover). The legend toggle hides a stage and
recomputes every conversion against the visible stages.
