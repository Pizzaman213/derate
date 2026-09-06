import { useState } from 'react'
import type { RoutingConfig, RoutingPolicy, RouteTarget } from '../api/types'
import { ProportionBar } from '../components/Bars'
import { Readout } from '../components/Readout'
import { Lamp } from '../components/Lamp'

const POLICIES: { value: RoutingPolicy; label: string }[] = [
  { value: 'least_outstanding', label: 'Least outstanding' },
  { value: 'round_robin', label: 'Round robin' },
  { value: 'weighted_capacity', label: 'Weighted capacity' },
  { value: 'cache_affinity', label: 'Cache affinity' },
  { value: 'failover', label: 'Failover' },
  { value: 'local_first', label: 'Local first' },
  { value: 'cost_aware', label: 'Cost aware' },
]

interface Props {
  configs: RoutingConfig[]
  onPolicy: (servedName: string, policy: RoutingPolicy) => Promise<void>
}

export function RoutingPanel({ configs, onPolicy }: Props) {
  return (
    <div style={{ display: 'grid', gap: 'var(--s2)' }}>
      {configs.map((cfg) => (
        <RoutingEntry key={cfg.served_name} cfg={cfg} onPolicy={onPolicy} />
      ))}
    </div>
  )
}

function RoutingEntry({
  cfg,
  onPolicy,
}: {
  cfg: RoutingConfig
  onPolicy: (servedName: string, policy: RoutingPolicy) => Promise<void>
}) {
  const [pending, setPending] = useState<RoutingPolicy | null>(null)
  const policy = pending ?? cfg.policy

  const change = async (p: RoutingPolicy) => {
    setPending(p)
    try {
      await onPolicy(cfg.served_name, p)
    } finally {
      setPending(null)
    }
  }

  return (
    <div style={{ display: 'grid', gap: 8 }}>
      <div style={{ fontWeight: 500 }}>{cfg.served_name}</div>

      <label style={{ display: 'grid', gap: 4 }}>
        <span className="sr-only">Routing policy for {cfg.served_name}</span>
        <select
          value={policy}
          onChange={(e) => void change(e.target.value as RoutingPolicy)}
          style={{ width: '100%' }}
        >
          {POLICIES.map((p) => (
            <option key={p.value} value={p.value}>
              {p.label}
            </option>
          ))}
        </select>
      </label>

      {/* Someone looking at a 70/30 split needs to know it was deliberate. */}
      {cfg.auto_selected && cfg.auto_reason ? (
        <p
          className="label"
          style={{ fontWeight: 400, margin: 0, color: 'var(--ink-muted)' }}
        >
          {cfg.auto_reason}
        </p>
      ) : null}

      {/* Under local first, the state anyone actually wants at a glance. */}
      {cfg.policy === 'local_first' && cfg.flow ? (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Lamp
            signal={cfg.flow === 'local' ? 'live' : 'warn'}
            label={cfg.flow === 'local' ? 'serving locally' : 'spilled to remote'}
          />
          <span className="label" style={{ fontWeight: 400 }}>
            {cfg.flow === 'local'
              ? 'Serving locally'
              : 'Spilled to remote: every local target is saturated'}
          </span>
        </div>
      ) : null}

      <div style={{ display: 'grid', gap: 10 }}>
        {cfg.targets.map((t) => (
          <TargetRow key={t.target_id} target={t} policy={policy} />
        ))}
      </div>
    </div>
  )
}

function TargetRow({ target: t, policy }: { target: RouteTarget; policy: RoutingPolicy }) {
  const remote = t.kind === 'remote'
  // A target held at zero weight is shown, greyed, with the reason. Hiding it
  // would make the cluster look smaller than it is.
  const benched = !remote && t.weight === 0 && t.zero_weight_reason != null
  const blocked = !t.admitting || !t.healthy
  const dim = benched || blocked

  return (
    <div style={{ display: 'grid', gap: 4, opacity: dim ? 0.62 : 1 }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'baseline',
          justifyContent: 'space-between',
          gap: 8,
        }}
      >
        <span style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
          <span className="label" style={{ fontWeight: 400 }}>
            {t.display_name ?? t.target_id}
          </span>
          {remote ? <span className="unit">remote</span> : null}
        </span>
        <span style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
          {policy === 'weighted_capacity' || policy === 'round_robin' ? (
            <Readout
              value={t.weight}
              decimals={2}
              width={4}
              tone={dim ? 'muted' : 'ink'}
              title="share of traffic"
            />
          ) : null}
          <Readout
            value={t.outstanding}
            width={2}
            unit="in flight"
            tone={dim ? 'muted' : 'ink'}
          />
        </span>
      </div>

      {policy === 'weighted_capacity' || policy === 'round_robin' ? (
        <ProportionBar
          value={t.weight}
          tone={dim ? 'muted' : 'ink'}
          label={`${t.display_name ?? t.target_id} takes ${Math.round(t.weight * 100)} percent of traffic`}
        />
      ) : null}

      {remote && t.cost_per_mtok != null ? (
        <div className="unit">${t.cost_per_mtok.toFixed(2)} per Mtok output</div>
      ) : null}

      {blocked ? (
        <div className="label" style={{ fontWeight: 400, color: 'var(--fault)' }}>
          {!t.healthy
            ? 'Unhealthy. Excluded from every policy until it recovers.'
            : 'Not admitting. Excluded from every policy, round robin included.'}
        </div>
      ) : null}

      {benched && !blocked ? (
        <div
          className="label"
          style={{ fontWeight: 400, color: 'var(--ink-muted)', whiteSpace: 'pre-wrap' }}
        >
          {t.zero_weight_reason}
        </div>
      ) : null}
    </div>
  )
}
