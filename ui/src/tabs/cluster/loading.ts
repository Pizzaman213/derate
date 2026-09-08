import type { DeploymentState, DownloadActivity, LaunchActivity } from '../../api/types'
import { gbytes } from '../../format'

/** A band that is not serving yet, as data.
 *
 *  Split from the drawing the way `sidebar/activity.ts` and `models/rows.ts`
 *  are, and for the same reason: which step a launch is on is a claim about
 *  the cluster, it is the part of this that can be wrong in a way types cannot
 *  catch, and `loading.check.mjs` pins every rule below.
 *
 *  **This file will not invent a step the wire cannot see.** The setup screen
 *  tells download from GPU-load by polling `/api/storage` every two seconds
 *  and watching the model cache grow (`SetupTab.tsx`); the floor cannot --
 *  that endpoint fans out to every node and walks a directory on each, which
 *  is why it is polled at thirty seconds, and a phase split made from a
 *  thirty-second sample would be a guess wearing a tick mark. So `launching`
 *  holds at "Downloading weights", which is where a launch spends its time and
 *  is the one of the two that stays true either way, and "Loading onto the
 *  GPU" is only ever ticked in retrospect, when the deployment reports ready.
 *  A real pull -- a provider download, which reports genuine bytes against a
 *  genuine total -- advances it, because then there is something to advance
 *  on. Same rule as `activity.ts`'s launch bar, which is null and always will
 *  be: no percentage exists here, so none is drawn.
 */

export type LaunchPhase = 'preparing' | 'downloading' | 'loading' | 'serving'

export const PHASE_ORDER: readonly LaunchPhase[] = [
  'preparing',
  'downloading',
  'loading',
  'serving',
]

export const PHASE_LABEL: Record<LaunchPhase, string> = {
  preparing: 'Preparing',
  downloading: 'Downloading weights',
  loading: 'Loading onto the GPU',
  serving: 'Serving',
}

/** The states `/api/activity` calls arriving, and the only ones that get the
 *  loading form. DEGRADED is deliberately not one: it is up and serving badly,
 *  which the band's own `degraded` word already says. STOPPING is the mirror
 *  -- it is leaving, not arriving. Kept in step with `_ARRIVING_STATES` in
 *  `gateway/internal_api.py`, which is what decides whether a launch row
 *  exists to read at all. */
export function isArriving(state: DeploymentState): boolean {
  return state === 'planned' || state === 'launching'
}

export interface LaunchStep {
  phase: LaunchPhase
  label: string
  /** Reached, current, or still ahead. Exactly one step is `now` until the
   *  deployment is serving, at which point every step is `done` -- a stepper
   *  with two current steps says nothing, and one with none has stalled. */
  state: 'done' | 'now' | 'todo'
  /** The right-hand column: a real size, or ''. Never a placeholder figure --
   *  a `0.0 GB` beside a step nothing has measured reads as a measurement. */
  detail: string
}

export interface LaunchView {
  steps: LaunchStep[]
  /** Seconds since this coordinator first saw the launch, or null when there
   *  is no launch row to read one off. Null is not zero: a launch whose start
   *  nobody recorded has run for an unknown time, and `0:00` claims it started
   *  this second. `LaunchActivity.since` is honest about being the moment the
   *  coordinator noticed rather than the moment the launch began, and after a
   *  restart it is when the coordinator came back -- the caption says so. */
  elapsed: number | null
  /** Whatever the manager last recorded, verbatim. */
  error: string | null
}

/** Which step a launch is on, from the deployment's state and -- only when one
 *  exists -- a pull actually reporting bytes for it. */
export function phaseOf(state: DeploymentState, pull: DownloadActivity | null): LaunchPhase {
  if (state === 'planned') return 'preparing'
  if (state === 'launching') return pull != null && pull.done ? 'loading' : 'downloading'
  return 'serving'
}

/** The pull backing this deployment, if any is running.
 *
 *  Matched against `model_id` and never `served_name`: a served name is an
 *  alias chosen here, and the puller only ever saw the model. The two spell
 *  the model the same way when the deployment came from a provider that
 *  reports its pulls, and disagree when it did not -- a miss is the ordinary
 *  case, not a fault, and it degrades to the honest default above. A finished
 *  pull still counts, because "the download finished" is the one fact that
 *  moves the stepper on. */
export function pullFor(
  downloads: readonly DownloadActivity[],
  modelId: string | null,
): DownloadActivity | null {
  if (!modelId) return null
  return downloads.find((d) => d.model === modelId && d.error == null) ?? null
}

function pullDetail(pull: DownloadActivity | null): string {
  if (pull == null || pull.total == null || pull.total <= 0) return ''
  return `${gbytes(pull.completed)} / ${gbytes(pull.total)} GB`
}

/** The stepper, and the clock under it.
 *
 *  `now` is passed in rather than read, so this stays pure and the verifier
 *  can hold time still. */
export function launchView(
  dep: { state: DeploymentState; model_id: string },
  launch: LaunchActivity | null,
  downloads: readonly DownloadActivity[],
  now: number,
): LaunchView {
  const pull = pullFor(downloads, dep.model_id)
  const phase = phaseOf(dep.state, pull)
  const at = PHASE_ORDER.indexOf(phase)
  const serving = phase === 'serving'

  const steps = PHASE_ORDER.map((p, i) => ({
    phase: p,
    label: PHASE_LABEL[p],
    state: serving || i < at ? ('done' as const) : i === at ? ('now' as const) : ('todo' as const),
    detail: p === 'downloading' ? pullDetail(pull) : '',
  }))

  return {
    steps,
    // Clamped at zero rather than allowed negative: a coordinator whose clock
    // is a second ahead of this browser's must not draw a launch that has not
    // started yet.
    elapsed: launch == null ? null : Math.max(0, Math.round(now - launch.since)),
    error: launch?.last_error ?? null,
  }
}

/** `m:ss`. Minutes are not padded and seconds always are, which is how a clock
 *  is read; `tabular-nums` in the stylesheet keeps the column from jittering
 *  as the digits change. */
export function clock(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}
