import type { Alert, AlertsReport } from '../api/types'

/** The rail's "what is wrong" rows, as data.
 *
 *  Split from `AlertsSection.tsx` exactly as `activity.ts` is split from
 *  `ActivitySection.tsx`, and for the same reason: the rules below are the
 *  part that can be wrong in a way types cannot catch, and `alerts.check.mjs`
 *  pins every one of them. React-free so the verifier can bundle it with
 *  esbuild's `platform: 'neutral'` -- a hook in here would break a check
 *  rather than a screen.
 */

export interface AlertRow {
  key: string
  kind: Alert['kind']
  severity: Alert['severity']
  /** What the operator calls it: a node's label where there is one. */
  title: string
  detail: string
  evidence: string | null
  /** Rendered beneath the sentence. Never a date when the start is unknown. */
  sinceLabel: string
  count: number
}

/** The heading each kind gets. Short, because the sentence says the rest. */
const KIND_TITLE: Record<Alert['kind'], string> = {
  node_down: 'node down',
  cap_reached: 'cap reached',
  oom: 'out of memory',
}

export function kindTitle(kind: Alert['kind']): string {
  return KIND_TITLE[kind] ?? kind
}

/** How long a condition has stood, or an honest statement that we do not know.
 *
 *  `since === null` is a real answer, not a missing one: a budget that was
 *  already over when the coordinator started has a day but no minute, because
 *  spend is persisted and the moment of the crossing is not. Rendering "just
 *  now" there, or the process start time, would be inventing a fact -- the
 *  same rule the fit gate follows when it declines to state a number it does
 *  not have.
 */
export function sinceLabel(alert: Alert, now: number): string {
  if (alert.since == null) {
    return alert.day ? `today (${alert.day}); the minute is not recorded` : 'start not recorded'
  }
  const s = Math.max(0, now - alert.since)
  if (s < 90) return `for ${Math.round(s)}s`
  if (s < 5400) return `for ${Math.round(s / 60)}m`
  if (s < 172800) return `for ${Math.round(s / 3600)}h`
  return `for ${Math.round(s / 86400)}d`
}

/** The rows to draw. Empty when nothing is wrong, which is the common case and
 *  is why the section renders nothing at all rather than a reassuring line. */
export function alertRows(report: AlertsReport | null, now: number): AlertRow[] {
  if (!report || !report.alerts) return []
  return report.alerts.map((a) => ({
    key: a.key,
    kind: a.kind,
    severity: a.severity,
    title: `${a.subject_label || a.subject} · ${kindTitle(a.kind)}`,
    detail: a.detail,
    // `''` would render an empty Verbatim block; null renders nothing.
    evidence: a.evidence ? a.evidence : null,
    sinceLabel: sinceLabel(a, now),
    count: a.count,
  }))
}

/** "seen 120 times" is worth saying; "seen once" is noise. */
export function countLabel(row: AlertRow): string | null {
  return row.count > 1 ? `seen ${row.count} times` : null
}
