import { useState } from 'react'
import { useSelection } from '../state/selection'
import { useCluster } from '../state/resources'
import { Disclosure } from '../components/Panel'
import { Verbatim, VerbatimList } from '../components/Verbatim'
import { fmt, planShortFromDegrees } from '../format'

// Ported from mockups-next/js/sidebar.js `sidebar()`'s plan block. The
// mockup's `#whyBox` is hand-written markup keyed on a fixture's `span`/`solo`
// shape; the real deployment carries its own `plan.reason` and
// `plan.rejected`, one sentence each from the planner, so that markup does
// not survive the port -- this renders those two fields verbatim instead.
export function PlanSection() {
  const { selDep } = useSelection()
  const cluster = useCluster()
  const [open, setOpen] = useState(false)

  const dep = selDep
    ? (cluster.data?.deployments.find((d) => d.served_name === selDep) ?? null)
    : null

  if (!dep) {
    return (
      <section>
        <h2>Plan</h2>
        <p className="unit">Nothing is being served.</p>
      </section>
    )
  }

  const plan = dep.plan
  // A plan "spans" when it placed the model across more than one node --
  // exactly the case a measured link figure describes. A single-node plan
  // has no inter-node link to report, so the field is dashed rather than
  // showing a stale or unrelated number.
  const spanning = plan.node_ids.length > 1

  return (
    <section>
      <h2>Plan · {dep.served_name}</h2>
      <div className="row">
        <span className="label">{planShortFromDegrees(plan)}</span>
        <span className="mono">{spanning ? `${fmt(plan.measured_link_gbps, 1)} GB/s` : '—'}</span>
      </div>

      <Disclosure summary="why" open={open} onToggle={() => setOpen((o) => !o)}>
        <Verbatim text={plan.reason} size="label" />
        {plan.rejected.length > 0 ? (
          <>
            <div className="unit" style={{ marginTop: 8 }}>Rejected</div>
            <VerbatimList items={plan.rejected} />
          </>
        ) : null}
      </Disclosure>
    </section>
  )
}
