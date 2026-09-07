// Where this browser tab sends /api and /v1.
//
// Everything in `client.ts` used to be a bare relative path, and for the
// shipping shape that is correct: the coordinator serves the built assets from
// its own origin, so same-origin IS the coordinator. But that is the only
// shape it was correct for. Under `npm run dev` the UI is on Vite's port and
// only reaches a gateway because vite.config.ts proxies two prefixes to a
// hardcoded :8080; point a browser at a built bundle opened from disk, or at
// one coordinator while wanting to look at another, and there is no way to say
// so without a rebuild.
//
// So the base lives here, in the browser, and nowhere else. It is deliberately
// NOT part of `/api/settings`: that endpoint is on the far side of the very
// connection this configures, and a setting you must already be connected to
// read cannot be the one that tells you how to connect.
//
// Empty string means same origin, which is the default and the deployed shape.
// Nothing about a coordinator that serves its own UI changes because this file
// exists.

const STORAGE_KEY = 'derate.coordinator-base'

/** Reads once at module load. localStorage throws in a sandboxed iframe and
 *  under some private-browsing modes; a UI that will not start because it
 *  could not read an optional preference is worse than one that forgets it. */
function load(): string {
  try {
    return normalizeBase(window.localStorage.getItem(STORAGE_KEY) ?? '') ?? ''
  } catch {
    return ''
  }
}

let current = load()
const listeners = new Set<() => void>()

/** The base every request is prefixed with. `''` means same origin. */
export function coordinatorBase(): string {
  return current
}

/** For `useSyncExternalStore`. Returns an unsubscribe. */
export function subscribeBase(onChange: () => void): () => void {
  listeners.add(onChange)
  return () => listeners.delete(onChange)
}

/** Prefixes one contract path. The path is passed through untouched when
 *  there is no base, so the same-origin case is byte-for-byte what it was. */
export function apiUrl(path: string): string {
  return current ? current + path : path
}

/** What the UI is talking to right now, in words, for display. */
export function describeBase(base: string): string {
  return base || `${window.location.origin} — this page's own origin`
}

/** Trims, supplies a scheme, drops the trailing slash. Returns `''` for a
 *  blank input (meaning same origin) and `null` for something that is not a
 *  URL at all, which the caller reports rather than storing.
 *
 *  A path is kept: a coordinator behind a reverse proxy at `/derate` is a real
 *  deployment, and stripping to the origin would silently talk to the wrong
 *  thing. A query or fragment is not — they would end up in the middle of
 *  every composed URL. */
