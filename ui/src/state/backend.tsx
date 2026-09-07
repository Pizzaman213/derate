import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { httpBackend, type Backend } from '../api/client'

interface BackendCtx {
  backend: Backend
  /** Bumped by any action that changes cluster state, so every polled resource
   *  refetches immediately instead of waiting out its interval. */
  revision: number
  invalidate: () => void
}

const Ctx = createContext<BackendCtx>({
  backend: httpBackend,
  revision: 0,
  invalidate: () => {},
})

export function BackendProvider({ children }: { children: ReactNode }) {
  // Live only: one backend, no probe, no fallback. `backend` is never null --
  // every resource below stops asking "is it resolved yet" and just asks
  // "did this poll succeed".
  const [revision, setRevision] = useState(0)
  const invalidate = useCallback(() => setRevision((r) => r + 1), [])

  const value = useMemo(
    () => ({ backend: httpBackend, revision, invalidate }),
    [revision, invalidate],
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
  const { backend, revision } = useBackend()
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const readRef = useRef(read)
  readRef.current = read

  useEffect(() => {
    let alive = true

    const tick = async () => {
      try {
        const next = await readRef.current(backend)
        if (!alive) return
        setData(next)
        setError(null)
      } catch (e) {
        if (!alive) return
        setError(e instanceof Error ? e : new Error(String(e)))
      }
    }

    void tick()
    const id = window.setInterval(tick, intervalMs)
    return () => {
      alive = false
      window.clearInterval(id)
    }
  }, [backend, intervalMs, revision])

  return { data, error, loading: data === null && error === null }
}
