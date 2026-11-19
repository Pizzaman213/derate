// Who is looking at this screen, as the coordinator sees it.
//
// The gateway answers /api only to a session (control_plane/gateway/authz.py),
// so the first question every load asks is `GET /api/auth/state`: is there an
// admin at all (no -> /setup), and does this browser hold a session (no ->
// /login). Everything that polls -- the metrics stream, the telemetry strip,
// every panel -- mounts only once the answer is "yes", which is why this sits
// between BackendProvider and the rest of the tree rather than inside a tab.
//
// A 401 from anywhere later (an expired session, an admin removed in another
// tab) arrives as `UNAUTHENTICATED_EVENT` from api/client.ts; this re-asks the
// state rather than trusting the event, so a 401 on one unrelated call cannot
// sign a valid session out.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { UNAUTHENTICATED_EVENT } from '../api/client'
import type { AuthState } from '../api/types'
import { useBackend } from './backend'

interface AuthApi {
  /** Null until the first answer. A failed request leaves it null too: the
   *  shell then shows nothing rather than guessing which screen is right. */
  state: AuthState | null
  /** Asks the coordinator again. Login, setup and logout call this. */
  refresh: () => Promise<AuthState | null>
  logout: () => Promise<void>
  /** Why the state could not be read, verbatim, or null. */
  error: string | null
}

const AuthContext = createContext<AuthApi | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const { backend } = useBackend()
  const [state, setState] = useState<AuthState | null>(null)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const next = await backend.authState()
      setState(next)
      setError(null)
      return next
    } catch (e) {
      setError(String((e as Error)?.message ?? e))
      return null
    }
  }, [backend])

  const logout = useCallback(async () => {
    try {
      await backend.logout()
    } finally {
      await refresh()
    }
  }, [backend, refresh])

  useEffect(() => {
    void refresh()
    const on = () => void refresh()
    window.addEventListener(UNAUTHENTICATED_EVENT, on)
    return () => window.removeEventListener(UNAUTHENTICATED_EVENT, on)
  }, [refresh])

  const api = useMemo(() => ({ state, refresh, logout, error }), [state, refresh, logout, error])
  return <AuthContext.Provider value={api}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthApi {
  const api = useContext(AuthContext)
  if (!api) throw new Error('useAuth outside AuthProvider')
  return api
}
