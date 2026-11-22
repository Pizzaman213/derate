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
//   the PATH names the screen           /dashboard /models /cluster
//                                       /chat /usage /settings
//   ...and the screen's own subject     /models/<model id>
//                                       /usage/requests/<id>  /usage/keys/<id>
//   the QUERY names what is selected    ?node= ?link= ?dep= ?open= ?ctx= ?seq=
//   ...and, on a model, the shape it is    ?on= ?tp= ?pp= ?ep=
//   being planned at
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

export type Dest =
  | 'dash'
  | 'models'
  | 'cluster'
  | 'chat'
  // Who used what, what it cost, and under which key: one destination with
  // three panes (USAGE_SUBS) in the path -- `/usage` (the overview),
  // `/usage/requests`, `/usage/keys`. A request id and a key id are subjects
  // under their pane -- `/usage/requests/r-abc123`, `/usage/keys/<key id>` --
  // the way `/models/<id>` is, so a chat turn, a key page or a health banner
  // can link straight to the one thing it is about.
  | 'usage'
  | 'settings'
  // Not in the header's nav, and deliberately still a real destination. First
  // run is a screen with a subject of its own, so by the rule at the top of
  // this file it belongs in the path -- which is also what makes it linkable,
  // reloadable, and re-runnable by typing it, rather than a mode you can only
  // reach by having installed the product ten seconds ago.
  | 'setup'
  // Off the nav too, and before the frame like setup: the one screen a
  // browser without a session can reach. Every other destination redirects
  // here (shell/AppShell.tsx `useAuthGate`).
  | 'login'

/** What the sheet (shell/Sheet.tsx) is showing. The model kind takes its two
 *  numbers from `ctx`/`seq`, so it is fully described by a kind and an id. */
export interface SheetRef {
  kind: 'node' | 'dep' | 'model'
  id: string
}

/** What the advanced fields show before anybody overrides anything.
 *
 *  These are NOT what a missing `?ctx=`/`?seq=` means any more, and the
 *  difference is the whole point. Absent means "the coordinator chooses", per
 *  model, from the fit arithmetic -- which is what lets the models screen show
 *  real verdicts on a fresh install with nothing typed anywhere. `?ctx=8192`
 *  means somebody asked for 8192 and every verdict is taken there instead.
 *
 *  So `parse('/models?ctx=8192').context` is 8192 and not null, and it round
 *  trips: two URLs, because there are two questions. What they are here for is
 *  the number the disclosure shows in its box before it has an answer to show,
 *  and the client's fallback when a caller does have to name one. */
export const DEFAULT_CONTEXT = 8192
export const DEFAULT_CONCURRENCY = 1

/** The window the spend screen is drawn over. `all` is the archive's 1d-rollup
 *  horizon (400 days). */
export const SPEND_WINDOWS = ['24h', '7d', '30d', 'all'] as const
export type SpendWindow = (typeof SPEND_WINDOWS)[number]

/** The windows the requests screen offers: the raw request table's horizon is
 *  30 days, so `all` would claim a reach it does not have. */
export const REQUEST_WINDOWS = ['24h', '7d', '30d'] as const

/** The panes of /usage, in nav order. `overview` is the bare `/usage`. */
export const USAGE_SUBS = ['overview', 'requests', 'rate-limits', 'keys'] as const
export type UsageSub = (typeof USAGE_SUBS)[number]

