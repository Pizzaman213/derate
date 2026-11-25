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
import { ResourceStore } from './resourceStore'

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
  /** One fetch per named endpoint, shared by every subscriber. One store per
   *  backend, so per coordinator: a new base starts from nothing. */
  store: ResourceStore
}

const Ctx = createContext<BackendCtx>({
  backend: httpBackend,
  revision: 0,
  invalidate: () => {},
  origin: '',
  store: new ResourceStore(httpBackend),
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

  // No close on cleanup: StrictMode runs every effect's cleanup once in
  // development and keeps the memoised store, so a closed store would never
  // fetch again. A replaced store goes idle by itself -- every subscriber's
  // effect keys on `store`, so each one unregisters from the old store, and a
  // name with no registration schedules nothing.
  const store = useMemo(() => new ResourceStore(backend), [backend])
  // The first render is not a change: every registration fetches on its own.
  useEffect(() => {
    if (revision > 0) store.refreshActive()
  }, [revision, store])
  // A background browser tab polls nothing; coming back fetches what is stale.
  useEffect(() => {
    const sync = () => store.setVisible(document.visibilityState !== 'hidden')
    sync()
    document.addEventListener('visibilitychange', sync)
    return () => document.removeEventListener('visibilitychange', sync)
  }, [store])

  const value = useMemo(
    () => ({ backend, revision, invalidate, origin, store }),
    [backend, revision, invalidate, origin, store],
  )
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useBackend() {
  return useContext(Ctx)
}

/** Whether the panes around this one are on screen. Every destination and
 *  every sub-pane stays mounted and is hidden when inactive, so without this
 *  a hidden screen polls exactly as hard as the one being read. */
const ActiveCtx = createContext(true)

/** Wraps a pane that stays mounted while hidden. Inside an inactive scope no
 *  resource fetches; its last answer is kept and it refetches when shown.
 *  Scopes nest: a sub-pane is active only inside an active screen. */
export function ResourceScope({ active, children }: { active: boolean; children: ReactNode }) {
  const outer = useContext(ActiveCtx)
  return <ActiveCtx.Provider value={outer && active}>{children}</ActiveCtx.Provider>
}

// Module-level so `useSyncExternalStore` sees one subscribe function.
function subscribeVisibility(onChange: () => void) {
  document.addEventListener('visibilitychange', onChange)
  return () => document.removeEventListener('visibilitychange', onChange)
}

/** False while the browser tab is in the background. */
export function usePageVisible(): boolean {
  return useSyncExternalStore(
    subscribeVisibility,
    () => document.visibilityState !== 'hidden',
    () => true,
  )
}

/** A polled endpoint shared by name: every component asking for `name` gets
 *  one read and one snapshot, polled at the shortest interval any LIVE
 *  subscriber asked for. A subscriber in a hidden pane does not count, and a
 *  hidden browser tab polls nothing at all (`ResourceStore.setVisible`).
 *
 *  `name` must identify the read completely: two different reads under one
 *  name would hand one subscriber the other's answer. A read with arguments
 *  stays on `useKeyedResource`. */
export function useSharedResource<T>(
  name: string,
  read: (b: Backend) => Promise<T>,
  intervalMs: number,
): Resource<T> {
  const { store } = useBackend()
  const live = useContext(ActiveCtx)
  const readRef = useRef(read)
  readRef.current = read
  useEffect(
    () => (live ? store.register(name, (b) => readRef.current(b), intervalMs) : undefined),
    [store, name, intervalMs, live],
  )
  const subscribe = useCallback((cb: () => void) => store.listen(name, cb), [store, name])
  const snapshot = () => store.snapshot(name)
  return useSyncExternalStore(subscribe, snapshot, snapshot) as Resource<T>
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
  // A hidden pane or a background browser tab polls nothing and keeps what it
  // has; becoming live re-runs the effect, which fetches at once.
  // Both hooks run on every render: `&&` would skip the second one whenever the
  // pane is hidden, and a hook called conditionally breaks React's hook order.
  const inScope = useContext(ActiveCtx)
  const pageVisible = usePageVisible()
  const live = inScope && pageVisible

  useEffect(() => {
    if (!live) return
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
  }, [backend, intervalMs, revision, fullKey, live])

  // Read during the render that changed the key, before the effect has run:
  // the previous key's answer is not an answer to this one.
  const stale = state.key !== fullKey
  const data = stale ? null : state.data
  const error = stale ? null : state.error
  return { data, error, loading: data === null && error === null }
}
