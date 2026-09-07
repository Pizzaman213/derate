import type { NodeStateDTO, TopologyNode } from '../api/types'

/** What to call a machine on screen, in one place.
 *
 *  Two rules, and the second is the one that matters:
 *
 *  1. A machine is called by its label if an operator gave it one, and by its
 *     `node_id` otherwise. Never by its hostname — `hostname` is not unique.
 *     A worker in a `--network host` container reports the host's hostname, so
 *     a two-node Spark can and does show two machines both called
 *     "spark-4d38", which is what this module exists to stop.
 *
 *  2. Whatever is shown, `node_id` stays the identity. Deployments, link
 *     measurements, routing targets and the saved graph arrangement are all
 *     keyed by it, so a renamed plate carries its id in the line beneath and
 *     in its tooltip. A caption you cannot map back to the thing the rest of
 *     the app is talking about is a worse bug than a duplicate name.
 */

/** Mirrors `MAX_LABEL_LEN` in control_plane/registry/labels.py. Enforced there
 *  -- this only stops the field accepting what the gateway is going to refuse,
 *  so the rejection arrives as a full input rather than as an error message. */
export const MAX_LABEL_LEN = 48

/** The two fields naming needs. Both wire shapes already carry them, so a
 *  caller passes whichever it happens to hold. */
export interface Nameable {
  node_id: string
  label?: string | null
}

export function nodeName(node: Nameable | null | undefined, fallback = ''): string {
  if (!node) return fallback
  const label = node.label?.trim()
  return label || node.node_id || fallback
}

/** The identity line under the name, or '' when the name already is it.
 *
 *  `hostname` is optional because most callers have a `Nameable` and nothing
 *  else; it only ever appears here when it disagrees with the node_id, which
 *  is exactly the container case above.
 */
export function nodeSubtitle(node: Nameable | null | undefined, hostname?: string): string {
  if (!node) return ''
  const label = node.label?.trim()
  // Renamed: the id everything else keys by is no longer on the plate, so it
  // becomes the sub-line. The hostname is one level further down, in the sheet.
  if (label && label !== node.node_id) return node.node_id
  const host = hostname?.trim()
  return host && host !== node.node_id ? host : ''
}

/** `name(id)` for the surfaces that hold ids rather than nodes — link chips,
 *  deployment node lists, the reach report's legs.
 *
 *  Falls back to the id itself for anything not in the roster: an id we cannot
 *  name is still an id, and rendering it beats rendering a blank. */
export function nameIndex(
  nodes: (Nameable | undefined)[] | null | undefined,
): (id: string) => string {
  const byId = new Map<string, string>()
  for (const node of nodes ?? []) {
    if (node?.node_id) byId.set(node.node_id, nodeName(node))
  }
  return (id: string) => byId.get(id) ?? id
}

/** The roster shapes, unwrapped. `/api/cluster` nests the profile; the
 *  topology payload does not. */
export const fromState = (n: NodeStateDTO): Nameable => ({
  node_id: n.profile.node_id,
  label: n.label,
})
export const fromTopology = (n: TopologyNode): Nameable => n
