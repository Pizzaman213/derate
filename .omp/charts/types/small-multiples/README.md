# Small multiples (`small-multiples`)

One small chart per value of a facet column, in a grid that fits the width, sharing the y scale by default so panels compare at a glance. The data marks are the child charts' own.

## When to use
- The same chart for several groups (regions, nodes, products) where comparing shapes matters.
- Too many series for one chart to stay readable.

## When not to use
- Two or three series that compare directly on one axis: `grouped-bar`, `multi-line`.
- One value per group: `bar`.

## Input
Rows for the child chart plus the facet column. Every alternate rows form works (CSV, column arrays); child field mappings go in `chartOptions.fields`.
```json
[{ "region": "North", "label": "Q1", "value": 120 }, { "region": "South", "label": "Q1", "value": 80 }]
```

## Options
| Option | Type | Default | |
|---|---|---|---|
| `facet` | string | (required) | the column whose values each get a panel (first-seen order; blank → “(blank)”) |
| `chart` | chart id | `bar` | the panels' chart; `bar` and `line` are always registered, others once imported |
| `columns` | integer | auto | panels per row; auto picks the largest panels that fit (180 px minimum) |
| `sharedScale` | `y \| x \| both \| none` | `y` | axes every panel shares (x: numeric or date extents only) |
| `valueField` | string | child's `y`/`value` mapping, else `y`, else `value` | column the shared y scale spans |
| `xField` | string | child's `x` mapping, else `x` | column the shared x scale spans |
| `yZero` | boolean | `true` | include 0 in the shared y scale |
| `chartOptions` | object | `{}` | options for every panel |

Every chart also takes the base options (`valueFormat`, `locale`, `currency`, `theme`, `patterns`, `title`, `onClick`, ... — see `core/fields.ts`).

## Example
```ts
chart('small-multiples', el, csv, { chart: 'line', facet: 'node', chartOptions: { fields: { x: 'day', y: 'tokens_per_s' } } })
```

## Edge cases
Empty → empty state. Missing facet column, unknown child chart, or itself as the child → a validation error naming it.
Child notes (nulls, duplicates) are prefixed with the facet. Over 5,000 child marks → canvas. Too many facets → “+N more” panel.
Long facet titles truncate with the full text on hover. Child legends are merged into one. Legend toggles hide series marks, but children that re-layout around hidden series do not see the toggle.
