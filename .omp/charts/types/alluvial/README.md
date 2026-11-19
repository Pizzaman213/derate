# Alluvial diagram (`alluvial`)

How one population divides across categorical stages shown in a fixed left-to-right
order; bands keep the colour of their first-stage category so a cohort can be followed.

## When to use
- The same people/items classified several ways: class → sex → survived, plan in
  January → April → July, intake → year 2 → outcome.
- Showing where a first-stage group ends up, stage by stage.

## When not to use
- Flows between arbitrary nodes with no stage structure: `sankey`.
- Two stages only and you need exact values: `table-heatmap` or `stacked-bar-100`.
- Flows that loop back: `chord`.
- More than ~12 categories per stage: the tail is grouped as "Other"; consider filtering first.

## Input
Rows with one column per stage and an optional `value` weight (without a value column every
row counts 1, so raw survey answers work as they are).
```json
[{ "Class": "First", "Sex": "Female", "Survived": "Yes", "value": 141 }]
```
Also accepted: CSV text, column arrays, and a field mapping for the weight
(`fields: { value: 'customers' }`). Stage columns default to every column except the value
(and mapped source columns) in order; pick and order them with `columns`.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `columns` | string[] | every non-value column | stage columns, left to right |
| `maxCategories` | integer ≥ 2 | `12` | categories kept per stage; the smallest rest become "Other" (neutral grey, noted) |
| `nodeSort` | `input \| size \| name` | `input` | order of categories within a stage |
| `showValues` | boolean | `false` | category total after each label |

Base options: see `core/fields.ts`.

## Example
```ts
chart('alluvial', el, csvText, { fields: { value: 'customers' }, columns: ['January', 'April', 'July'] })
```

## Edge cases
Empty → empty state. All zeros → "All values are zero". One row → one band per stage gap.
Negative or missing values → left out with a note. Duplicate rows → summed, noted. 600
categories in a stage → the 11 largest plus "Other", noted. Long and RTL labels truncate
to the gap between stages (full text on hover) and are dropped where they collide.
