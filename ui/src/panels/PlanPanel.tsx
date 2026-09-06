import { useState } from 'react'
import type { ParallelismPlan } from '../api/types'
import { Disclosure } from '../components/Panel'
import { Verbatim, VerbatimList } from '../components/Verbatim'
import { Readout } from '../components/Readout'
import { planShortFromDegrees } from '../format'

interface Props {
  plan: ParallelismPlan | null
  /** Node pairs in the cluster that have never been probed. A plan derived
   *  without a measurement is a guess, and the panel says so. */
  unmeasuredPairs: [string, string][]
  measuring: string | null
  onMeasure: (a: string, b: string) => void
}

/** The derived plan, the measurement it came from, and the reasoning behind it.
 *
 *  The expansion is the product thesis made visible: the planner disagreed with
 *  the playbook, and here is the sentence and the rejected alternatives that say
 *  why. Both are rendered exactly as the planner emitted them. */
export function PlanPanel({ plan, unmeasuredPairs, measuring, onMeasure }: Props) {
  const [open, setOpen] = useState(false)

  const missing = unmeasuredPairs[0]

  if (!plan) {
    return (
      <div style={{ display: 'grid', gap: 'var(--s1)' }}>
        <p className="label muted" style={{ fontWeight: 400, margin: 0 }}>
          Nothing is deployed, so there is no plan yet.
        </p>
        {missing ? <MeasurePrompt pair={missing} measuring={measuring} onMeasure={onMeasure} /> : null}
      </div>
    )
  }

  return (
    <div style={{ display: 'grid', gap: 10 }}>
      <div style={{ fontWeight: 500 }}>{planShortFromDegrees(plan)}</div>

      <div
        style={{
          display: 'flex',
          alignItems: 'baseline',
          justifyContent: 'space-between',
          gap: 8,
        }}
      >
        <span className="label muted" style={{ fontWeight: 400 }}>
          link
        </span>
        <Readout value={plan.measured_link_gbps} decimals={1} width={5} unit="GB/s" />
      </div>

      <Disclosure summary="why" open={open} onToggle={() => setOpen((o) => !o)}>
        <div style={{ display: 'grid', gap: 'var(--s1)' }}>
          <Verbatim text={plan.reason} size="label" />
          {plan.rejected.length > 0 ? (
            <div style={{ display: 'grid', gap: 6 }}>
              <div className="label muted" style={{ fontWeight: 400 }}>
                rejected
              </div>
              <VerbatimList items={plan.rejected} />
            </div>
          ) : null}
        </div>
      </Disclosure>

      {missing ? <MeasurePrompt pair={missing} measuring={measuring} onMeasure={onMeasure} /> : null}
    </div>
  )
}

/** A measurement saturates the link and disrupts anything running on it, so it
 *  is offered and never started unasked. */
function MeasurePrompt({
  pair,
  measuring,
  onMeasure,
}: {
  pair: [string, string]
  measuring: string | null
  onMeasure: (a: string, b: string) => void
}) {
  const key = pair.join('~')
  const busy = measuring === key
  return (
    <div
      style={{
        display: 'grid',
        gap: 8,
        paddingTop: 10,
        borderTop: '1px solid var(--rule)',
      }}
    >
      <p className="label" style={{ fontWeight: 400, margin: 0 }}>
        {pair[0]} to {pair[1]} has never been measured. Any plan across that pair
        would be a guess.
      </p>
      <p className="label muted" style={{ fontWeight: 400, margin: 0 }}>
        Measuring saturates the link for about 20 seconds and will slow anything
        serving over it.
      </p>
      <div>
        <button onClick={() => onMeasure(pair[0], pair[1])} disabled={busy}>
          {busy ? 'Measuring…' : 'Measure link'}
        </button>
      </div>
    </div>
  )
}
