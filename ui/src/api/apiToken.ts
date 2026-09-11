// The bearer token this browser tab sends on every /api request, when the
// coordinator was started with DERATE_API_TOKEN set (see gateway/auth.py).
//
// Unset on this coordinator, and unset in most deployments -- /api has no
// authentication by default. This exists only for the operator who did set
// DERATE_API_TOKEN and would otherwise be locked out of their own UI: without
// somewhere to put the token, every request client.ts makes would come back
// 401 the moment the coordinator started requiring one.
//
// Lives in localStorage, per browser, same as api/origin.ts's coordinator
// base and for the same reason: it is what this tab uses to reach the
// coordinator, so it cannot itself be a setting read through that connection.
// It is never sent anywhere except as the Authorization header on requests
// this tab already makes to its own configured coordinator.

const STORAGE_KEY = 'derate.api-token'

/** Reads once at module load. localStorage throws in a sandboxed iframe and
 *  under some private-browsing modes; a UI that will not start because it
 *  could not read an optional preference is worse than one that forgets it. */
function load(): string {
  try {
    return window.localStorage.getItem(STORAGE_KEY) ?? ''
  } catch {
    return ''
  }
}

let current = load()
const listeners = new Set<() => void>()

/** The token every request carries as `Authorization: Bearer <token>`, or
 *  `''` when none is set -- the ordinary case, and a no-op on that request. */
export function apiToken(): string {
  return current
}

/** For `useSyncExternalStore`. Returns an unsubscribe. */
export function subscribeToken(onChange: () => void): () => void {
  listeners.add(onChange)
  return () => listeners.delete(onChange)
}

/** Persists the token and wakes every subscriber. */
export function setApiToken(token: string): void {
  const trimmed = token.trim()
  if (trimmed === current) return
  current = trimmed
  try {
    if (trimmed) window.localStorage.setItem(STORAGE_KEY, trimmed)
    else window.localStorage.removeItem(STORAGE_KEY)
  } catch {
    // Not persisted. It still applies to this tab, which is the part that
    // matters right now; the next reload goes back to unset.
  }
  for (const listener of listeners) listener()
}