export interface Route {
  dest: Dest
  /** Which pane of `/usage`: `overview` on the bare path, null off `/usage`. */
  sub: UsageSub | null
  /** The model open in the models tab's detail pane. Only on `/models`. */
  model: string | null
  /** The key whose detail page is open. Only on `/usage/keys`. */
  key: string | null
  node: string | null
  /** `"a~b"`, node ids sorted -- `linkKey` in state/selection.ts. */
  link: string | null
  /** The deployment the dashboard and the sidebar are "about". */
  dep: string | null
  sheet: SheetRef | null
  /** An override, or `null` for "the coordinator chooses". Not a value with a
   *  default: the fit gate picks a context per model from what actually fits,
   *  clamped to the model's own window, and that is the answer on the default
   *  path. These are only set once somebody opens the advanced disclosure. */
  context: number | null
  concurrency: number | null
  /** The machines the operator named, sorted and deduplicated. `null` means the
   *  planner picks and sends no `node_ids` -- the two are different requests,
   *  so absence is preserved rather than collapsed to "every machine". */
  on: string[] | null
  /** The degrees the operator named. Held as a SET because `parallelism` is
   *  adopted as a whole object (tabs/models/DegreeFields.tsx): with an
   *  omitted key meaning 1 on the wire, "TP mine, PP the planner's" cannot be
   *  expressed at all, so either all three are here or none are.
   *
   *  `ep` joined them when expert parallel became a field rather than an API
   *  it was possible to reach only by hand. There is no `dp`: the data-parallel
   *  degree is not independently selectable anywhere in this product -- vLLM
   *  builds no rank group for expert parallel, so its size IS `dp * tp`, and
   *  the cross-node shape the planner emits is `dp = ep` with `tp = 1`.
   *  `state/placement.ts` writes that pairing into the request; the coordinator
   *  is still the only thing that judges it. */
  tp: number | null
  pp: number | null
  ep: number | null
  /** Speculative decoding, as `method:k` — `spec=ngram:5`. `null` means one
   *  token per step, which is what every URL written before this existed meant
   *  and what the coordinator does when the field is absent.
   *
   *  A pair in one parameter rather than two, and not for brevity: the two
   *  halves are never independently meaningful. A method with no token count
   *  is not a request the fit gate can price, and a token count with no method
   *  names nothing — so unlike `tp`/`pp`, which each stand alone at 1, there is
   *  no sensible value to fill a missing half with. One parameter cannot be
   *  half-written. */
  spec: SpecRef | null
  /** The window a screen is drawn over. On `/usage` (overview) any of
   *  SPEND_WINDOWS, null meaning `all`; on `/usage/requests` only 24h/7d/30d,
   *  null meaning 24h. */
  win: SpendWindow | null
  /** The request open in the requests pane's detail. Only on `/usage/requests`. */
  request: string | null
  /** Filters for requests and rate-limit history. `dep` selects a model on
   *  rate limits; provider/target narrow upstream attribution. */
  fkey: string | null
  fmodel: string | null
  fstatus: 'ok' | 'failed' | '429' | null
  ftarget: string | null
  fprovider: string | null
  fretried: boolean
}

/** A speculative-decoding selection: which method, and how many tokens to
 *  draft per step. The method is not narrowed to the union the API declares —
 *  a URL is somebody else's text, and a method this build does not know must
 *  round-trip to the server and come back as its refusal, not be silently
 *  dropped here into a launch that speculates differently than the link said. */
export interface SpecRef {
  method: string
  tokens: number
  /** A separately-published draft head's repository, for a method whose draft
   *  is not in the target's own checkpoint. Written as its own `?head=`
   *  parameter rather than a third colon-separated field: a repository id is
   *  somebody else's string and may contain a colon (a quant tag does), so
   *  packing it into `spec=` would make the separator ambiguous the first time
   *  one did. */
  model?: string
}

/** The degrees a route names, as the request wants them, or null for "the
 *  planner picks". `data_parallel` mirrors `expert_parallel` and is never its
 *  own control: vLLM's expert-parallel size IS `dp * tp`, so across machines
 *  the only shape that means EP=n is `dp=n, tp=1`. The coordinator still
 *  judges the pairing (`legality.py::ep_rejection`). */
export function degreesFromRoute(
  tp: number | null,
  pp: number | null,
  ep: number | null,
): { tensor_parallel: number; pipeline_parallel: number; expert_parallel: number; data_parallel: number } | null {
  return tp === null && pp === null && ep === null
    ? null
    : {
        tensor_parallel: tp ?? 1,
        pipeline_parallel: pp ?? 1,
        expert_parallel: ep ?? 1,
        data_parallel: ep ?? 1,
      }
}

