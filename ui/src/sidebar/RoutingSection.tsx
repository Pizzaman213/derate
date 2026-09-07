import { useState } from 'react'
import type { RoutingConfig, RoutingPolicy, RouteTarget } from '../api/types'
import { useSelection } from '../state/selection'
import { PROPORTIONAL } from '../state/policy'
import { useRouting } from '../state/resources'
import { useBackend } from '../state/backend'
import { Lamp } from '../components/Lamp'
import { Verbatim, VerbatimList } from '../components/Verbatim'

// Ported from mockups-next/js/sidebar.js `sidebar()`'s weights block plus
// js/routing.js's NOTES, extended from five policies to the wire's seven
// (cache_affinity and failover have no NOTES entry in the mockup; their help
// text below is transcribed from gateway/policies.py's own docstrings rather
// than invented). `curW()` -- the mockup's per-policy client-side weight
// arithmetic -- does not survive the port: RouteTarget.weight is the one real
// number the wire gives us, so both bar grammars below draw from it, varying
// only in how the value is presented, never in what value is shown.
const POLICIES: { value: RoutingPolicy; help: string }[] = [
  { value: 'least_outstanding', help: 'Fewest in-flight wins. Accounts for a replica mid-prefill.' },
  { value: 'round_robin', help: 'Even rotation. Ignores that a replica may be mid-prefill.' },
  { value: 'weighted_capacity', help: 'Share proportional to measured throughput.' },
  {
    value: 'cache_affinity',
    help:
      "Repeat prompt prefixes stick to the same local target. Remote targets are excluded — a remote runtime's cache cannot be reasoned about.",
  },
  { value: 'failover', help: 'Everything to the primary target; switches only when it stops admitting.' },
  { value: 'local_first', help: 'Remote targets take traffic only once every local slot stops admitting.' },
  { value: 'cost_aware', help: 'Cheapest admitting target. Local priced from measured draw.' },
]


function targetLabel(t: RouteTarget): string {
  if (t.kind === 'local' && t.node_ids && t.node_ids.length > 0) return t.node_ids.join(' + ')
  return t.target_id
}

export function RoutingSection() {
  const { selDep } = useSelection()
  const routing = useRouting()
  const { backend, invalidate } = useBackend()
  const [pending, setPending] = useState<RoutingPolicy | null>(null)
  const [error, setError] = useState<string | null>(null)

  const cfg: RoutingConfig | null = selDep
    ? (routing.data?.find((c) => c.served_name === selDep) ?? null)
    : null

  if (!cfg) {
    return (
      <section>
        <h2>Routing</h2>
        <p className="unit">Nothing is being served.</p>
      </section>
    )
  }

  const policy = pending ?? cfg.policy
  const help = POLICIES.find((p) => p.value === policy)?.help ?? ''
  const proportional = PROPORTIONAL.has(policy)

  const change = async (p: RoutingPolicy) => {
    setPending(p)
    setError(null)
    try {
      await backend.setPolicy(cfg.served_name, p)
      invalidate()
    } catch (e) {
      // The server's own rejection, verbatim -- same convention as every
      // other mutation in this package. Without this, a 409/500 here looked
      // identical to nothing happening, in the one section whose thesis is
      // that the server's own sentences are the product.
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setPending(null)
    }
  }

  return (
    <section>
      <h2>Routing</h2>
      <label style={{ display: 'block' }}>
        <span className="sr-only">Routing policy for {cfg.served_name}</span>
        <select
          value={policy}
          onChange={(e) => void change(e.target.value as RoutingPolicy)}
          style={{ width: '100%' }}
        >
          {POLICIES.map((p) => (
            <option key={p.value} value={p.value}>
              {p.value}
            </option>
          ))}
        </select>
      </label>

      {error ? (
        <div className="label" style={{ color: 'var(--fault)', marginTop: 6, whiteSpace: 'pre-wrap' }}>
          {error}
        </div>
      ) : null}

      {cfg.auto_selected && cfg.auto_reason ? (
        <div style={{ marginTop: 6 }}>
          <Verbatim text={cfg.auto_reason} size="label" />
        </div>
      ) : null}

      {cfg.policy === 'local_first' && cfg.flow ? (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 6 }}>
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

      <div style={{ marginTop: 10, display: 'grid', gap: 8 }}>
        {cfg.targets.map((t) => (
          <TargetWeight key={t.target_id} target={t} proportional={proportional} />
        ))}
      </div>

      <div className="unit" style={{ marginTop: 4 }}>
        {cfg.targets.length > 1 ? (
          <>
            {help}
            <br />
            <span className="muted">
              {proportional
                ? 'Bars are the configured share.'
                : 'Bars show where traffic is going right now, not a configured share — this policy does not set one.'}
            </span>
          </>
        ) : (
          'Single target. Policy has no effect.'
        )}
      </div>
    </section>
  )
}

function TargetWeight({ target: t, proportional }: { target: RouteTarget; proportional: boolean }) {
  const pctv = Math.round(t.weight * 100)
  const label = targetLabel(t)
  const circuitFlagged = t.circuit === 'open' || t.circuit === 'half_open'
  // Only a local target is ever floored to zero, and only ever with a reason
  // attached (gateway/targets.py `apply_scores`). A remote at zero is just
  // not currently favoured -- there is no wire reason to show for that.
  const zeroReason = t.kind === 'local' && t.weight === 0 ? t.zero_weight_reason : null
  // A breaker state is folded into the same deprioritized/opacity family as
  // "not admitting", not a new color: --fault/--warn already mean node
  // health and pressure elsewhere (Lamp signals, stale-node tint, unhealthy
  // providers), and a circuit chip painted in either would hand them a
  // second meaning. The old RoutingPanel drew this as an uncoloured, hollow
  // Lamp folded into the row's dim state -- restored here rather than a
  // colored pill.
  const excluded = !t.healthy || !t.admitting || circuitFlagged

  return (
    <div style={{ display: 'grid', gap: 4, opacity: excluded ? 0.62 : 1 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <div
          style={{
            flex: 1,
            height: 15,
            borderRadius: 2,
            overflow: 'hidden',
            background: proportional ? 'var(--panel-sunk)' : 'none',
            boxShadow: `inset 0 0 0 1px ${proportional ? 'var(--edge)' : 'var(--rule)'}`,
          }}
        >
          <div
            style={{
              height: '100%',
              width: `${pctv}%`,
              background: 'var(--fill)',
              opacity: proportional ? (t.weight > 0 ? 1 : 0.25) : t.weight > 0 ? 0.55 : 0,
            }}
          />
        </div>
        <span className="mono unit" style={{ width: 32, textAlign: 'right' }}>
          {pctv}%
        </span>
      </div>

      <div className="unit" style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
        {circuitFlagged ? (
          <Lamp
            signal="idle"
            hollow
            label={
              t.circuit === 'open'
                ? `${label} circuit open, benched after repeated failures`
                : `${label} circuit half-open, probing`
            }
          />
        ) : null}
        <span>
          {label}
          {t.kind === 'remote' ? ' · remote' : ''}
        </span>
        {zeroReason ? <span>· {zeroReason}</span> : null}
      </div>

      {t.admission_blocks && t.admission_blocks.length > 0 ? (
        <VerbatimList items={t.admission_blocks} />
      ) : null}
    </div>
  )
}
