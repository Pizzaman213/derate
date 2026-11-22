// What a node's agent is running, against the coordinator it answers to.
//
// Pure and React-free, because check.mjs bundles it with esbuild's
// `platform: 'neutral'` and `software.check.mjs` drives it directly.
//
// Everything here WARNS and nothing refuses: a rolling upgrade means some node
// is always on the other build for a while, and that is allowed. The two
// sentences that matter are kept apart on purpose -- "upgrade this node" is
// only true when the node is on another build (or too old to say); a node on
// the coordinator's own build that lacks a feature had it fail to start, and
// telling the operator to upgrade it would send them to fix the wrong thing.

/** The slice of a node row this reads. `NodeStateDTO` satisfies it. */
export interface SoftwareInput {
  healthy: boolean
  build?: string | null
  /** The server's comparison; never recomputed here. */
  build_skew?: boolean
  /** null/absent = the agent has not said; [] = said, and has none. */
  features?: string[] | null
}

export interface FeatureRow {
  feature: string
  label: string
  present: boolean
}

export interface NodeSoftware {
  /** "<name> runs build X; this coordinator runs Y.", or null when not skewed. */
  skew: string | null
  /** Every feature this coordinator knows, then any newer ones the node has.
   *  null when the agent has not said what it can do. */
  features: FeatureRow[] | null
  /** One sentence per thing an operator can act on, in the order to act. */
  gaps: string[]
}

/** `copy_receiver` -> `copy receiver`. The server names; this only spaces. */
export const featureLabel = (feature: string): string => feature.replace(/_/g, ' ')

export function nodeSoftware(
  name: string,
  node: SoftwareInput,
  coordinatorBuild: string | null | undefined,
  known: readonly string[],
): NodeSoftware {
  const build = node.build || null
  const ours = coordinatorBuild || null
  const skew = node.build_skew && build
    ? ours
      ? `${name} runs build ${build}; this coordinator runs ${ours}.`
      : `${name} runs build ${build}, which is not this coordinator's.`
    : null

  const said = node.features ?? null
  const features = said === null
    ? null
    : [
        ...known.map((f) => ({ feature: f, label: featureLabel(f), present: said.includes(f) })),
        ...said.filter((f) => !known.includes(f)).map((f) => ({ feature: f, label: featureLabel(f), present: true })),
      ]

  const gaps: string[] = []
  // A gateway that predates the field knows no features, and then nothing can
  // be said to be missing -- not even "too old to say".
  if (known.length > 0) {
    if (features === null) {
      // Only an agent that answered can be too old to say. One that has not
      // answered yet (or is down) has told us nothing either way.
      if (node.healthy) gaps.push(`${name} does not report what it can do, so its agent predates this coordinator: upgrade this node.`)
    } else {
      // Same build only when both are named and the server says they agree;
      // an unknown on either side is not agreement.
      const same = build !== null && ours !== null && !node.build_skew
      for (const row of features) {
        if (row.present) continue
        gaps.push(
          same
            ? `${name} has no ${row.label}: it runs the coordinator's build, so it failed to start there; its node log says why.`
            : `${name} has no ${row.label}: upgrade this node.`,
        )
      }
    }
  }
  return { skew, features, gaps }
}

/** The compact list: "copy receiver · link health · no transfer capacity". */
export function featureLine(rows: readonly FeatureRow[]): string {
  return rows.map((r) => (r.present ? r.label : `no ${r.label}`)).join(' · ')
}
