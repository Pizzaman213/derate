import type { DeploymentDTO, RoutingConfig, TopologyDeployment } from '../../api/types'
import type { SelectionApi } from '../../state/selection'
import type { SafeMetricsFrame } from '../../state/useMetrics'
import type { TelemetrySeries } from '../../state/useTelemetry'
import { Readout } from '../../components/Readout'
import { planShortFromDegrees } from '../../format'
import { ChartCell } from './Chart'

interface Props {
  deployments: DeploymentDTO[]
  topologyDeployments: TopologyDeployment[]
  frame: SafeMetricsFrame | null
  routing: RoutingConfig[]
  telemetry: TelemetrySeries
  selection: SelectionApi
}

/** One row per deployment: name, plan, a 60s spark of its throughput, the
 *  live number, and how many routing targets answer to it. Single click
 *  selects (shared with the flow graph and the telemetry sub-tab);
 *  double-click, or the small button that only appears on hover/selection,
 *  opens the detail sheet. Ported from mockups-next/js/dashboard.js
 *  `depsStrip()`. */
export function DeploymentsStrip({
  deployments,
  topologyDeployments,
  frame,
  routing,
  telemetry,
  selection,
}: Props) {
  if (deployments.length === 0) {
    return (
      <p className="label muted" style={{ fontWeight: 400, margin: 0 }}>
        Nothing is being served.
      </p>
    )
  }

  return (
    <div>
      {deployments.map((d) => {
        const sel = selection.selDep === d.served_name
        const liveTps =
          frame?.deployments.find((x) => x.deployment_id === d.deployment_id)?.tokens_per_sec ??
          topologyDeployments.find((x) => x.deployment_id === d.deployment_id)?.tokens_per_sec ??
          null
        const targets = routing.find((c) => c.served_name === d.served_name)?.targets.length ?? null
        const series = telemetry.depTps[d.served_name] ?? []

        const openDetail = () => selection.openSheet({ kind: 'dep', id: d.served_name })

        return (
          <div
            key={d.deployment_id}
            className={`deprow${sel ? ' sel' : ''}`}
            onClick={() => selection.selectDep(d.served_name)}
            onDoubleClick={openDetail}
            title="Double-click for detail"
          >
            <span className="mono" style={{ fontSize: 13 }}>
              {d.served_name}
            </span>
            <span className="unit">{planShortFromDegrees(d.plan)}</span>
            <ChartCell points={series} label={`${d.served_name} throughput over the last 60 seconds`} />
            <Readout value={liveTps} decimals={0} width={5} unit="tok/s" align="right" />
            <span className="unit" style={{ textAlign: 'right' }}>
              {targets == null ? '—' : targets} target{targets === 1 ? '' : 's'}
              <button
                className="ghost dets"
                style={{ padding: '1px 5px', marginLeft: 4 }}
                onClick={(e) => {
                  e.stopPropagation()
                  openDetail()
                }}
                aria-label={`${d.served_name} detail`}
              >
                ↗
              </button>
            </span>
          </div>
        )
      })}
    </div>
  )
}