const SEGMENT: Record<Dest, string> = {
  dash: 'dashboard',
  models: 'models',
  cluster: 'cluster',
  chat: 'chat',
  usage: 'usage',
  settings: 'settings',
  setup: 'setup',
  login: 'login',
}

/** Every destination, at runtime.
 *
 *  `Dest` is a type and is gone by the time anything runs, so a verifier that
 *  wants to walk every screen has to be handed the list -- and a hand-kept
 *  copy of it is the exact thing `check.mjs` refuses to keep for verifiers,
 *  for the exact reason: it stops covering the newest one the moment somebody
 *  adds it. `screens.check.mjs` claimed to capture "one per destination in
 *  state/routes.ts" while walking a literal array, and `/speech` was invisible
 *  to it for as long as that was true.
 *
 *  Derived from `SEGMENT` rather than written beside it, because SEGMENT is
 *  the map that has to be complete anyway: a `Dest` missing from it parses as
 *  the dashboard and href()s to `/undefined`, silently, in both directions. */
export const DESTINATIONS = Object.keys(SEGMENT) as Dest[]

const BY_SEGMENT = new Map<string, Dest>(
  (Object.entries(SEGMENT) as [Dest, string][]).map(([dest, seg]) => [seg, dest]),
)

/** The three destinations /usage replaced on 2026-11-18. Read by parse() so
 *  a link somebody shared or bookmarked still lands; never written by href(),
 *  so the address bar is rewritten to the one spelling on arrival. */
const LEGACY_USAGE: Record<string, UsageSub> = { spend: 'overview', keys: 'keys', requests: 'requests' }

const SHEET_KINDS = new Set<SheetRef['kind']>(['node', 'dep', 'model'])

/** `:`, `/` and `,` are all legal unescaped in a query string (RFC 3986 §3.4)
 *  and a browser leaves them alone, so `?open=model:meta-llama/Llama-3.1-8B`
 *  and `?on=spark-01,spark-02` stay readable instead of arriving as `%3A`,
 *  `%2F` and `%2C`. `URLSearchParams` would encode all three, which is why
 *  this is hand-rolled; parsing accepts either form.
 *
 *  Unescaping the comma is safe for every other parameter as well: nothing
 *  splits a query value on one except `?on=`, which is the only field whose
 *  value is a list. */
function enc(value: string): string {
  return encodeURIComponent(value)
    .replace(/%3A/g, ':')
    .replace(/%2F/g, '/')
    .replace(/%2C/g, ',')
}

function positive(raw: string | null): number | null {
  if (raw === null) return null
  const n = Number(raw)
  if (!Number.isFinite(n) || n <= 0) return null
  return Math.round(n)
}

/** A degree. Identical to `positive()` and kept separate on purpose.
 *
 *  Both now read absent as absent and every value as itself -- but for
 *  different reasons, and they moved apart once already. `positive()` used to
 *  collapse a value equal to the default to null; that was right while a
 *  missing context meant 8192, and it was always wrong here, because `tp=1`
 *  against a planner that wants `tp=2` is an override and collapsing it would
 *  hand the axis back to the planner it was overruling. Merging them would
 *  make the next change to one of those rules silently a change to both. */
function degree(raw: string | null): number | null {
  if (raw === null) return null
  const n = Number(raw)
  if (!Number.isFinite(n) || n < 1) return null
  return Math.round(n)
}

/** The machines, sorted and deduplicated so `?on=b,a` and `?on=a,b` are one
 *  URL. Sorting matches what the machine board does before it sends
 *  (tabs/models/board.ts `toggle`), for the same reason: two tick orders must
 *  make one request body and one server-side memo key. An empty or all-blank value is no selection rather
 *  than the empty selection, which the gateway answers with a 400. */
