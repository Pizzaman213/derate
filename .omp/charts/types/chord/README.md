# Chord diagram (`chord`)

Flows between groups around a circle: each group's arc is as long as everything it sends,
and a ribbon joins every pair, each end as wide as that direction's flow.

## When to use
- Two-way flows among a handful to a few dozen groups: migration between regions, trade
  between countries, switching between brands.
- Showing which pairs dominate and whether flows are balanced in both directions.

## When not to use
- Flows that only go forward through stages: `sankey` or `alluvial`.
- Exact values matter more than the pattern: `adjacency-matrix` or `table-heatmap`.
- Unweighted connections: `arc-diagram` or `force-network`.
- More than ~40 groups: labels are dropped and ribbons blur; filter or aggregate first.

## Input
A square matrix: `matrix[i][j]` is the flow from `names[i]` to `names[j]`.
```json
{ "names": ["Black", "Blond"], "matrix": [[11975, 5871], [1951, 10048]] }
```
Also accepted: a graph `{ nodes?, links: [{ source, target, value }] }`, link rows, CSV
text (`source,target,value`), column arrays, and any column names through
`fields: { source: 'from', target: 'to', value: 'movers' }`. Duplicate links are summed;
without values every link counts 1.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `sort` | `input \| descending \| ascending` | `input` | group order around the circle |
| `padAngle` | number (0–0.2 rad) | shrinks with group count | gap between groups |

Base options: see `core/fields.ts`.

## Example
```ts
chart('chord', el, { names, matrix }, { sort: 'descending', valueFormat: 'si' })
```

## Edge cases
Empty → empty state. All zeros → "All values are zero". One group → a ring and a
self-ribbon. Negative and missing cells → omitted with a note. Repeated names → rows and
columns merged, noted. Groups that send nothing → no arc, noted. 100 groups (5,050
ribbons) → canvas. Labels are horizontal, truncated toward the edge (full text on hover)
and dropped where they collide.
