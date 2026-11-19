# datasets

Sample data for the chart pack: synthetic, deterministic, hand-plausible. Every chart
type has at least one dataset here it can render without inventing data.

| name | kind | rows | for |
| --- | --- | --- | --- |
| `sales-by-month` | rows | 96 | 24 months x 4 regions: revenue, units, target. Line, grouped/stacked bar, area, KPI. |
| `project-timeline` | rows | 14 | Launch plan with phases, owners, progress, dependencies, milestones. Gantt, swimlane. |
| `survey-results` | rows | 60 | 6 Likert questions x 5 levels x 2 segments. Diverging bar, 100% stacked bar. |
| `server-metrics` | rows | 864 | 3 hosts x 288 five-minute samples on 2026-11-16 UTC, an incident 13:50-14:45, 5 null gaps. |
| `population-by-region` | rows | 40 | Countries with ISO3, population, area, median age, centroid. Choropleth, bubble map, scatter. |
| `budget-breakdown` | tree | 46 | Nested `{name, children}` tree, 5 departments > 10 categories > 30 line items. Treemap, sunburst. |
| `budget-breakdown-flat` | rows | 46 | The same tree as `{id, parent, name, value}` rows (stratify-ready). |
| `network-graph` | graph | 30 | Service dependency map: 30 nodes in 4 groups, 61 weighted links. Force network, chord, sankey. |
| `org-tree` | rows | 35 | `{id, parent, ...}` org chart, CEO root, four levels. |
| `ohlc-prices` | rows | 120 | Weekday OHLC + volume from 2026-06-01, seeded random walk. Candlestick, OHLC. |

`datasets.json` is the registry and the source of truth for the table above; `index.ts`
exposes it typed (`DATASET_NAMES`, `DatasetEntry`, `datasetEntry(name)`,
`datasetsFor(intent)`) through a JSON import, so it works in the browser bundle too.

## File format

```jsonc
{
  "name": "sales-by-month",            // equals the file name and the registry entry
  "description": "...",
  "source": "synthetic, deterministic",
  "data": [ { ... } ],                  // rows array; a root node for kind "tree";
                                        // { nodes, links } for kind "graph"
  "fields": {                           // rows, tree nodes, or graph nodes
    "revenue": { "type": "number", "description": "...", "unit": "USD" }
  },
  "linkFields": { ... }                 // graph only: describes each link
}
```

Field `type` is one of `date` (ISO 8601; date-times carry `Z`), `number`, `category`,
`text`, `id`, `boolean`. Optional keys on a field:

- `unit` -- display unit.
- `nulls: N` -- the field holds exactly N nulls (gaps, or non-leaf rows of a flat tree).
- `nullable: true` -- null is legal but not counted (the root's `parent`).
- `on: "leaf"` -- tree datasets: set on leaves only; parents are the sum of their children.

Registry entries: `name`, `file` (`<name>.json`), `description`, `kind`
(`rows|tree|graph`), `rows` (rows, tree nodes, or graph nodes), `fields` (names, in file
order), `intents`, `suggested` (chart ids), `added` (YYYY-MM-DD).

## Validating

```bash
node datasets/validate.mjs [folder]   # node built-ins only; exit 1 lists every problem
npx vitest run datasets
```

The validator checks the registry against the folder in both directions, required keys,
row counts, every declared field on every record and its type, declared null counts, no
undeclared keys, id/parent rows (parents resolve, one root, no cycles), tree shape and
leaf-only fields, graph links resolving to node ids, and `low <= open,close <= high` on
any dataset with OHLC fields. Run it after adding a dataset.

## Size

Files stay under 60 KB except `server-metrics` (~98 KB): 864 rows of six keys each cannot
fit in 60 KB as row objects, and the 3 x 288 shape is the point of the dataset.