function nodeList(raw: string | null): string[] | null {
  if (raw === null) return null
  const ids = [...new Set(raw.split(',').map((s) => s.trim()).filter(Boolean))]
  return ids.length ? ids.sort() : null
}

/** `method:k`. Null unless both halves are there and `k` is a positive integer.
 *
 *  Deliberately strict about the count and deliberately not strict about the
 *  method: an unreadable count has no honest reading (drafting "NaN" tokens is
 *  not a request), while an unknown method is a question only the coordinator
 *  can answer, and it answers it with a sentence naming what this checkpoint
 *  does offer. */
function specRef(raw: string | null, head: string | null): SpecRef | null {
  if (!raw) return null
  const at = raw.indexOf(':')
  if (at <= 0) return null
  const method = raw.slice(0, at).trim()
  const tokens = positive(raw.slice(at + 1))
  if (!method || tokens === null) return null
  const model = head?.trim()
  return model ? { method, tokens, model } : { method, tokens }
}

type DegreeSet = { tp: number | null; pp: number | null; ep: number | null }

const NO_DEGREES: DegreeSet = { tp: null, pp: null, ep: null }

/** Every axis, or none. */
function degreeSet(params: URLSearchParams): DegreeSet {
  const tp = degree(params.get('tp'))
  const pp = degree(params.get('pp'))
  const ep = degree(params.get('ep'))
  if (tp === null && pp === null && ep === null) return NO_DEGREES
  return { tp: tp ?? 1, pp: pp ?? 1, ep: ep ?? 1 }
}

/** `pathname + search`, split and validated. Anything unrecognised -- a typo, a
 *  truncated paste, a link from a newer build -- reads as the dashboard rather
 *  than as an error state, and `RouterProvider` then rewrites the address bar
 *  to say so. */
export function parse(url: string): Route {
  const cut = url.indexOf('?')
  const pathname = cut === -1 ? url : url.slice(0, cut)
  const search = cut === -1 ? '' : url.slice(cut)
  let segments: string[]
  try {
    segments = pathname.split('/').filter(Boolean).map(decodeURIComponent)
  } catch {
    // A malformed escape (`/models/%E0`) throws URIError, and a throw here is
    // a blank app. It is a URL nobody can mean, so it reads as the dashboard
    // like any other unrecognised path.
    return parse('/')
  }
  const legacy = LEGACY_USAGE[segments[0] ?? '']
  if (legacy) segments = ['usage', legacy, ...segments.slice(1)]
  const dest = BY_SEGMENT.get(segments[0] ?? '') ?? 'dash'
  const params = new URLSearchParams(search)

  const model =
    dest === 'models' && segments.length > 1 ? segments.slice(1).join('/') : null
  const sub: UsageSub | null =
    dest === 'usage'
      ? (USAGE_SUBS as readonly string[]).includes(segments[1] ?? '')
        ? (segments[1] as UsageSub)
        : params.get('dep') ? 'rate-limits' : 'overview'
      : null
  // Single slugs, unlike a model id: only the segment after the pane is the
  // subject, and anything past it is a typo to drop.
  const key = sub === 'keys' ? (segments[2] ?? null) : null
  const request = sub === 'requests' ? (segments[2] ?? null) : null
  const onRequests = sub === 'requests'
  const onHistory = onRequests || sub === 'rate-limits'

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
  // Either axis present adopts the pair, filling the other with 1 -- the same
  // first-touch-adopts gesture the field itself makes.
  const degrees = wantsNumbers ? degreeSet(params) : NO_DEGREES
  // A window the overview draws, or null -- which reads as the default,
  // like every other unrecognised parameter.
  const rawWin = params.get('win') ?? ''
  const win =
    sub === 'overview' && (SPEND_WINDOWS as readonly string[]).includes(rawWin)
      ? (rawWin as SpendWindow)
      : onHistory && (REQUEST_WINDOWS as readonly string[]).includes(rawWin)
        ? (rawWin as SpendWindow)
        : null
  const rawStatus = params.get('status')

  return {
    dest,
    sub,
    model,
    key,
    node: params.get('node') || null,
    link: params.get('link') || null,
    dep: params.get('dep') || null,
    sheet,
    context: wantsNumbers ? positive(params.get('ctx')) : null,
    concurrency: wantsNumbers ? positive(params.get('seq')) : null,
    on: wantsNumbers ? nodeList(params.get('on')) : null,
    tp: degrees.tp,
    pp: degrees.pp,
    ep: degrees.ep,
    spec: wantsNumbers ? specRef(params.get('spec'), params.get('head')) : null,
    win,
    request,
    fkey: onRequests ? params.get('key') || null : null,
    fmodel: onHistory ? params.get('model') || null : null,
    fstatus: onRequests && (rawStatus === 'ok' || rawStatus === 'failed' || rawStatus === '429') ? rawStatus : null,
    ftarget: onHistory ? params.get('target') || null : null,
    fprovider: onHistory ? params.get('provider') || null : null,
    fretried: onRequests && params.get('retried') === '1',
  }
}

