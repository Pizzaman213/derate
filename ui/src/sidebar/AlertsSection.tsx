import type { AlertsReport } from '../api/types'
import { useAlerts } from '../state/resources'
import { alertRows, countLabel } from './alerts'
import { Lamp } from '../components/Lamp'
import { Verbatim } from '../components/Verbatim'

/** What is wrong right now.
 *
 *  First in the rail, above the roster. The rail's README says "the order is
 *  an argument, not a layout preference", and the argument here is that
 *  "something is wrong" outranks "here are the machines".
 *
 *  It renders only when there is something wrong — the same rule
 *  `ActivitySection` follows and for the same reason: a permanent "Nothing is
 *  wrong." line beneath four sections that already describe a settled cluster
 *  makes the rail worse, and the section appearing IS the signal.
 *
 *  Every sentence goes through `Verbatim`. These are the fit gate's and
 *  `admission_block`'s own words, and the rule is that they are never
 *  truncated, re-cased or summarised.
 */
export function AlertsSection() {
  const alerts = useAlerts()
  const rows = alertRows((alerts.data as AlertsReport | null) ?? null, Date.now() / 1000)

  if (rows.length === 0) return null

  return (
    <section>
      <h2>Wrong now</h2>
      <div style={{ display: 'grid', gap: 'var(--s-3)' }}>
        {rows.map((row) => (
          <div key={row.key} style={{ display: 'grid', gap: 2 }}>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
              <Lamp signal={row.severity} label={`${row.title}: ${row.severity}`} />
              <span className="label">{row.title}</span>
            </div>
            <Verbatim text={row.detail} size="label" />
            {row.evidence ? <Verbatim text={row.evidence} size="label" /> : null}
            <div className="unit">
              {row.sinceLabel}
              {countLabel(row) ? ` · ${countLabel(row)}` : ''}
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}
