import { useState } from 'react'
import type { DeploymentDTO, NodeStateDTO, RoutingConfig } from '../../api/types'
import type { SafeMetricsFrame } from '../../state/useMetrics'
import { Lamp } from '../../components/Lamp'
import { Readout } from '../../components/Readout'
import { Verbatim } from '../../components/Verbatim'
import { nodeLive, nodeSignal, utilLabel } from '../../state/live'
import { shortGpu } from '../../format'
import { fromState, nodeName } from '../../state/names'
import type { HistoryWindow } from '../../state/history'
import { WindowChips } from './WindowChips'
import { NodeCharts } from './NodeCharts'
import { HardwareRows } from './HardwareRows'
import { Interconnect } from './Interconnect'
import { ServingBlock } from './ServingBlock'
import { RequestsTable } from './RequestsTable'
import { EventsAndLogs } from './EventsAndLogs'
import { ResidentProcesses } from './ResidentProcesses'
import { RenameNode } from './RenameNode'

interface Props {
  node: NodeStateDTO
  deployments: DeploymentDTO[]
  routing: RoutingConfig[]
  frame: SafeMetricsFrame | null
  stale: boolean
  onClose: () => void
}

/** The node page.
 *
 *  Left column is what the machine IS: its live readouts, its four traces, its
 *  hardware, its links to the other machines. Right column is what it is
 *  DOING: every model it serves with that model's own numbers, the requests
 *  that ran here, what happened to it, and what is holding its GPU.
 *
 *  The window chips at the top govern the whole page. `live` is the 60-second
 *  ring this browser accumulates from the 1 Hz frame; the rest come off the
 *  coordinator's durable archive and can say which parts of themselves are
 *  missing. They are not two lengths of one thing and the page does not
 *  pretend otherwise -- see Provenance, which sits under the charts and says
 *  which of the two answered and whether it survives a restart.
 *
 *  Grown from the 440px card this used to be, which showed four instantaneous
 *  readouts and a list of deployment names. Two things it deliberately still
 *  does not do: routing (targets, shares, circuits, cost) belongs to the
 *  deployment inspector and each served name here is a button into it, and the
 *  throttle slider the original mockup had stays gone because there is still
 *  nothing on the wire to throttle. */
export function NodeInspector({ node, deployments, routing, frame, stale, onClose }: Props) {
  const [window, setWindow] = useState<HistoryWindow>('live')

  const p = node.profile
  const live = nodeLive(node, frame, stale)
  const signal = nodeSignal(node, live)
  const grey = signal === 'fault' || !live.fresh
  const unified = p.device_class === 'gb10'
  const here = deployments.filter((d) => d.node_ids.includes(p.node_id))

  return (
    <div>
      <div className="nodehead">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 12 }}>
          <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <Lamp signal={signal} hollow={grey && signal !== 'fault'} label={node.state} />
            <span className="label mono" style={{ fontSize: 17 }}>
              {nodeName(fromState(node))}
            </span>
            {/* The two identities the name may be standing in front of. The id
                is what every deployment, link and routing target is keyed by;
                the hostname is what the machine calls itself, which is NOT
                unique -- a worker in a --network host container reports the
                host's. Both stay here so a renamed plate can be mapped back. */}
            <span className="unit">
              {node.label ? `${p.node_id} · ` : ''}
              {p.hostname} · {node.role}
            </span>
          </span>
          <span style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
            <WindowChips value={window} onChange={setWindow} />
            <button onClick={onClose}>Close</button>
          </span>
        </div>
        <div className="unit" style={{ marginTop: 4 }}>
          {shortGpu(p.gpu_name)} · {unified ? 'unified memory' : 'discrete'} ·{' '}
          {p.memory_bandwidth_gbps.toFixed(1)} GB/s
        </div>
      </div>

      {node.eligible === false && node.ineligible_reason ? (
        <div style={{ margin: '14px 0 0' }}>
          <Verbatim text={node.ineligible_reason} size="label" />
        </div>
      ) : null}

      <div className="nodegrid" style={{ marginTop: 14 }}>
        <div>
          <div className="quad">
            <Stat label="power" value={live.power_w} unit="W" stale={grey} />
            <Stat label="temperature" value={live.temp_c} unit="°C" stale={grey} />
            <Stat label={utilLabel(p)} value={live.util_pct} unit="%" stale={grey} />
            <Stat label="memory used" value={live.memory_used_pct} unit="%" stale={grey} />
          </div>
          <div className="unit" style={{ marginTop: 6 }}>
            These four are always now, whichever window is selected below: what the machine is
            doing at this second is a different question from what it did over the last hour.
          </div>

          <div className="sub">telemetry</div>
          <NodeCharts node={node} window={window} />

          <div className="sub">hardware and memory</div>
          <HardwareRows node={node} />
          <div className="unit" style={{ marginTop: 6 }}>
            {unified
              ? 'Unified memory: the model and the operating system share one pool, so the static ceiling overstates what is actually allocatable. Allocatable is the figure the fit gate plans against, read live from the node rather than worked out here.'
              : 'Allocatable is the figure the fit gate plans against, read live from the node rather than worked out here.'}
          </div>

          <div className="sub">links to other machines</div>
          <Interconnect nodeId={p.node_id} />

          <div className="sub">name</div>
          <RenameNode nodeId={p.node_id} label={node.label ?? null} />
        </div>

        <div>
          <div className="sub" style={{ marginTop: 0, border: 'none', paddingTop: 0 }}>
            serving
          </div>
          {here.length === 0 ? (
            <div className="unit">Nothing.</div>
          ) : (
            here.map((d) => (
              <ServingBlock
                key={d.deployment_id}
                dep={d}
                cfg={routing.find((c) => c.served_name === d.served_name) ?? null}
                frame={frame}
                window={window}
              />
            ))
          )}

          <div className="sub">requests that ran here</div>
          <RequestsTable nodeId={p.node_id} window={window} />

          <ResidentProcesses nodeId={p.node_id} />

          <EventsAndLogs nodeId={p.node_id} window={window} />
        </div>
      </div>
    </div>
  )
}

function Stat({
  label,
  value,
  unit,
  stale,
}: {
  label: string
  value: number | null
  unit: string
  stale: boolean
}) {
  return (
    <div>
      <Readout value={value} decimals={0} width={3} size="readout" stale={stale} />
      <div className="unit">
        {unit} {label}
      </div>
    </div>
  )
}
