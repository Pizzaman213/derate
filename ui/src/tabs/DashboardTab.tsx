import { useState } from 'react'
import { useCluster, useRouting, useTopology } from '../state/resources'
import { useMetrics } from '../state/metrics'
import { useTelemetrySeries } from '../state/telemetry'
import { useSelection } from '../state/selection'
import { AggregateRow } from './dashboard/AggregateRow'
import { DeploymentsStrip } from './dashboard/DeploymentsStrip'
import { TelemetrySub } from './dashboard/TelemetrySub'
import { LoadSub } from './dashboard/LoadSub'
import { HeadroomSub } from './dashboard/HeadroomSub'

type Sub = 'overview' | 'headroom' | 'telemetry' | 'load'

const SUBS: { id: Sub; label: string }[] = [
  { id: 'overview', label: 'Overview' },
  // Second, because it answers the planner's question in reverse -- not "does
  // this fit" but "what fits". The planner itself is on the model's screen.
  { id: 'headroom', label: 'Headroom' },
  { id: 'telemetry', label: 'Telemetry' },
  { id: 'load', label: 'Load' },
]

/** The Dashboard destination: four sub-tabs (`.subs`) -- what is running, what
 *  it is doing right now, what would still fit, and who it is doing it for.
 *
 *  Planning is not here. The bar that used to sit above these tabs, always
 *  visible, was a second copy of `tabs/models/ServePanel.tsx`: the same
 *  `POST /api/plan`, the same `Verdict` box. The copy on the model's own URL
 *  is the better one, because it carries the question in the URL (`?ctx=`,
 *  `?seq=`, the degrees) and this one held it in component state, so a plan
 *  asked here could not be linked to. One plan surface, on the screen that
 *  names the model.
 *
 *  Ported from mockups-next/js/dashboard.js. The accumulated 60-second window
 *  used to be built here and drilled down from here, which made it reachable
 *  only from this destination; it now sits beside the SSE subscription in
 *  TelemetryProvider, because the node sheet draws it too and is not inside
 *  this tab. */
export function DashboardTab() {
  const [sub, setSub] = useState<Sub>('overview')
  const cluster = useCluster()
  const topology = useTopology()
  const routing = useRouting()
  const { frame } = useMetrics()
  const selection = useSelection()

  const telemetry = useTelemetrySeries()

  return (
    <div style={{ display: 'grid', gap: 'var(--s-4)' }}>
      <div className="subs" role="tablist" aria-label="Dashboard sections">
        {SUBS.map((s) => (
          <button key={s.id} role="tab" aria-selected={sub === s.id} onClick={() => setSub(s.id)}>
            {s.label}
          </button>
        ))}
      </div>

      <div hidden={sub !== 'headroom'}>
        <HeadroomSub context={8192} concurrency={1} />
      </div>

      <div hidden={sub !== 'overview'} style={{ display: 'grid', gap: 'var(--s-4)' }}>
        <AggregateRow cluster={cluster.data} frame={frame} />
        <section>
          <h2>Deployments</h2>
          <DeploymentsStrip
            deployments={cluster.data?.deployments ?? []}
            topologyDeployments={topology.data?.deployments ?? []}
            frame={frame}
            routing={routing.data ?? []}
            telemetry={telemetry}
            selection={selection}
          />
        </section>
      </div>

      <div hidden={sub !== 'telemetry'}>
        <TelemetrySub nodes={cluster.data?.nodes ?? []} telemetry={telemetry} selection={selection} />
      </div>

      <div hidden={sub !== 'load'}>
        <LoadSub routing={routing.data ?? []} nodes={cluster.data?.nodes ?? []} />
      </div>
    </div>
  )
}
