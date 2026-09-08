import { useState } from 'react'
import type { DeploymentDTO, NodeStateDTO, RoutingConfig } from '../../api/types'
import type { SafeMetricsFrame } from '../../state/useMetrics'
import { Lamp } from '../../components/Lamp'
import { Readout } from '../../components/Readout'
import { Verbatim } from '../../components/Verbatim'
import { nodeLive, nodeSignal, utilLabel } from '../../state/live'
import { useMemoryReport } from '../../state/resources'
import { deviceClassLabel, shortGpu } from '../../format'
import { fromState, nodeName } from '../../state/names'
import type { HistoryWindow } from '../../state/history'
import { WindowChips } from './WindowChips'
import { NodeCharts } from './NodeCharts'
import { HardwareRows } from './HardwareRows'
import { Interconnect } from './Interconnect'
import { runners } from '../../tabs/cluster/layout'
import { ServingBlock } from './ServingBlock'
import { RequestsTable } from './RequestsTable'
import { EventsAndLogs } from './EventsAndLogs'
import { ResidentProcesses } from './ResidentProcesses'
import { NodeRuntimeCard } from './NodeRuntimeCard'
import { NodeTerminal } from './Terminal'
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
 *  that ran here, what happened to it, what is holding its GPU, and -- when
 *  none of those said enough -- a prompt on the machine itself.
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
  const memoryReport = useMemoryReport()

  const p = node.profile
  const live = nodeLive(node, frame, stale)
  // The server's verdict, not a threshold of ours: HardwareRows below already
  // renders this exact field, and the lamp disagreeing with the line under it
  // was the whole defect.
  const severity = memoryReport.data?.nodes.find((n) => n.node_id === node.profile.node_id)
    ?.memory_severity
  const signal = nodeSignal(node, live, severity)
  const grey = signal === 'fault' || !live.fresh
  const unified = p.device_class === 'gb10'
  // The one-line hardware summary under the name. Every part is dropped when
  // there is nothing true to put in it: a Raspberry Pi read
  // "· discrete · 0.0 GB/s" -- a memory topology it does not have, and a
  // bandwidth of exactly zero, which `probe.bandwidth_for` returns to mean
  // "we do not know this part" and which reads here as a measured bus.
  // `unified` above stays GB10-only on purpose: the sentence it guards is
  // about the static ceiling overstating what nvidia-smi can account for,
  // which is a GB10 fact rather than a unified-memory one.
  const topology =
    p.device_class === 'gb10' || p.device_class === 'apple'
      ? 'unified memory'
      : p.device_class === 'discrete'
        ? 'discrete'
        : null
  const summary = [
    shortGpu(p.gpu_name) || deviceClassLabel(p.device_class),
    topology,
    p.memory_bandwidth_gbps > 0 ? `${p.memory_bandwidth_gbps.toFixed(1)} GB/s` : null,
  ].filter(Boolean)
  // What this machine is running NOW, not everything it has ever been asked to
  // run. `/api/deployments` is a ledger: it keeps every attempt for a week, so
  // nine failed tries at four models are nine rows all naming this node, and
  // each one drew a full serving block -- lamp, plan line, four readouts and
  // three charts -- for a container that does not exist. `runners()` is the
  // floor's and the dashboard strip's, deliberately not a fourth spelling of
  // the same set: a machine cannot be serving something here and idle there.
  // A finished attempt still says what went wrong, on the Models screen, which
  // is the surface that bands a row by its verdict.
  const here = runners(deployments).filter((d) => d.node_ids.includes(p.node_id))

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
          {summary.join(' · ')}
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

          {/* Renders only when something is actually listening, so this is
              silent on the machines where it would be noise. */}
          <NodeRuntimeCard nodeId={p.node_id} />

          <EventsAndLogs nodeId={p.node_id} window={window} />

          {/* Last in the column, and the escalation from everything above
              it: the recorded lines did not say enough, so go and look. */}
          <NodeTerminal nodeId={p.node_id} />
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