export function normalizeBase(raw: string): string | null {
  const trimmed = raw.trim()
  if (!trimmed) return ''
  // A leading slash is a path, not a base. Left to the scheme-supplying line
  // below it becomes `http:///api`, whose host is empty -- and `new URL` is
  // perfectly happy with that, so it has to be caught here.
  if (trimmed.startsWith('/')) return null
  // Bare `host:port` is what an operator types. Assume http rather than
  // https: this is a LAN coordinator, and guessing https would fail on the
  // handshake with an error that says nothing about the missing scheme.
  const withScheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(trimmed) ? trimmed : `http://${trimmed}`
  let url: URL
  try {
    url = new URL(withScheme)
  } catch {
    return null
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return null
  if (!url.hostname) return null
  return `${url.origin}${url.pathname}`.replace(/\/+$/, '')
}

/** Persists the base and wakes every subscriber. Takes an already-normalized
 *  value -- the caller has to handle `null` before it gets here, and doing it
 *  in one place keeps "is this a URL" out of the setter. */
export function setCoordinatorBase(base: string): void {
  if (base === current) return
  current = base
  try {
    if (base) window.localStorage.setItem(STORAGE_KEY, base)
    else window.localStorage.removeItem(STORAGE_KEY)
  } catch {
    // Not persisted. It still applies to this tab, which is the part that
    // matters right now; the next reload goes back to same origin.
  }
  for (const listener of listeners) listener()
}

// ── Testing one ──────────────────────────────────────────────────────────────

export interface ProbeResult {
  ok: boolean
  /** The base that was actually probed, normalized. */
  base: string
  /** Round trip to /healthz, in ms. Null when nothing answered. */
  ms: number | null
  /** One sentence, for the operator. Never a stack trace. */
  detail: string
}

/** `/healthz` is the cheapest thing that proves a derate coordinator is on the
 *  other end -- it takes no lock, touches no node, and exists whether or not a
 *  cluster has formed. `/api/cluster` follows only to say what was found,
 *  because "something answered" and "there is a cluster there" are different
 *  answers and an operator pressing Test wants the second one.
 *
 *  Deliberately not a `Backend` method: it probes a candidate base rather than
 *  the configured one, which is the entire point of testing before saving. */
export async function probeCoordinator(base: string, timeoutMs = 6000): Promise<ProbeResult> {
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), timeoutMs)
  const startedAt = performance.now()

  try {
    const res = await fetch(`${base}/healthz`, {
      signal: controller.signal,
      // No credentials: nothing here is cookie-authenticated, and asking for
      // them would forbid a wildcard CORS origin on the server side.
      credentials: 'omit',
      cache: 'no-store',
    })
    const ms = Math.round(performance.now() - startedAt)

    if (!res.ok) {
      return {
        ok: false,
        base,
        ms,
        detail: `Answered ${res.status} ${res.statusText} at /healthz. Something is listening there, but it is not answering as a coordinator.`,
      }
    }

    const body = (await res.json().catch(() => null)) as
      | { ok?: unknown; started_at?: unknown; degraded_startup?: unknown }
      | null
    if (!body || body.ok !== true) {
      return {
        ok: false,
        base,
        ms,
        detail:
          'Something answered on that address, but not with a coordinator health response. Check the port.',
      }
    }

    // `degraded_startup` is a list of "subsystem: reason" (deps.py), not a
    // flag. Empty is the healthy case. Reported rather than swallowed: a
    // coordinator that came up without its registry answers /healthz happily
    // and then explains nothing, which is a confusing place to end up after
    // pressing a button called Test connection.
    const degraded = Array.isArray(body.degraded_startup) ? body.degraded_startup : []
    const note = degraded.length ? ` It started degraded: ${degraded.join('; ')}.` : ''
    return {
      ok: true,
      base,
      ms,
      detail: `Coordinator answered in ${ms} ms. ${await clusterLine(base, controller.signal)}${note}`,
    }
  } catch (e) {
    const aborted = (e as { name?: string } | null)?.name === 'AbortError'
    return {
      ok: false,
      base,
      ms: null,
      detail: aborted
        ? `Nothing answered within ${(timeoutMs / 1000).toFixed(0)} s.`
        : unreachable(base),
    }
  } finally {
    window.clearTimeout(timer)
  }
}

/** A failed `fetch` rejects with a bare TypeError and no detail -- the browser
 *  withholds it on purpose, so DNS failure, connection refused and a blocked
 *  cross-origin response are genuinely indistinguishable from here. Rather
 *  than guess at one, name all three, and name the one this UI can actually
 *  cause: a coordinator that has not been told to allow this page's origin. */
function unreachable(base: string): string {
  const sameOrigin = !base || base.startsWith(window.location.origin)
  const cors = sameOrigin
    ? ''
    : ` Because ${new URL(base).origin} is a different origin from ${window.location.origin}, the coordinator must also allow this page: start it with DERATE_ALLOWED_ORIGINS=${window.location.origin}.`
  return `The browser could not reach it. Nothing is listening, the address is wrong, or the connection was refused.${cors}`
}

/** Best effort, and appended to a success either way: the connection is
 *  already proven by the time this runs, so a cluster read that fails is a
 *  footnote rather than a verdict. */
async function clusterLine(base: string, signal: AbortSignal): Promise<string> {
  try {
    const res = await fetch(`${base}/api/cluster`, {
      signal,
      credentials: 'omit',
      cache: 'no-store',
    })
    if (!res.ok) return `/api/cluster answered ${res.status}.`
    const wire = (await res.json()) as {
      cluster_id?: string
      summary?: { node_count?: number; healthy_nodes?: number }
    }
    const total = wire.summary?.node_count ?? 0
    const healthy = wire.summary?.healthy_nodes ?? 0
    return `Cluster ${wire.cluster_id ?? '—'}: ${total} node${total === 1 ? '' : 's'}, ${healthy} healthy.`
  } catch {
    return 'The cluster could not be read, but the coordinator is up.'
  }
}
