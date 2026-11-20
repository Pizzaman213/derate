// What the last screen of the wizard reads off the cluster, and how a launch
// failure is split for display. Pure: no React, no fetch. Gated by
// `done.check.mjs`.
//
// The done screen used to trust a flag the model step set -- `launched: true`
// the moment somebody pressed Continue -- and so called a launch that had
// already failed "starting", with a green dot. It reads the deployment records
// and the served-model list now, which are the two things that cannot be wrong
// about whether anything answers.

import type { DeploymentDTO, ServedModel } from '../../api/types'

export type DoneReading =
  /** The pick's deployment is ready/degraded, or something else is served.
   *  `local` says whether a deployment on these machines answers to `model`;
   *  false means a provider does, and "your machine" must not claim it. */
  | { kind: 'serving'; model: string; local: boolean }
  /** The pick's deployment is launching. */
  | { kind: 'loading'; model: string }
  /** The pick's deployment failed. `why` is the first line of `last_error`. */
  | { kind: 'failed'; model: string; why: string }
  /** No deployment for the pick, and nothing served. */
  | { kind: 'nothing' }

/** The line the deploy manager puts between its own explanation and the
 *  launcher's dump (`control_plane/deploy/manager.py::_fail_launch`). */
export const SPARKRUN_OUTPUT_MARK = '\n--- sparkrun output ---\n'

/** The pick decides when there is a deployment for it -- a failed pick is
 *  reported as failed even while a provider model answers, because the person
 *  asked about that one. Otherwise anything served counts.
 *
 *  A pick can have several records: the restart coordinator relaunches a
 *  failed deployment under a new id and the dead one stays listed. So the
 *  records are read best-first -- one answering beats one loading beats one
 *  failed -- rather than whichever happens to sort first. */
export function doneReading(
  chose: { model: { row: { model_id: string; label: string } } | null },
  deployments: DeploymentDTO[],
  served: ServedModel[],
): DoneReading {
  const pick = chose.model?.row
  const mine = pick ? deployments.filter((d) => d.model_id === pick.model_id) : []
  const up = mine.find((d) => d.state === 'ready' || d.state === 'degraded')
  if (up) return { kind: 'serving', model: up.served_name, local: true }
  const coming = mine.find((d) => d.state === 'launching' || d.state === 'planned')
  if (coming) return { kind: 'loading', model: coming.served_name }
  const dead = mine.find((d) => d.state === 'failed')
  if (dead && pick) {
    const first = dead.last_error?.split('\n')[0]
    return { kind: 'failed', model: pick.label, why: first || 'the launcher reported a failure' }
  }
  // A name a local deployment answers to first: it is the one "your machine"
  // can honestly be said to serve.
  const answering = new Set(
    deployments.filter((d) => d.state === 'ready' || d.state === 'degraded').map((d) => d.served_name),
  )
  const localServed = served.find((m) => answering.has(m.id))
  if (localServed) return { kind: 'serving', model: localServed.id, local: true }
  const first = served[0]
  if (first) return { kind: 'serving', model: first.id, local: false }
  return { kind: 'nothing' }
}

/** A launch failure, split at the launcher's dump: `head` is derate's own
 *  sentence(s), `output` sparkrun's verbatim output or null when there is none.
 *  Neither part is trimmed, re-cased or shortened. */
export function splitLaunchFailure(text: string): { head: string; output: string | null } {
  const at = text.indexOf(SPARKRUN_OUTPUT_MARK)
  if (at < 0) return { head: text, output: null }
  return { head: text.slice(0, at), output: text.slice(at + SPARKRUN_OUTPUT_MARK.length) }
}
