# The graph

One graph of the code and the design, queried before anything is grepped.
`.omp/graph/graph.json` is graphify's AST graph of the whole repo (Python and
TypeScript; 10.5k nodes) with the design pack's nodes merged in: every token,
icon, registered component and spec anchor, and the edges that join them to
the files that use them.

## Rebuild

```
cd .omp && npm run graph:update   # incremental: extract . --code-only (manifest cache), ~10 s
cd .omp && npm run graph:build    # full: the same with --force (GRAPHIFY_FORCE=1), ~20 s
```

Both run `graph/build.mjs`: `graphify extract . --code-only` into
`GRAPHIFY_OUT=.omp/graph/graphify-out`, `graphify cluster-only --no-label
--no-viz` for `GRAPH_REPORT.md`, `graph/design-graph.mjs` to add the design
nodes and write `graph.json`, `graphify god-nodes --json` into
`god-nodes.json`, and `graph/diagram.mjs` to regenerate the architecture
diagram in `design/DESIGN.md`. No LLM, no API key, no network.
`PYTHONHASHSEED=0` keeps the community numbering stable between runs. The
`tool_result` hook in `extensions/design-pack` runs `graph:update` two seconds
after an edit under `control_plane/**`, `ui/src/**` or `.omp/design/**`.

`graphify update` is deliberately not used: it also indexes every markdown
heading (1,027 nodes here) and would disagree with the code-only extract.

## Layout

```
.omp/graph/graphify-out/   graphify's own output directory; gitignored.
                           Its basename is in graphify's skip set, so nothing
                           under it is ever indexed.
.omp/graph/graph.json      code + design — the file every query reads (committed)
.omp/graph/design-graph.json  the design part alone (committed)
.omp/graph/god-nodes.json  the 25 most connected nodes (committed)
.omp/graph/GRAPH_REPORT.md graphify's community report (committed)
.graphifyignore            what extraction skips; gitignore syntax, additive.
```

`graphify merge-graphs` is not used for the design merge: it is a cross-repo
tool that prefixes every node id with a repo tag and flattens the result to an
undirected simple graph, which breaks the id-based `defined_in` links and the
direction `affected` depends on. `design-graph.mjs` appends nodes and edges in
graphify's own node-link shape instead, ids preserved.

## What the design nodes are

| Node | id | label | edges out |
|---|---|---|---|
| token | `design_token_<name>` | `--name` | `semantic_of` → primitive, `component_of` → semantic |
| primitive | `design_primitive_<path>` | `color.stone.900` | — |
| icon | `design_icon_<name>` | `icon:<name>` | — |
| component | `design_component_<name>` | `component:<Name>` | `defined_in` → code symbol, `specified_in` → spec, `tested_by` → check, `uses_token`, `uses_icon`, `renders` → component |
| spec | `design_spec_<anchor>` | `DESIGN.md#anchor` | — |
| test | (the code graph's file node) | `readout.check.mjs` | `references` → component |

Every file under `ui/src` and `.omp/tests` gets `uses_token` edges for each
`var(--name)` / `tokens.<theme>.name` it contains and `uses_icon` for each
`<Icon name="…">`. Files that import a component's module get a `references`
edge to the component node; that is the edge `graphify affected` walks.
Everything is `EXTRACTED`: each edge is a line grep found.

## Queries

```
graphify query "which components use --fault" --graph .omp/graph/graph.json
graphify affected "component:Readout" --graph .omp/graph/graph.json      # importers + readout.check.mjs
graphify explain "design_token_live" --graph .omp/graph/graph.json       # by id: the label "--live" fuzzy-matches .live()
graphify path "component:Readout" "--fault" --graph .omp/graph/graph.json
graphify god-nodes --graph .omp/graph/graph.json --top 10
```

From omp: the `graph_query` tool, or `/graph <question>` and `/impact <target>`
(extension `design-pack`).

## The rule

Query the graph before grepping. A grep answers "where does this string
occur"; the graph answers "what depends on this", "what does this component
spend", "which check covers it" — and it answers across the Python and the
TypeScript at once. `.omp/RULES.md` carries the same rule for omp.
