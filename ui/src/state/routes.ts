// The address bar as application state.
//
// Every screen in this app used to live at "/". You could not link somebody to
// the machine you were looking at, bookmark the models list at the context you
// were fitting against, or reload without landing back on the dashboard --
// which for a tool whose whole output is a verdict about one model on one
// cluster is the difference between "look at this" and "click Cluster, then
// spark-03, then scroll".
//
// The scheme, and the rule that decides which half of a URL a thing goes in:
//
//   the PATH names the screen           /dashboard /models /cluster /storage
//                                       /chat /spend /settings
//   ...and the screen's own subject     /models/<model id>
//   the QUERY names what is selected    ?node= ?link= ?dep= ?open= ?ctx= ?seq=
//
// A model id carries slashes (`meta-llama/Llama-3.1-8B`) and they are kept as
// real path separators: `/models/meta-llama/Llama-3.1-8B` is the URL somebody
// would guess, and the id is the whole tail of the path rather than one
// segment. Every other id is a single slug (`spark-01`, `qwen3-30b-a3b`).
//
// Selections are query parameters and not path segments because they are not
// owned by a destination: the same node can be selected on the cluster floor,
// in the dashboard's telemetry strip and in the sidebar roster, and the sheet
// (`?open=`) is a modal that sits over whichever destination is showing.
//
// Deep paths need the coordinator to answer them with `index.html`; it does
// (gateway/app.py, `_UIStatics.get_response`), and vite's dev server does the
// same by default. Break either and a shared link 404s only on reload, which
// is the failure that makes people stop sharing links.

export type Dest = 'dash' | 'models' | 'cluster' | 'storage' | 'chat' | 'spend' | 'settings'

/** What the sheet (shell/Sheet.tsx) is showing. The model kind takes its two
 *  numbers from `ctx`/`seq`, so it is fully described by a kind and an id. */
export interface SheetRef {
  kind: 'node' | 'dep' | 'model'
  id: string
}

/** 8192/1 is what `GET /api/capacity`, the client's own fallback and the models
 *  tab all default to. A route holds `null` for either number when it is at the
 *  default, so the common URL stays `/models` rather than `/models?ctx=8192`. */
export const DEFAULT_CONTEXT = 8192
export const DEFAULT_CONCURRENCY = 1

export interface Route {
  dest: Dest
  /** The model open in the models tab's detail pane. Only on `/models`. */
  model: string | null
  node: string | null
  /** `"a~b"`, node ids sorted -- `linkKey` in state/selection.ts. */
  link: string | null
  /** The deployment the dashboard and the sidebar are "about". */
  dep: string | null
  sheet: SheetRef | null
  context: number | null
  concurrency: number | null
}

const SEGMENT: Record<Dest, string> = {
  dash: 'dashboard',
  models: 'models',
  cluster: 'cluster',
  storage: 'storage',
  chat: 'chat',
  spend: 'spend',
  settings: 'settings',
}

const BY_SEGMENT = new Map<string, Dest>(
  (Object.entries(SEGMENT) as [Dest, string][]).map(([dest, seg]) => [seg, dest]),
)

const SHEET_KINDS = new Set<SheetRef['kind']>(['node', 'dep', 'model'])

/** `:` and `/` are legal unescaped in a query string (RFC 3986 §3.4) and a
 *  browser leaves them alone, so `?open=model:meta-llama/Llama-3.1-8B` stays
 *  readable instead of arriving as `%3A`/`%2F`. `URLSearchParams` would encode
 *  both, which is why this is hand-rolled; parsing accepts either form. */
function enc(value: string): string {
  return encodeURIComponent(value).replace(/%3A/g, ':').replace(/%2F/g, '/')
}

function positive(raw: string | null, fallback: number): number | null {
  if (raw === null) return null
  const n = Number(raw)
  if (!Number.isFinite(n) || n <= 0) return null
  const rounded = Math.round(n)
  // Normalised on the way in, so `parse(href(r))` is `r` for anything this
  // module produced: a URL that spells out the default holds no more
  // information than one that omits it.
  return rounded === fallback ? null : rounded
}

/** `pathname + search`, split and validated. Anything unrecognised -- a typo, a
 *  truncated paste, a link from a newer build -- reads as the dashboard rather
 *  than as an error state, and `RouterProvider` then rewrites the address bar
 *  to say so. */
export function parse(url: string): Route {
  const cut = url.indexOf('?')
  const pathname = cut === -1 ? url : url.slice(0, cut)
  const search = cut === -1 ? '' : url.slice(cut)
  const segments = pathname.split('/').filter(Boolean).map(decodeURIComponent)
  const dest = BY_SEGMENT.get(segments[0] ?? '') ?? 'dash'
  const params = new URLSearchParams(search)

  const model =
    dest === 'models' && segments.length > 1 ? segments.slice(1).join('/') : null

  let sheet: SheetRef | null = null
  const open = params.get('open')
  if (open) {
    const at = open.indexOf(':')
    const kind = at === -1 ? '' : open.slice(0, at)
    const id = at === -1 ? '' : open.slice(at + 1)
    if (id && SHEET_KINDS.has(kind as SheetRef['kind'])) {
      sheet = { kind: kind as SheetRef['kind'], id }
    }
  }

  const wantsNumbers = dest === 'models' || sheet?.kind === 'model'

  return {
    dest,
    model,
    node: params.get('node') || null,
    link: params.get('link') || null,
    dep: params.get('dep') || null,
    sheet,
    context: wantsNumbers ? positive(params.get('ctx'), DEFAULT_CONTEXT) : null,
    concurrency: wantsNumbers ? positive(params.get('seq'), DEFAULT_CONCURRENCY) : null,
  }
}

/** The one canonical spelling of a route. Everything that writes to history
 *  goes through here, so a route that carries a field its destination has no
 *  use for (a model id on `/cluster`, a context on `/spend`) drops it on the
 *  way out rather than accumulating in the address bar. */
export function href(route: Route): string {
  const path = [SEGMENT[route.dest]]
  if (route.dest === 'models' && route.model) {
    path.push(...route.model.split('/').filter(Boolean).map(encodeURIComponent))
  }

  const query: string[] = []
  const put = (key: string, value: string | null) => {
    if (value) query.push(`${key}=${enc(value)}`)
  }
  put('node', route.node)
  put('link', route.link)
  put('dep', route.dep)
  put('open', route.sheet ? `${route.sheet.kind}:${route.sheet.id}` : null)
  if (route.dest === 'models' || route.sheet?.kind === 'model') {
    if (route.context && route.context !== DEFAULT_CONTEXT) {
      put('ctx', String(route.context))
    }
    if (route.concurrency && route.concurrency !== DEFAULT_CONCURRENCY) {
      put('seq', String(route.concurrency))
    }
  }

  return `/${path.join('/')}${query.length ? `?${query.join('&')}` : ''}`
}
