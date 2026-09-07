import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { useTopology } from './resources'
import type { TopologyDeployment } from '../api/types'

// Selection semantics transplanted from mockups-next/js/dashboard.js and
// cluster.js (S.sel, S.selNode, S.selLink, S.openDep):
//
//   - selecting a node clears the selected link, and toggles: clicking the
//     already-selected node deselects it.
//   - selecting a link clears the selected node, and toggles the same way.
//   - `pickNode` (used by the per-node telemetry chip strip) sets the node
//     directly, with no toggle-to-deselect, and still clears the link.
//   - the selected deployment is independent of both and never goes empty on
//     its own: it is the thing the dashboard, the sidebar and the flow graph
//     are all "about" at any moment, and it persists across clicks elsewhere.
//   - the sheet (the one modal, see shell/Sheet.tsx) names a kind and an id;
//     it does not otherwise interact with the three selections above.

export type SheetTarget =
  | { kind: 'node' | 'dep'; id: string }
  /** A model id rather than a cluster object: the sheet resolves it
   *  itself, because a model is not something the cluster holds. The two
   *  numbers ride along because the fit verdicts in the ladder are taken
   *  at them, and the sheet has no other way to see what the tab's fields
   *  were set to. */
  | { kind: 'model'; id: string; context: number; concurrency: number }

export interface SelectionApi {
  selDep: string | null
  selNode: string | null
  /** `"a~b"`, node ids sorted, so the same pair always keys the same way
   *  regardless of which end the caller names first. */
  selLink: string | null
  sheet: SheetTarget | null

  selectDep: (servedName: string) => void
  selectNode: (nodeId: string) => void
  selectLink: (a: string, b: string) => void
  pickNode: (nodeId: string) => void
  openSheet: (target: SheetTarget) => void
  closeSheet: () => void
}

export function linkKey(a: string, b: string): string {
  return [a, b].sort().join('~')
}

/** First ready/degraded deployment by tokens/sec descending, then name --
 *  the same ordering the dashboard's deployment strip uses, so "the deployment
 *  the UI is about" defaults to the one doing the most work rather than
 *  whichever the backend happened to list first. */
function defaultDep(deployments: TopologyDeployment[]): string | null {
  const live = deployments.filter((d) => d.state === 'ready' || d.state === 'degraded')
  if (live.length === 0) return null
  return [...live].sort(
    (a, b) =>
      b.tokens_per_sec - a.tokens_per_sec || a.served_name.localeCompare(b.served_name),
  )[0]!.served_name
}

const Ctx = createContext<SelectionApi | null>(null)

export function SelectionProvider({ children }: { children: ReactNode }) {
  const topology = useTopology()
  const deployments = topology.data?.deployments ?? []

  const [explicitDep, setExplicitDep] = useState<string | null>(null)
  const [selNode, setSelNode] = useState<string | null>(null)
  const [selLink, setSelLink] = useState<string | null>(null)
  const [sheet, setSheet] = useState<SheetTarget | null>(null)

  // Falls back the moment the chosen name is no longer being served -- a
  // stopped deployment never leaves the sidebar pointed at a name nothing
  // answers to.
  const selDep = useMemo(() => {
    if (explicitDep != null && deployments.some((d) => d.served_name === explicitDep)) {
      return explicitDep
    }
    return defaultDep(deployments)
  }, [explicitDep, deployments])

  const selectDep = useCallback((servedName: string) => {
    setExplicitDep(servedName)
  }, [])

  const selectNode = useCallback((nodeId: string) => {
    setSelNode((cur) => (cur === nodeId ? null : nodeId))
    setSelLink(null)
  }, [])

  const selectLink = useCallback((a: string, b: string) => {
    const key = linkKey(a, b)
    setSelLink((cur) => (cur === key ? null : key))
    setSelNode(null)
  }, [])

  const pickNode = useCallback((nodeId: string) => {
    setSelNode(nodeId)
    setSelLink(null)
  }, [])

  const openSheet = useCallback((target: SheetTarget) => setSheet(target), [])
  const closeSheet = useCallback(() => setSheet(null), [])

  const value = useMemo<SelectionApi>(
    () => ({
      selDep,
      selNode,
      selLink,
      sheet,
      selectDep,
      selectNode,
      selectLink,
      pickNode,
      openSheet,
      closeSheet,
    }),
    [selDep, selNode, selLink, sheet, selectDep, selectNode, selectLink, pickNode, openSheet, closeSheet],
  )

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useSelection(): SelectionApi {
  const ctx = useContext(Ctx)
  if (!ctx) throw new Error('useSelection must be used within a SelectionProvider')
  return ctx
}