/** The one canonical spelling of a route. Everything that writes to history
 *  goes through here, so a route that carries a field its destination has no
 *  use for (a model id on `/cluster`, a context on `/usage`) drops it on the
 *  way out rather than accumulating in the address bar. */
export function href(route: Route): string {
  const path = [SEGMENT[route.dest]]
  if (route.dest === 'models' && route.model) {
    path.push(...route.model.split('/').filter(Boolean).map(encodeURIComponent))
  }
  // A `usage` route without a pane is the overview, so the header's
  // `linkTo({dest:'usage'})` works from a screen whose `sub` is null.
  const sub = route.dest === 'usage' ? (route.sub ?? 'overview') : null
  if (sub && sub !== 'overview') {
    path.push(sub)
    if (sub === 'keys' && route.key) path.push(encodeURIComponent(route.key))
    if (sub === 'requests' && route.request) path.push(encodeURIComponent(route.request))
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
    // Written whenever they are set, including at 8192/1. They used to be
    // dropped at the default, because the default was all absence could mean.
    // Absence now means the coordinator picks, so dropping an explicit 8192
    // would silently rewrite "judge everything at 8192" into "judge everything
    // at whatever fits" -- a different question, on a link somebody shared.
    if (route.context) put('ctx', String(route.context))
    if (route.concurrency) put('seq', String(route.concurrency))
    // Sorted on the way out as well as in, so a route built by hand spells the
    // same URL as one that came off the address bar.
    if (route.on && route.on.length) {
      put('on', [...new Set(route.on)].sort().join(','))
    }
    // Written as a set, never singly: `tp` alone would parse back as the whole
    // set {tp, 1, 1} and stop being the route that was written down.
    if (route.tp !== null || route.pp !== null || route.ep !== null) {
      put('tp', String(route.tp ?? 1))
      put('pp', String(route.pp ?? 1))
      put('ep', String(route.ep ?? 1))
    }
    if (route.spec) {
      put('spec', `${route.spec.method}:${route.spec.tokens}`)
      // Only ever beside a `spec`. A head with no method and no count is not a
      // request the fit gate can price, so it is never written on its own --
      // and `specRef` correspondingly ignores it without one.
      if (route.spec.model) put('head', route.spec.model)
    }
  }
  if (sub === 'overview') put('win', route.win)
  if (sub === 'requests' || sub === 'rate-limits') {
    put('win', route.win && (REQUEST_WINDOWS as readonly string[]).includes(route.win) ? route.win : null)
    if (sub === 'requests') put('key', route.fkey)
    put('model', route.fmodel)
    if (sub === 'requests') put('status', route.fstatus)
    put('target', route.ftarget)
    put('provider', route.fprovider)
    if (sub === 'requests' && route.fretried) put('retried', '1')
  }

  return `/${path.join('/')}${query.length ? `?${query.join('&')}` : ''}`
}
