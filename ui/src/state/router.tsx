import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type MouseEvent,
  type ReactNode,
} from 'react'
import { href, parse, type Route } from './routes'

// The React half of the URL scheme. What the scheme *is* -- which path names
// which screen, which selection rides in the query -- lives in `routes.ts`,
// with no React in it, so `router.check.mjs` can verify it in node.

export type { Dest, Route, SheetRef } from './routes'
export { DEFAULT_CONCURRENCY, DEFAULT_CONTEXT, href, parse } from './routes'

export interface RouterApi {
  route: Route
  /** Merge a patch onto the route the address bar holds *now* and go there.
   *
   *  `replace` for a selection somebody is scrubbing through -- clicking around
   *  the machine floor must not build a hundred history entries to Back out of
   *  -- and a push for anything a person would expect Back to undo. */
  navigate: (patch: Partial<Route>, opts?: { replace?: boolean }) => void
  /** The same merge, as an `href` for an `<a>`. Real links, so the browser's
   *  own "copy link address" and middle-click work on the destination tabs. */
  linkTo: (patch: Partial<Route>) => string
}

const Ctx = createContext<RouterApi | null>(null)

function here(): string {
  return window.location.pathname + window.location.search
}

export function RouterProvider({ children }: { children: ReactNode }) {
  const [loc, setLoc] = useState(here)

  useEffect(() => {
    const onPop = () => setLoc(here())
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  const route = useMemo(() => parse(loc), [loc])
  const canonical = href(route)

  // "/" is the entry point everyone actually types, and a stale or hand-edited
  // link can name anything. Both settle onto the canonical spelling with no
  // history entry, so the address bar always describes what is on screen and
  // Back still goes wherever the person came from.
  useEffect(() => {
    if (canonical === here()) return
    window.history.replaceState(null, '', canonical)
    setLoc(canonical)
  }, [canonical])

  const navigate = useCallback<RouterApi['navigate']>((patch, opts) => {
    // Reads the live URL rather than closing over `route`: a handler held from
    // an earlier render would otherwise write its patch onto a stale route and
    // silently revert whatever happened in between.
    const next = href({ ...parse(here()), ...patch })
    if (next === here()) return
    if (opts?.replace) window.history.replaceState(null, '', next)
    else window.history.pushState(null, '', next)
    setLoc(next)
  }, [])

  const linkTo = useCallback<RouterApi['linkTo']>(
    (patch) => href({ ...parse(here()), ...patch }),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reads the live
    // URL; `loc` is here so an href rendered into the DOM is recomputed when
    // the route moves under it.
    [loc],
  )

  const value = useMemo<RouterApi>(
    () => ({ route, navigate, linkTo }),
    [route, navigate, linkTo],
  )

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useRouter(): RouterApi {
  const ctx = useContext(Ctx)
  if (!ctx) throw new Error('useRouter must be used within a RouterProvider')
  return ctx
}

/** True for a click the app should handle itself. A modified click (new tab,
 *  new window, download, or the middle button) is the browser's to keep -- and
 *  keeping it is half the point of rendering destinations as real links. */
export function plainClick(e: MouseEvent): boolean {
  return !(e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0)
}
