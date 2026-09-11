/** The link-tuning rail's decisions, with no DOM in them.
 *
 *  Out here for the reason `sidebar/activity.ts` and `models/ladder.ts` are:
 *  `check.mjs` bundles with esbuild's `platform: 'neutral'` and cannot import a
 *  module that reaches React, so a decision left inside `SelectionRail.tsx` is
 *  a decision nothing can check. Nothing in this directory's rail was reachable
 *  by a verifier before this file; that is why the rules below had no test.
 *
 *  Must not import `state/selection` — that pulls in React. `format.ts` and
 *  `layout.ts` are safe.
 */

import type { LinkTuning, TuningRow } from '../../api/types'

/** The four states a pair can be in, and they are genuinely four.
 *
 *  The distinction that matters is the middle two: the server answers `env:
 *  {}` both for "nobody has measured this" and for "measured, and the default
 *  won". Rendering the second as the first offers to redo finished work and
 *  reads a completed calibration as a gap — which is the same class of error
 *  as an absent number rendered as zero.
 */
export type TuningState = 'uncalibrated' | 'default-best' | 'tuned' | 'failed'

export function tuningState(tuning: LinkTuning | null | undefined): TuningState {
  if (!tuning || !tuning.calibrated) return 'uncalibrated'
  // Every row an error and none of them a timing: the fabric refused. That is
  // a finding about the link, not a missing measurement, and it must not read
  // as "never tried".
  const rows = tuning.rows ?? []
  if (rows.length > 0 && rows.every((r) => r.error != null)) return 'failed'
  return Object.keys(tuning.env ?? {}).length > 0 ? 'tuned' : 'default-best'
}

/** The one-line value for the rail's Tuning row. */
export function tuningLabel(tuning: LinkTuning | null | undefined): string {
  switch (tuningState(tuning)) {
    case 'uncalibrated':
      return 'not tuned'
    case 'failed':
      return 'calibration did not complete'
    case 'default-best':
      // A result, and it has to read as one. "none" or "—" here would be
      // indistinguishable from the untuned case two lines up.
      return "NCCL's own defaults are best here"
    case 'tuned':
      return Object.entries(tuning!.env)
        .map(([k, v]) => `${k}=${v}`)
        .sort()
        .join(' ')
  }
}

/** Whether offering to calibrate this pair makes sense.
 *
 *  A pair already being worked on is not offered again: `calibrate` and
 *  `measure` share one lock server-side, so a second press would not refuse —
 *  it would block for minutes behind the first, which is indistinguishable
 *  from a hang.
 */
export function canTune(
  tuning: LinkTuning | null | undefined,
  measuring: boolean,
): boolean {
  return !measuring && tuningState(tuning) !== 'tuned'
}

/** The evidence line under the verdict: what each candidate actually cost.
 *
 *  Two numbers per candidate because the choice is a trade between two
 *  regimes, and one of them alone always makes the wrong candidate look right.
 *  Sorted by the setting's own text so the default (`""`) leads.
 */
export function tuningEvidence(
  tuning: LinkTuning | null | undefined,
  decodeBand: number,
  bulkBand: number,
): { label: string; decodeUs: number | null; bulkGbps: number | null }[] {
  const rows = tuning?.rows ?? []
  const byEnv = new Map<string, TuningRow[]>()
  for (const r of rows) {
    if (r.error != null) continue
    const key = envLabel(r.env)
    byEnv.set(key, [...(byEnv.get(key) ?? []), r])
  }
  return [...byEnv.entries()]
    .map(([label, group]) => ({
      label,
      decodeUs: group.find((r) => r.size_band === decodeBand)?.microseconds ?? null,
      bulkGbps: group.find((r) => r.size_band === bulkBand)?.busbw_gbps ?? null,
    }))
    .sort((a, b) => a.label.localeCompare(b.label))
}

export function envLabel(env: Record<string, string>): string {
  const parts = Object.entries(env)
    .map(([k, v]) => `${k}=${v}`)
    .sort()
  return parts.length ? parts.join(' ') : 'default'
}
