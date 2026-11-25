import { useState } from 'react'
import { useCluster, useRouting, useTopology } from '../state/resources'
import { ResourceScope } from '../state/backend'
import { useMetrics } from '../state/metrics'
import { useTelemetrySeries } from '../state/telemetry'
import { useSelection } from '../state/selection'
import { freeGiB, Tile } from './dashboard/AggregateRow'
import { DeploymentsStrip } from './dashboard/DeploymentsStrip'
import { TelemetrySub } from './dashboard/TelemetrySub'
import { LoadSub } from './dashboard/LoadSub'
import { HeadroomSub } from './dashboard/HeadroomSub'
import { UsageCard, UsageWindow } from './dashboard/UsageCard'
import { DEFAULT_DAYS, type UsageDays } from './dashboard/usageSummary'

type Sub = 'overview' | 'headroom' | 'telemetry'

const SUBS: { id: Sub; label: string }[] = [
  { id: 'overview', label: 'Overview' },
  // Second, because it answers the planner's question in reverse -- not "does
  // this fit" but "what fits". The planner itself is on the model's screen.
  { id: 'headroom', label: 'Headroom' },
  { id: 'telemetry', label: 'Telemetry' },
]

/** The Dashboard destination: three sub-tabs (`.subs`) -- usage at a glance,
 *  what would still fit, and what is running right now with where its load lands.
 *
 *  Planning is not here. The bar that used to sit above these tabs, always
 *  visible, was a second copy of the model pane's plan (now
 *  `tabs/models/useModelSession.ts`): the same `POST /api/plan`, the same
 *  verdict box. The copy on the model's own URL
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
  const [usageDays, setUsageDays] = useState<UsageDays>(DEFAULT_DAYS)
  const cluster = useCluster()
  const topology = useTopology()
  const routing = useRouting()
  const { frame } = useMetrics()
  const selection = useSelection()

  const telemetry = useTelemetrySeries()

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr)', gap: 'var(--space-16)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-12)', flexWrap: 'wrap' }}>
        <div className="subs" role="tablist" aria-label="Dashboard sections" style={{ flex: 1, margin: 0 }}>
          {SUBS.map((s) => (
            <button type="button" key={s.id} role="tab" aria-selected={sub === s.id} onClick={() => setSub(s.id)}>
              {s.label}
            </button>
          ))}
        </div>
        {sub === 'overview' ? <UsageWindow days={usageDays} onChange={setUsageDays} /> : null}
      </div>

      <div hidden={sub !== 'headroom'}>
        <ResourceScope active={sub === 'headroom'}>
          <HeadroomSub context={8192} concurrency={1} />
        </ResourceScope>
      </div>

      <div hidden={sub !== 'overview'}>
        <ResourceScope active={sub === 'overview'}>
          <UsageCard days={usageDays} cluster={cluster.data} frame={frame} />
        </ResourceScope>
      </div>

      <div hidden={sub !== 'telemetry'}>
        <ResourceScope active={sub === 'telemetry'}>
          <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr)', gap: 'var(--space-16)' }}>
            <Tile value={freeGiB(cluster.data)} decimals={1} width={6} label="GiB addressable free" />
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
            <TelemetrySub nodes={cluster.data?.nodes ?? []} telemetry={telemetry} selection={selection} />
            <section>
              <h2>Load</h2>
              <LoadSub routing={routing.data ?? []} nodes={cluster.data?.nodes ?? []} />
            </section>
          </div>
        </ResourceScope>
      </div>
    </div>
  )
}
