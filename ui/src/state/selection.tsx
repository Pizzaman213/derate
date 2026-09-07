import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  type ReactNode,
} from 'react'
import { useTopology } from './resources'
import { DEFAULT_CONCURRENCY, DEFAULT_CONTEXT, useRouter } from './router'
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
//
// None of it is component state any more: all four live in the URL query
// (state/routes.ts), so a selection is something you can send to somebody.
// The API below is unchanged -- every caller still just says `selectNode(id)`
// -- but what it writes is the address bar, and what it reads is the address
// bar, which is also what makes the Back button work on all of it.
//
// Push or replace, and the reason for each:
//
//   selecting     replace. Clicking across a machine floor is scrubbing, not
//                 navigating; a history entry per click would make Back a
//                 hundred-press undo of something nobody thinks of as an
//                 action. The URL still updates, so it is still shareable.
//   opening the   push. It is a screen, and Back is how people close screens.
//   sheet
//   closing it    replace. The entry the push created becomes a sheet-less
//                 one, so Back from a closed sheet goes to wherever you were
//                 before you opened it rather than reopening it.

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
  const { route, navigate } = useRouter()

  const explicitDep = route.dep
  const selNode = route.node
  const selLink = route.link

  // The URL carries a kind and an id. A model sheet also needs the two numbers
  // its verdicts are taken at, and they come from the same `ctx`/`seq` the
  // models tab reads -- one pair of numbers per URL, so a shared link cannot
  // show a ladder answering a different question from the caption above it.
  const sheet = useMemo<SheetTarget | null>(() => {
    const open = route.sheet
    if (!open) return null
    if (open.kind === 'model') {
      return {
        kind: 'model',
        id: open.id,
        context: route.context ?? DEFAULT_CONTEXT,
        concurrency: route.concurrency ?? DEFAULT_CONCURRENCY,
      }
    }
    return { kind: open.kind, id: open.id }
  }, [route.sheet, route.context, route.concurrency])

  // Falls back the moment the chosen name is no longer being served -- a
  // stopped deployment never leaves the sidebar pointed at a name nothing
  // answers to.
  const selDep = useMemo(() => {
    if (explicitDep != null && deployments.some((d) => d.served_name === explicitDep)) {
      return explicitDep
    }
    return defaultDep(deployments)
  }, [explicitDep, deployments])

  const selectDep = useCallback(
    (servedName: string) => navigate({ dep: servedName }, { replace: true }),
    [navigate],
  )

  const selectNode = useCallback(
    (nodeId: string) =>
      navigate({ node: selNode === nodeId ? null : nodeId, link: null }, { replace: true }),
    [navigate, selNode],
  )

  const selectLink = useCallback(
    (a: string, b: string) => {
      const key = linkKey(a, b)
      navigate({ link: selLink === key ? null : key, node: null }, { replace: true })
    },
    [navigate, selLink],
  )

  const pickNode = useCallback(
    (nodeId: string) => navigate({ node: nodeId, link: null }, { replace: true }),
    [navigate],
  )

  const openSheet = useCallback(
    (target: SheetTarget) =>
      navigate(
        target.kind === 'model'
          ? {
              sheet: { kind: 'model', id: target.id },
              context: target.context,
              concurrency: target.concurrency,
            }
          : { sheet: { kind: target.kind, id: target.id } },
      ),
    [navigate],
  )

  const closeSheet = useCallback(() => navigate({ sheet: null }, { replace: true }), [navigate])

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
