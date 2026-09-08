import type { Activity, DownloadActivity, LaunchActivity } from '../api/types'
import { phaseLabel } from '../state/launchPhase'
import { remainingLabel } from '../format'

/** The rail's "what is happening" rows, as data.
 *
 *  Split from `ActivitySection.tsx` the way `tabs/models/rows.ts` is split from
 *  `CatalogList.tsx`, and for the same reason: the rules below are the part
 *  that can be wrong in a way types cannot catch, and `activity.check.mjs`
 *  pins every one of them.
 */

export type ActivityKind = 'download' | 'launch'

export interface ActivityRow {
  key: string
  kind: ActivityKind
  /** The model, as whoever is fetching or serving it names it. */
  title: string
  /** Where it is going: a provider's display name, or the nodes it launches
   *  onto. Empty when nothing said. */
  target: string
  /** The server's own sentence about what it is doing, or '' when it has not
   *  said anything yet. Rendered verbatim -- never rewritten here. */
  status: string
  /** 0..1 for a bar, or null when there is no honest number.
   *
   *  Null and 0 are NOT interchangeable: `ProportionBar` draws null as a
   *  dashed empty track and 0 as a solid empty one, on the documented rule
   *  that a missing value must never be pixel-identical to zero. */
  value: number | null
  /** The figures under the bar, already worded. */
  detail: string
  signal: 'live' | 'warn' | 'fault'
  /** Sort key and the "for how long" figure, in unix seconds. */
  since: number
  error: string | null
}

const GIB = 1024 ** 3

function gib(bytes: number): string {
  return (bytes / GIB).toFixed(bytes >= 10 * GIB ? 0 : 1)
}

/** A transfer's share of itself, or null while nothing has sized it.
 *
 *  A pull spends its first seconds in "pulling manifest" with no total, and a
 *  solid 0% bar there states that a download which has not been measured is
 *  0% done. Clamped above because a provider's last frame can report a
 *  `completed` a few bytes past its own `total`. */
export function downloadValue(d: DownloadActivity): number | null {
  if (d.total == null || d.total <= 0) return null
  return Math.max(0, Math.min(1, d.completed / d.total))
}

function downloadRow(d: DownloadActivity): ActivityRow {
  return {
    key: `download:${d.pull_id}`,
    kind: 'download',
    title: d.model,
    target: d.provider,
    status: d.status,
    value: downloadValue(d),
    detail:
      d.total == null
        ? // Not "0 of 0 GiB": nothing has said how big this is yet, and a
          // denominator of zero is an invented measurement.
          'sizing'
        : `${gib(d.completed)} / ${gib(d.total)} GiB`,
    signal: d.error ? 'fault' : d.done ? 'live' : 'warn',
    since: 0,
    error: d.error,
  }
}

/** A launch's share of itself, which for almost all of a launch is nothing.
 *
 *  This function used to be the constant `null`, on the reasoning that the
 *  weights are downloaded inside a container the control plane cannot see
 *  into. The container turned out to be readable -- `sparkrun logs` reaches
 *  it and the manager reads it while the launch is still in flight -- and
 *  during exactly one step of a launch the runtime counts its own checkpoint
 *  shards and prints "5/11". That pair is a measurement somebody else made.
 *
 *  Every other step still returns null, and that is not an omission to be
 *  fixed later: nothing reports a total for the image pull, the weight
 *  download, the compile or the graph capture, so a bar for any of them would
 *  be filling at a rate this project made up. */
export function launchValue(l: LaunchActivity): number | null {
  if (l.fraction == null || !Number.isFinite(l.fraction)) return null
  return Math.max(0, Math.min(1, l.fraction))
}

function launchRow(l: LaunchActivity): ActivityRow {
  return {
    key: `launch:${l.deployment_id}`,
    kind: 'launch',
    title: l.served_name,
    target: l.node_ids.join(', '),
    // What sparkrun or the runtime said it was doing, and only the FSM state
    // ("launching") when neither has said anything yet. Verbatim either way:
    // "Pulling image: ghcr.io/..." and "Loading safetensors checkpoint
    // shards: 5/11" are more precise than any sentence written here, which is
    // the same rule the download rows above already follow.
    status: l.status || l.state,
    value: launchValue(l),
    // The step, how long it says it has left, then the runtime running it.
    // The step leads because it is the part that changes; an unrecognised
    // phase contributes nothing rather than a raw identifier, and a step that
    // measures no total contributes no estimate rather than a guess.
    detail: [phaseLabel(l.phase), remainingLabel(l.eta_s), l.runtime]
      .filter(Boolean)
      .join(' · '),
    // `fatal` as well as `last_error`: the runtime announcing its own death
    // reaches this row before the manager has finished writing the failure
    // onto the record, and a lamp that stays amber over "EngineCore failed to
    // start." is the row saying it is fine while quoting the opposite.
    signal: l.last_error || l.fatal ? 'fault' : 'warn',
    since: l.since,
    error: l.last_error,
  }
}

/** Every row the rail should draw, in the order it should draw them.
 *
 *  Downloads first, then launches, each oldest-first. Order has to be stable
 *  while the numbers underneath it move: a row that reshuffles as its own bar
 *  advances is unreadable, which is why nothing here sorts by progress. */
export function activityRows(activity: Activity | null | undefined): ActivityRow[] {
  if (!activity) return []
  const downloads = (activity.downloads ?? []).map(downloadRow)
  const launches = (activity.launches ?? [])
    .map(launchRow)
    .sort((a, b) => a.since - b.since)
  return [...downloads, ...launches]
}
