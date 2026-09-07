import { repoIdIsUnambiguous } from '../../api/modelcache'
import type { CuratedModel, DeploymentDTO, NodeStorage } from '../../api/types'

export interface ModelOption {
  model_id: string
  label: string
  detail: string
  /** Adopted into the Context/Seqs fields on pick, when the source knows a
   *  real number. A cached entry never does -- nothing has read its config. */
  default_context?: number
  default_concurrency?: number
  /** Listed but not choosable, with `detail` saying why. */
  disabled?: boolean
}

export interface ModelGroup {
  label: string
  options: ModelOption[]
}

const CURATED = 'Curated'
const SERVING = 'Serving now'
const CACHED = 'Downloaded here'
const SELECTED = 'Selected'

/** The picker's groups, in order, deduped.
 *
 *  Rules that are not obvious:
 *
 *  - **Served names are not repository ids.** The serving group is built from
 *    `DeploymentDTO.model_id`, never from `/v1/models`, whose entries are
 *    served names and include remote provider models. A served name cannot be
 *    planned.
 *  - **Exact string equality, never case-folded.** HuggingFace ids are
 *    case-sensitive, and a cached `models--Qwen--Qwen3-30B-A3B` already decodes
 *    to the identical string, so exact matching dedupes it correctly with no
 *    normalisation to get wrong.
 *  - **First group wins.** Curated carries the richest label and the defaults;
 *    everything serving is also downloaded, so the more specific fact wins.
 *  - **An empty group renders nothing.** An empty heading is a claim that a
 *    category exists and is empty.
 *  - **`Selected` exists for a disappearing option.** If a serving model is
 *    picked and its deployment then stops, its `<option>` would vanish while
 *    the select still holds the id, and the field would render blank. Same
 *    failure `selection.tsx` guards for with `selDep`.
 */
export function modelGroups(input: {
  curated: CuratedModel[]
  deployments: DeploymentDTO[]
  storage: NodeStorage[] | null
  selected: string | null
}): ModelGroup[] {
  const seen = new Set<string>()
  const groups: ModelGroup[] = []

  const add = (label: string, options: ModelOption[]) => {
    const fresh = options.filter((o) => {
      if (seen.has(o.model_id)) return false
      seen.add(o.model_id)
      return true
    })
    if (fresh.length) groups.push({ label, options: fresh })
  }

  add(
    CURATED,
    input.curated.map((m) => ({
      model_id: m.model_id,
      label: m.label,
      detail: m.detail,
      default_context: m.default_context,
      default_concurrency: m.default_concurrency,
    })),
  )

  const live = input.deployments.filter(
    (d) => d.state !== 'stopped' && d.state !== 'failed',
  )
  add(
    SERVING,
    live.map((d) => ({
      model_id: d.model_id,
      label: d.served_name,
      detail: `${d.runtime} · ${d.state}`,
      // Not a guess: this is how the model is actually configured right now.
      default_context: d.context_length,
      default_concurrency: d.max_concurrent_seqs,
    })),
  )

  // One option per repository, across every node that holds it.
  const holders = new Map<string, { repo: string; nodes: string[]; exact: boolean; folder: string }>()
  for (const node of input.storage ?? []) {
    for (const repo of node.models?.repos ?? []) {
      const key = repo.folder
      const entry = holders.get(key)
      if (entry) entry.nodes.push(node.node_id)
      else
        holders.set(key, {
          repo: repo.repo_id,
          nodes: [node.node_id],
          exact: repoIdIsUnambiguous(repo),
          folder: repo.folder,
        })
    }
  }
  add(
    CACHED,
    [...holders.values()]
      .sort((a, b) => a.repo.localeCompare(b.repo))
      .map((h) => ({
        model_id: h.exact ? h.repo : h.folder,
        label: h.repo,
        detail: h.exact
          ? h.nodes.length === 1
            ? `on ${h.nodes[0]}`
            : `on ${h.nodes.length} machines`
          : 'ambiguous folder name',
        disabled: !h.exact,
      })),
  )

  if (input.selected && !seen.has(input.selected)) {
    add(SELECTED, [
      { model_id: input.selected, label: input.selected, detail: '' },
    ])
  }

  return groups
}
