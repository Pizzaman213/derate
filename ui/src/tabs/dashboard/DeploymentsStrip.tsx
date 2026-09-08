import { useMemo, useState } from 'react'
import { isAudio } from '../../api/types'
import type { DeploymentDTO, RoutingConfig, TopologyDeployment } from '../../api/types'
import { useBackend } from '../../state/backend'
import type { SelectionApi } from '../../state/selection'
import type { SafeMetricsFrame } from '../../state/useMetrics'
import type { TelemetrySeries } from '../../state/useTelemetry'
import { Readout } from '../../components/Readout'
import { planShortFromDegrees } from '../../format'
import { runners } from '../cluster/layout'
import { ChartCell } from './Chart'

interface Props {
  deployments: DeploymentDTO[]
  topologyDeployments: TopologyDeployment[]
  frame: SafeMetricsFrame | null
  routing: RoutingConfig[]
  telemetry: TelemetrySeries
  selection: SelectionApi
}

/** One row per *running* deployment: name, plan, a 60s spark of its
 *  throughput, the live number, and how many routing targets answer to it.
 *  Single click selects (shared with the flow graph and the telemetry
 *  sub-tab); double-click, or the small button that only appears on
 *  hover/selection, opens the detail sheet. Ported from
 *  mockups-next/js/dashboard.js `depsStrip()`.
 *
 *  `/api/deployments` is a ledger, not a roster: it keeps every attempt, so
 *  three failed tries at one model are three rows carrying the same served
 *  name, the same plan and a 0 tok/s readout the frame never sent. The strip
 *  has no vocabulary for "over" -- there is no band, no state column, and the
 *  stop button simply vanishes -- so a stopped deployment drew as an ordinary
 *  serving row. `runners()` is `cluster/layout.ts`'s, which is
 *  `models/rows.ts`'s `TERMINAL`: the floor and this strip must not disagree
 *  about whether a model is running. Stopping is kept -- it is winding down
 *  but still holds its machines. */
export function DeploymentsStrip({
  deployments,
  topologyDeployments,
  frame,
  routing,
  telemetry,
  selection,
}: Props) {
  const { backend, invalidate } = useBackend()
  // Keyed by deployment id so one row's stop never greys another's controls.
  const [stopping, setStopping] = useState<string | null>(null)
  // What is actually up. A row stopped from this strip disappears from it on
  // the next poll rather than lingering as a serving row with no stop button.
  const live = useMemo(() => runners(deployments), [deployments])

  // The strip is where you are when you notice something should stop.
  // Routing through the sheet for it would be friction this interface does
  // not impose anywhere else -- NodesCard removes a node from its table too.
  const stop = async (d: DeploymentDTO) => {
    if (
      !window.confirm(
        `Stop ${d.served_name}? It stops accepting new requests immediately; ` +
          `requests already in flight finish.`,
      )
    )
      return
    setStopping(d.deployment_id)
    try {
      await backend.stopDeployment(d.deployment_id)
      invalidate()
    } catch {
      // The sheet is where the reason belongs; a strip row has no room for a
      // sentence and must not swallow it into a silent no-op either.
      invalidate()
    } finally {
      setStopping(null)
    }
  }
  if (live.length === 0) {
    return (
      <p className="label muted" style={{ fontWeight: 400, margin: 0 }}>
        Nothing is being served.
      </p>
    )
  }

  return (
    <div>
      {live.map((d) => {
        const sel = selection.selDep === d.served_name
        const liveTps =
          frame?.deployments.find((x) => x.deployment_id === d.deployment_id)?.tokens_per_sec ??
          topologyDeployments.find((x) => x.deployment_id === d.deployment_id)?.tokens_per_sec ??
          null
        const targets = routing.find((c) => c.served_name === d.served_name)?.targets.length ?? null
        const series = telemetry.depTps[d.served_name] ?? []
        // A speech or transcription server decodes no tokens, so tok/s is not
        // a number that is missing -- it is a number that does not exist. The
        // strip says so rather than rendering a confident 0.
        const audio = isAudio(d.modality)

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
            <span className="unit">
              {planShortFromDegrees(d.plan)}
              {audio ? ` · ${d.modality}` : ''}
            </span>
            <ChartCell points={series} label={`${d.served_name} throughput over the last 60 seconds`} />
            {audio ? (
              <span
                className="unit"
                style={{ textAlign: 'right' }}
                title={`A ${d.modality} deployment produces audio, not tokens; there is no tok/s to report.`}
              >
                —
              </span>
            ) : (
              <Readout value={liveTps} decimals={0} width={5} unit="tok/s" align="right" />
            )}
            <span className="unit" style={{ textAlign: 'right' }}>
              {targets == null ? '—' : targets} target{targets === 1 ? '' : 's'}
              {d.state !== 'stopping' && d.state !== 'stopped' ? (
                <button
                  className="ghost dets"
                  style={{ padding: '1px 5px', marginLeft: 4 }}
                  disabled={stopping === d.deployment_id}
                  onClick={(e) => {
                    e.stopPropagation()
                    void stop(d)
                  }}
                  aria-label={`stop ${d.served_name}`}
                >
                  {stopping === d.deployment_id ? '…' : 'stop'}
                </button>
              ) : null}
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
