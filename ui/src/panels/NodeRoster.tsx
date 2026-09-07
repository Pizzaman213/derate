import type { NodeStateDTO } from '../api/types'
import type { SafeMetricsFrame } from '../state/useMetrics'
import { nodeLive, nodeSignal } from '../state/live'
import { Lamp } from '../components/Lamp'
import { Readout } from '../components/Readout'
import { gbytes, shortGpu } from '../format'

// nodeLive/nodeSignal moved to state/live.ts so views/NodeDetail.tsx (which
// needs the exact same "is this node actually okay" call) does not reimplement
// it. Re-exported here so views/NodeDetail.tsx's existing
// `import { nodeLive, nodeSignal } from '../panels/NodeRoster'` still resolves.
export { nodeLive, nodeSignal } from '../state/live'

interface Props {
  nodes: NodeStateDTO[]
  frame: SafeMetricsFrame | null
  streamStale: boolean
  onSelect: (nodeId: string) => void
  selected: string | null
}

export function NodeRoster({ nodes, frame, streamStale, onSelect, selected }: Props) {
  return (
    <div style={{ display: 'grid', gap: 'var(--s1)' }}>
      {nodes.map((n) => {
        const live = nodeLive(n, frame, streamStale)
        const signal = nodeSignal(n, live)
        const down = signal === 'fault'
        // An unreachable node keeps its last known values, greyed. Zeroing them
        // would claim a measurement we do not have.
        const grey = down || !live.fresh
        const id = n.profile.node_id
        return (
          <button
            key={id}
            onClick={() => onSelect(id)}
            aria-current={selected === id}
            style={{
              border: 0,
              borderLeft: `2px solid ${
                selected === id ? 'var(--ink)' : 'transparent'
              }`,
              borderRadius: 0,
              padding: '0 0 0 10px',
              margin: '0 0 0 -12px',
              width: 'calc(100% + 12px)',
              textAlign: 'left',
              display: 'grid',
              gap: 2,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
              <span style={{ fontWeight: 500 }}>{n.profile.hostname}</span>
              {n.role === 'coordinator' ? (
                <span className="unit">coordinator</span>
              ) : null}
            </div>

            <div className="unit">
              {shortGpu(n.profile.gpu_name)} · {gbytes(n.profile.total_memory, 0)} GB
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <Lamp
                signal={signal}
                hollow={grey && !down}
                label={
                  down
                    ? `${id} unreachable`
                    : signal === 'warn'
                      ? `${id} under pressure`
                      : `${id} healthy`
                }
              />
              <Readout
                value={live.power_w}
                width={3}
                unit="W"
                stale={grey}
                align="right"
              />
              <Readout
                value={live.temp_c}
                width={2}
                unit="°C"
                stale={grey}
                align="right"
              />
            </div>

            {down && n.last_error ? (
              <div
                className="label"
                style={{
                  fontWeight: 400,
                  color: 'var(--fault)',
                  whiteSpace: 'pre-wrap',
                }}
              >
                {n.last_error}
              </div>
            ) : null}

            {n.eligible === false && n.ineligible_reason ? (
              <div
                className="label"
                style={{
                  fontWeight: 400,
                  color: 'var(--ink-muted)',
                  whiteSpace: 'pre-wrap',
                }}
              >
                {n.ineligible_reason}
              </div>
            ) : null}
          </button>
        )
      })}
    </div>
  )
}
