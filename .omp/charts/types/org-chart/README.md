# Org chart (`org-chart`)

One box per person (name, title) under their manager, top-down, joined by elbow connectors.

## When to use
- Reporting lines: who reports to whom, team sizes, titles.
- Up to a few hundred people; larger orgs still render (boxes shrink, text moves to tooltips).

## When not to use
- A hierarchy without people/titles, or where depth is the point: `tree` or `dendrogram`.
- Head-count or budget sizes per team: `treemap`, `icicle` or `circle-packing`.

## Input
Rows of `{ id, parent, name, title? }`; the top person has an empty `parent`:
```json
[{ "id": "ceo", "parent": null, "name": "Ada Park", "title": "CEO" }, { "id": "cto", "parent": "ceo", "name": "Ben Ito", "title": "CTO" }]
```
Also accepted: CSV text, column arrays, a field mapping (`{ fields: { id: 'emp', parent: 'manager', name: 'full_name' } }`)
and a nested `{ name, title?, children }` object. Cycles and unknown managers are errors that name the row.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `colorBy` | `single \| branch` | `branch` | one colour, or one per executive's organisation |
| `separator` | string | `/` | path separator for path rows |

Every chart also takes the base options (`valueFormat`, `locale`, `theme`, `patterns`, `title`,
`onClick`, ... — see `core/fields.ts`).

## Layout
Always fits the width -- never a scrollbar. When a row of reports is too wide it wraps into
several rows hanging off a side trunk; box width and an overall scale are chosen together so
boxes stay as large as the space allows. Text below a readable size is dropped (noted) and
stays in the tooltip and keyboard label.

## Example
```ts
chart('org-chart', el, csv, { fields: { id: 'emp', parent: 'boss' }, colorBy: 'single' })
```

## Edge cases
Empty → empty state. Single → one box. Duplicate names under one manager → separate boxes + note.
Null name → the id; null title → left off. Zero values → ignored (values only appear in tooltips).
Forty reports under one lead → wrapped rows. Nine-level chain → fits by scaling. 5,200 people → canvas, tiny boxes, no text (noted), names on hover.
Branch colours apply only when the top-level reports have teams of their own (a flat team is one colour).
Long/RTL names and titles → truncated in the box, full text on hover.
