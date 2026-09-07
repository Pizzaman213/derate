import type { NodeStateDTO, RoutingConfig } from '../../api/types'
import { fromState, nodeName } from '../../state/names'

interface Props {
  routing: RoutingConfig[]
  nodes: NodeStateDTO[]
}

interface Column {
  /** target_id -- one column PER LOCAL TARGET, not per served name: two
   *  replicas of one model are separate deployments with separate counters
   *  on separate nodes, and merging them stamped replica A's requests under
   *  replica B's Sparks (wave-2 verifier D8). */
  key: string
  label: string
  nodeIds: Set<string>
  /** This target's own `counters.completed`; `null` = never selected,
   *  distinct from a real, measured zero. */
  completed: number | null
}

interface Row {
  node: NodeStateDTO
  cells: Map<string, { count: number | null; shared: boolean }>
  /** null only when every column this row spans is itself null (every local
   *  target under every served name touching this node has never been
   *  dispatched to) -- the row-level echo of the same distinction the cells
   *  already draw, so the total never claims "0" where the honest answer is
   *  "no data yet". */
  total: number | null
}

/** Request load by Spark, attributed by SHARING rather than dividing: the
 *  gateway keys request stats by target_id, not by physical node, and in
 *  pipeline parallel every request traverses every node in the plan. A
 *  deployment's completed count is therefore stamped under every node it
 *  spans, column totals legitimately exceed the request total, and the note
 *  below says so. Ported from mockups-next/js/dashboard.js `loadMatrix()`. */
export function LoadSub({ routing, nodes }: Props) {
  const cols = buildColumns(routing)

  const rows: Row[] = []
  for (const node of nodes) {
    const cells = new Map<string, { count: number | null; shared: boolean }>()
    let total = 0
    let hasReal = false
    let hits = 0
    for (const c of cols) {
      if (!c.nodeIds.has(node.profile.node_id)) continue
      hits += 1
      cells.set(c.key, { count: c.completed, shared: c.nodeIds.size > 1 })
      if (c.completed != null) {
        total += c.completed
        hasReal = true
      }
    }
    if (hits > 0) rows.push({ node, cells, total: hasReal ? total : null })
  }

  if (rows.length === 0) {
    return (
      <p className="label muted" style={{ fontWeight: 400, margin: 0 }}>
        Nothing is being served.
      </p>
    )
  }

  const real = cols.reduce((a, c) => a + (c.completed ?? 0), 0)
  const summed = rows.reduce((a, r) => a + (r.total ?? 0), 0)
  const shared = cols.some((c) => c.nodeIds.size > 1)

  return (
    <div>
      <div style={{ overflowX: 'auto' }}>
        <table>
          <thead>
            <tr>
              <th>Spark</th>
              {cols.map((c) => (
                <th key={c.key} style={{ textAlign: 'right' }}>
                  {c.label}
                </th>
              ))}
              <th style={{ textAlign: 'right' }}>Node total</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.node.profile.node_id}>
                <td className="mono">{nodeName(fromState(r.node))}</td>
                {cols.map((c) => {
                  const cell = r.cells.get(c.key)
                  return cell ? (
                    <td key={c.key} className="num">
                      {cell.count == null ? '—' : cell.count.toLocaleString()}
                      {cell.shared ? <span className="unit"> shared</span> : null}
                    </td>
                  ) : (
                    <td key={c.key} className="num muted">
                      —
                    </td>
                  )
                })}
                <td className="num">{r.total == null ? '—' : r.total.toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="unit" style={{ marginTop: 8 }}>
        {/* Counters run since the gateway started, not since midnight -- "today"
            would claim a reset that never happens. And this total is local-only
            (this table has no rows for what spilled to a remote provider), so
            the sentence says so rather than reading as a cluster-wide count. */}
        {shared
          ? `${real.toLocaleString()} requests to local Sparks since the gateway started. Node totals sum to ${summed.toLocaleString()} ` +
            `because a model spanning Sparks is counted on each — in pipeline parallel every ` +
            `request traverses every node, so the count is shared, not divided.`
          : `${real.toLocaleString()} requests to local Sparks since the gateway started.`}
      </p>
    </div>
  )
}

function buildColumns(routing: RoutingConfig[]): Column[] {
  const cols: Column[] = []
  for (const cfg of routing) {
    const local = cfg.targets.filter((t) => t.kind === 'local' && (t.node_ids?.length ?? 0) > 0)
    // One column per local target: each replica's count lands only on the
    // nodes in ITS plan. The label disambiguates only when a served name
    // genuinely has more than one local replica.
    for (const t of local) {
      cols.push({
        key: t.target_id,
        label: local.length > 1 ? `${cfg.served_name} · ${t.target_id}` : cfg.served_name,
        nodeIds: new Set(t.node_ids ?? []),
        completed: t.counters?.completed ?? null,
      })
    }
  }
  return cols
}
