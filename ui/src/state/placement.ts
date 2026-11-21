import { useCallback, useMemo } from 'react'
import type { ParallelismRequest } from '../api/types'
import { useRouter } from './router'
import { degreesFromRoute } from './routes'

// The deployment shape a model is being planned at: which machines, and at
// which degrees. Both live in the URL (`?on=`, `?tp=`/`?pp=`/`?ep=`) for the same
// reason `?ctx=`/`?seq=` do -- a verdict is only worth sending to somebody if
// the question it answers travels with it. A placement held in component state
// means two people opening one link read two different answers off the same
// screen, which is exactly the failure the scheme exists to prevent.
//
// Deliberately its own module rather than four more members on
// `state/selection.tsx`. What that file holds is a *selection* -- the thing a
// screen is pointed at, toggled by clicking around -- and this is an argument
// to a request. They share a mechanism and nothing else.

export interface PlacementApi {
  /** The machines the operator named, sorted. `null` is "the planner picks",
   *  and is a different request from any array -- including an empty one,
   *  which the gateway answers with a 400. */
  nodeIds: string[] | null
  /** The degrees the operator named, ready to spread into a plan or launch
   *  body. `null` is "the planner picks". */
  degrees: ParallelismRequest | null
  setNodeIds: (next: string[] | null) => void
  setDegrees: (next: ParallelismRequest | null) => void
}

export function usePlacement(): PlacementApi {
  const { route, navigate } = useRouter()

  // Sorted here as well as in `routes.ts` so the identity below is stable
  // across a poll that did not change the URL: `useMemo` on `route.on` is only
  // as good as the array it is handed, and `useModelSession`'s debounced plan
  // effect has both of these in its dependency array. A new array every render
  // would refire it forever -- the failure `useModelSession` documents where
  // it drops a machine that left the cluster.
  const key = route.on ? route.on.join(',') : null
  const nodeIds = useMemo(() => (key ? key.split(',') : null), [key])

  // `data_parallel` mirrors `expert_parallel`; `degreesFromRoute` says why.
  const degrees = useMemo<ParallelismRequest | null>(
    () => degreesFromRoute(route.tp, route.pp, route.ep),
    [route.tp, route.pp, route.ep],
  )

  // Both setters take only `navigate`, so they keep one identity for the life
  // of the pane. `NodeBars` hands them to a checkbox per row, and a new
  // function on every plan that lands would rebuild every row underneath the
  // cursor -- the same identity trap `selection.tsx` documents for its toggles.
  const setNodeIds = useCallback(
    (next: string[] | null) => {
      // `[]` is not a request anybody can mean here -- unticking the last
      // machine hands the choice back rather than asking the gateway to plan
      // on nothing.
      const on = next && next.length ? [...new Set(next)].sort() : null
      navigate({ on }, { replace: true })
    },
    [navigate],
  )

  const setDegrees = useCallback(
    (next: ParallelismRequest | null) => {
      navigate(
        next
          ? {
              tp: next.tensor_parallel ?? 1,
              pp: next.pipeline_parallel ?? 1,
              ep: next.expert_parallel ?? 1,
            }
          : { tp: null, pp: null, ep: null },
        { replace: true },
      )
    },
    [navigate],
  )

  return { nodeIds, degrees, setNodeIds, setDegrees }
}
