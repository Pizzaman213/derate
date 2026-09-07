import { useCluster } from '../../state/resources'
import { gbytes } from '../../format'

// Ported from mockups-next/js/settings.js `settings()`'s `clusterCard` block.
// The join token row is gone: it names a real field (a secret, no less) that
// no endpoint here ever returns, so the row would either be fabricated or
// broken.
//
// A "Gateway" row is dropped too, on the same principle, not just the same
// paragraph: ClusterSummary (api/types.ts) carries no gateway address, so
// the only candidate value is the browser's own window.location.host. That
// is not a coordinator-reported fact -- it is same-origin with the gateway
// only by deployment convention, and it is actively wrong under `npm run
// dev`, where the UI is served from Vite's own port and proxies to the real
// gateway. A row that reads correctly in production and lies in dev is
// worse than no row; add a gateway field to /api/cluster before reviving it.
export function ClusterCard() {
  const cluster = useCluster()
  const s = cluster.data?.summary

  const rows: [string, string][] = [
    ['Cluster id', s?.cluster_id ?? '—'],
    ['Coordinator', s?.coordinator || '—'],
    ['Nodes', s ? `${s.node_count} · ${s.healthy_count} healthy` : '—'],
    ['Total memory', s ? `${gbytes(s.total_addressable_memory, 0)} GiB` : '—'],
    ['Discovery', 'mDNS · _derate._tcp.local.'],
  ]

  return (
    <div className="card2">
      <h3>Cluster</h3>
      <div>
        {rows.map(([k, v]) => (
          <div className="row" key={k}>
            <span>{k}</span>
            <span className="mono">{v}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
