import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from 'react'
import { httpBackend, type Backend } from '../api/client'
import { coordinatorBase, subscribeBase } from '../api/origin'

interface BackendCtx {
  backend: Backend
  /** Bumped by any action that changes cluster state, so every polled resource
   *  refetches immediately instead of waiting out its interval. */
  revision: number
  invalidate: () => void
  /** The coordinator base in force, `''` for same origin. Part of every
   *  resource's identity: two coordinators answering the same path are not
   *  answering the same question. */
  origin: string
}

const Ctx = createContext<BackendCtx>({
  backend: httpBackend,
  revision: 0,
  invalidate: () => {},
  origin: '',
})

export function BackendProvider({ children }: { children: ReactNode }) {
  // Live only: one backend, no probe, no fallback. `backend` is never null --
  // every resource below stops asking "is it resolved yet" and just asks
  // "did this poll succeed".
  const [revision, setRevision] = useState(0)
  const invalidate = useCallback(() => setRevision((r) => r + 1), [])

  const origin = useSyncExternalStore(subscribeBase, coordinatorBase, coordinatorBase)

  // A fresh object per base, holding the same methods. `httpBackend` is a
  // module singleton and its methods read the base at call time, so nothing
  // here needs rebuilding -- but every effect downstream keys on the backend's
  // IDENTITY, and without a new one the metrics EventSource would go on
  // streaming from the coordinator you just navigated away from.
  const backend = useMemo(() => ({ ...httpBackend }), [origin])

  const value = useMemo(
    () => ({ backend, revision, invalidate, origin }),
    [backend, revision, invalidate, origin],
  )
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useBackend() {
  return useContext(Ctx)
}

export interface Resource<T> {
  data: T | null
  error: Error | null
  /** True only before the first successful load. A refresh does not blank the
   *  screen: stale structure is better than a flash of nothing. */
  loading: boolean
}

/** Polls one endpoint. Keeps the last good value through a failed refresh. */
export function useResource<T>(
  read: (b: Backend) => Promise<T>,
  intervalMs: number,
): Resource<T> {
  return useKeyedResource('', read, intervalMs)
}

/** `useResource` for a read that has arguments.
 *
 *  `useResource` holds `read` in a ref and re-runs its effect only when the
 *  backend, the interval or the revision change -- which is right for a fixed
 *  endpoint and wrong the moment the read is parameterised. A hook called with
 *  a new node id keeps polling the OLD one until the next tick, and, worse,
 *  goes on returning the old node's `data` in the meantime: another machine's
 *  processes, rendered under this machine's heading, for up to a full interval.
 *  Nothing about that reads as stale, because the numbers are real.
 *
 *  `key` is whatever identifies the read -- a node id, a window, the two
 *  joined. Changing it refetches immediately AND clears `data`, so the caller
 *  gets its "Reading…" state back rather than a confident answer to a
 *  question it is no longer asking. */
export function useKeyedResource<T>(
  key: string,
  read: (b: Backend) => Promise<T>,
  intervalMs: number,
): Resource<T> {
  const { backend, revision, origin } = useBackend()
  // The coordinator is part of the key whether the caller thought about it or
  // not. Without this, pointing the UI at another coordinator would keep the
  // previous cluster's answer on screen through the first poll -- other
  // machines' names and numbers under this cluster's headings, and nothing
  // about them reading as stale, because they are real.
  const fullKey = `${origin}\u0000${key}`
  const [state, setState] = useState<{ key: string; data: T | null; error: Error | null }>({
    key: fullKey,
    data: null,
    error: null,
  })
  const readRef = useRef(read)
  readRef.current = read

  useEffect(() => {
    let alive = true

    const tick = async () => {
      try {
        const next = await readRef.current(backend)
        if (!alive) return
        setState({ key: fullKey, data: next, error: null })
      } catch (e) {
        if (!alive) return
        // Keep the last good value through a failed refresh, exactly as the
        // unkeyed form does -- but only if it belongs to THIS key.
        setState((prev) => ({
          key: fullKey,
          data: prev.key === fullKey ? prev.data : null,
          error: e instanceof Error ? e : new Error(String(e)),
        }))
      }
    }

    void tick()
    const id = window.setInterval(tick, intervalMs)
    return () => {
      alive = false
      window.clearInterval(id)
    }
  }, [backend, intervalMs, revision, fullKey])

  // Read during the render that changed the key, before the effect has run:
  // the previous key's answer is not an answer to this one.
  const stale = state.key !== fullKey
  const data = stale ? null : state.data
  const error = stale ? null : state.error
  return { data, error, loading: data === null && error === null }
}
