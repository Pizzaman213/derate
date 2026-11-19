import { useCluster, useTopology } from '../state/resources'
import { useMetrics } from '../state/metrics'
import { useSelection } from '../state/selection'
import { nodeLive, nodeSignal, utilKind } from '../state/live'
import { fromState, nodeName } from '../state/names'
import { Lamp } from '../components/Lamp'
import { Readout } from '../components/Readout'
import { ProportionBar } from '../components/Bars'
import { pct, relativeTime } from '../format'

// Ported from mockups-next/js/sidebar.js `roster()`. The slots model (a fixed
// array of GPU slots per node, each either free or holding a model) does not
// exist on the real wire -- a node just has zero or more deployments touching
// it, by way of topology's per-node `deployments` (deployment ids). So there
// is no "N free" count and no per-slot bar here, only the memory line the
// live frame actually gives us.

export function RosterSection() {
  const cluster = useCluster()
  const topology = useTopology()
  const { frame, stale } = useMetrics()
  const { openSheet } = useSelection()

  const nodes = cluster.data?.nodes ?? []

  const servedNameById = new Map(
    (topology.data?.deployments ?? []).map((d) => [d.deployment_id, d.served_name]),
  )
  const deploymentIdsByNode = new Map(
    (topology.data?.nodes ?? []).map((n) => [n.node_id, n.deployments]),
  )

  return (
    <section>
      <h2>Nodes</h2>
      <div style={{ display: 'grid', gap: 'var(--space-12)' }}>
        {nodes.map((n) => {
          const id = n.profile.node_id
          const live = nodeLive(n, frame, stale)
          const signal = nodeSignal(n, live)
          const down = signal === 'fault'
          const grey = down || !live.fresh
          const ineligible = n.eligible === false

          const names = (deploymentIdsByNode.get(id) ?? [])
            .map((depId) => servedNameById.get(depId))
            .filter((x): x is string => Boolean(x))

          return (
            <button
              type="button"
              key={id}
              onDoubleClick={() => openSheet({ kind: 'node', id })}
              onClick={(e) => { if (e.detail === 0) openSheet({ kind: 'node', id }) }}
              aria-label={`${nodeName(fromState(n))}: Enter or double-click for detail`}
              title={ineligible ? (n.ineligible_reason ?? undefined) : undefined}
              style={{
                font: 'inherit',
                textAlign: 'left',
                width: '100%',
                background: 'none',
                display: 'grid',
                gap: 'var(--space-4)',
                cursor: 'pointer',
                opacity: ineligible ? 'var(--opacity-55)' : 1,
                border: ineligible ? '1px dashed var(--rule)' : '1px solid transparent',
                borderRadius: 'var(--radius)',
                padding: 'var(--space-4) var(--space-6)',
                margin: '0 calc(-1 * var(--space-6))',
              }}
            >
              <div className="row" style={{ padding: '0 0 var(--space-3)' }}>
                <span
                  className="label"
                  style={{
                    fontWeight: ineligible ? 400 : 500,
                    color: ineligible ? 'var(--ink-muted)' : undefined,
                  }}
                >
                  {nodeName(fromState(n))}
                </span>
                <span className="mono unit" style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-6)' }}>
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
                  <Readout value={down ? null : live.power_w} width={3} unit="W" stale={grey} />
                </span>
              </div>

              {ineligible && n.ineligible_reason ? (
                <div className="unit" style={{ whiteSpace: 'pre-wrap' }}>{n.ineligible_reason}</div>
              ) : null}

              <ProportionBar
                value={down || live.memory_used_pct == null ? null : live.memory_used_pct / 100}
                tone={grey ? 'muted' : 'ink'}
                label={live.memory_used_pct == null ? `no memory reading for ${id}` : `${pct(live.memory_used_pct)} percent of addressable memory in use on ${id}`}
              />

              <div className="unit">{names.length > 0 ? names.join(' · ') : 'no deployments'}</div>

              <div className="unit" style={{ marginTop: 'var(--space-2)' }}>
                {down
                  ? `unreachable · last seen ${relativeTime(n.last_seen)}`
                  : `${utilKind(n.profile)} ${pct(live.util_pct)}%${n.profile.device_class === 'gb10' ? ' · shared' : ''}`}
              </div>
            </button>
          )
        })}
        {nodes.length === 0 ? <p className="unit">No nodes yet.</p> : null}
      </div>
    </section>
  )
}
