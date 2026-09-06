import type { NodeStateDTO } from '../api/types'
import type { SafeMetricsFrame } from '../state/useMetrics'
import { Lamp } from '../components/Lamp'
import { Readout } from '../components/Readout'
import { gbytes, shortGpu } from '../format'

/** Presentation thresholds for the warn lamp. Pressure, not failure: the node
 *  is serving, and it is close to a limit somebody should know about. */
const HOT_C = 80
const MEMORY_PRESSURE_PCT = 92

export interface NodeLive {
  power_w: number | null
  temp_c: number | null
  memory_used_pct: number | null
  util_pct: number | null
}

/** Live values for a node, falling back to the last snapshot when the stream
 *  has nothing. Returns `fresh: false` when the numbers are last-known rather
 *  than current, so the caller greys them instead of passing them off as live. */
export function nodeLive(
  node: NodeStateDTO,
  frame: SafeMetricsFrame | null,
  streamStale: boolean,
): NodeLive & { fresh: boolean } {
  const f = frame?.nodes.find((n) => n.node_id === node.profile.node_id)
  const hasLive =
    !streamStale &&
    f != null &&
    (f.power_w != null || f.temp_c != null || f.util_pct != null)

  if (hasLive) {
    return {
      power_w: f.power_w,
      temp_c: f.temp_c,
      memory_used_pct: f.memory_used_pct,
      util_pct: f.util_pct,
      fresh: true,
    }
  }
  return {
    power_w: node.power_watts,
    temp_c: node.temperature_c,
    memory_used_pct: (node.memory_used / node.profile.addressable_memory) * 100,
    util_pct: node.utilization_pct,
    fresh: false,
  }
}

export function nodeSignal(
  node: NodeStateDTO,
  live: NodeLive,
): 'live' | 'warn' | 'fault' {
  if (node.state === 'unreachable' || !node.healthy) return 'fault'
  if ((live.temp_c ?? 0) >= HOT_C) return 'warn'
  if ((live.memory_used_pct ?? 0) >= MEMORY_PRESSURE_PCT) return 'warn'
  if (node.state === 'degraded') return 'warn'
  return 'live'
}

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
