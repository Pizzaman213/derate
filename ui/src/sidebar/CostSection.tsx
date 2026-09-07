import type { RouteTarget, TopologyDeployment } from '../api/types'
import type { SafeMetricsFrame } from '../state/useMetrics'
import { useSelection } from '../state/selection'
import { useRouting, useSettings, useTopology } from '../state/resources'
import { useMetrics } from '../state/metrics'

// Ported from mockups-next/js/sidebar.js `sidebar()`'s cost block. The mockup
// computes local cost client-side from watts/tok-s/rate because that is the
// only way its fixture can show the provenance line ("from N W at N tok/s");
// the real RouteTarget.cost_per_mtok is computed the same way server-side,
// but from a settings-poll snapshot rather than the live frame, so this keeps
// the same client-side arithmetic against the live metrics stream instead of
// trusting the wire's own number here.
function targetLabel(t: RouteTarget): string {
  if (t.kind === 'local' && t.node_ids && t.node_ids.length > 0) return t.node_ids.join(' + ')
  return t.target_id
}

export function CostSection() {
  const { selDep } = useSelection()
  const routing = useRouting()
  const settings = useSettings()
  const topology = useTopology()
  const { frame } = useMetrics()

  const cfg = selDep ? (routing.data?.find((c) => c.served_name === selDep) ?? null) : null

  if (!cfg) {
    return (
      <section>
        <h2>Cost per Mtok</h2>
        <p className="unit">Nothing is being served.</p>
      </section>
    )
  }

  const rate = settings.data?.electricity_rate_usd_per_kwh ?? 0
  const rateSet = rate > 0

  return (
    <section>
      <h2>Cost per Mtok · {cfg.served_name}</h2>
      <div style={{ display: 'grid', gap: 2 }}>
        {cfg.targets.map((t) => (
          <CostRow
            key={t.target_id}
            target={t}
            rate={rate}
            rateSet={rateSet}
            topologyDeployments={topology.data?.deployments ?? []}
            frame={frame}
          />
        ))}
      </div>
    </section>
  )
}

function CostRow({
  target: t,
  rate,
  rateSet,
  topologyDeployments,
  frame,
}: {
  target: RouteTarget
  rate: number
  rateSet: boolean
  topologyDeployments: TopologyDeployment[]
  frame: SafeMetricsFrame | null
}) {
  const label = targetLabel(t)

  if (t.kind === 'remote') {
    return (
      <div>
        <div className="row">
          <span>{label}</span>
          <span className="mono">{t.cost_per_mtok != null ? `$${t.cost_per_mtok.toFixed(3)}` : '—'}</span>
        </div>
        <div className="unit" style={{ margin: '-4px 0 6px' }}>published by the provider</div>
      </div>
    )
  }

  if (!rateSet) {
    return (
      <div>
        <div className="row">
          <span>{label}</span>
          <span className="mono">—</span>
        </div>
        <div className="unit" style={{ margin: '-4px 0 6px' }}>
          set an electricity rate to price local generation
        </div>
      </div>
    )
  }

  const nodeIds = t.node_ids ?? []
  const watts = nodeIds.reduce((sum, id) => {
    const w = frame?.nodes.find((n) => n.node_id === id)?.power_w
    return sum + (w ?? 0)
  }, 0)
  const hasWatts = nodeIds.some((id) => frame?.nodes.find((n) => n.node_id === id)?.power_w != null)

  const depFrame = frame?.deployments.find((d) => d.deployment_id === t.target_id)
  const tps =
    depFrame?.tokens_per_sec ??
    topologyDeployments.find((d) => d.deployment_id === t.target_id)?.tokens_per_sec ??
    null

  if (!hasWatts || tps == null || tps <= 0) {
    return (
      <div>
        <div className="row">
          <span>{label}</span>
          <span className="mono">—</span>
        </div>
      </div>
    )
  }

  const usd = ((watts / 1000) * rate / (tps * 3600)) * 1_000_000

  return (
    <div>
      <div className="row">
        <span>{label}</span>
        <span className="mono">${usd.toFixed(3)}</span>
      </div>
      <div className="unit" style={{ margin: '-4px 0 6px' }}>
        from {Math.round(watts)} W at {tps.toFixed(0)} tok/s · ${rate.toFixed(2)}/kWh
      </div>
    </div>
  )
}
