// The browser gallery: every gallery state mounted live, one <section> each,
// under the product's own stylesheets. Opened by the component and layout
// checks; `npm run ui:check` builds it into tests/ui/.build/.
//
// URL parameters:
//   ?theme=light|dark|high-contrast   sets data-theme (default: system)
//   ?stress=long|url|rtl|emoji|empty  text stress applied to sample strings
//   ?zoom=2                           CSS zoom on the root (200%)
//   ?font=large                       every --size-* token × 1.25 at runtime
//   ?only=<Component>                 mount one component's states only
//   ?variant=<kind>                   with only=Sheet: open that sheet kind;
//                                     with only=RevealModal: mount that state
//
// The Sheet is provider-bound and reads the coordinator, so the gallery
// installs a fake `fetch` that answers from fixtures/api.json (recorded from a
// real coordinator) and a fake `EventSource` that stays silent. Nothing here is
// imported by the product.

import { StrictMode, useEffect, useState, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource/ibm-plex-sans/400.css'
import '@fontsource/ibm-plex-sans/500.css'
import '@fontsource/ibm-plex-mono/400.css'
import '@fontsource/ibm-plex-mono/500.css'
import '../../../design/tokens/tokens.css'
import '../../../../ui/src/styles/base.css'
import '../../../../ui/src/styles/derate.css'
import '../../../../ui/src/tabs/setup/setup.css'
import { tokens } from '../../../design/tokens/tokens.ts'
import { RouterProvider } from '../../../../ui/src/state/router'
import { BackendProvider } from '../../../../ui/src/state/backend'
import { MetricsProvider } from '../../../../ui/src/state/metrics'
import { TelemetryProvider } from '../../../../ui/src/state/telemetry'
import { SelectionProvider, useSelection, type SheetTarget } from '../../../../ui/src/state/selection'
import { Sheet } from '../../../../ui/src/shell/Sheet'
import { RevealModal } from '../../../../ui/src/components/RevealModal'
import { ProviderSignIn } from '../../../../ui/src/tabs/settings/ProviderSignIn'
import type { ProviderSignInFlow } from '../../../../ui/src/api/types'
import { STATES, REVEAL_FIXTURES } from './states'
import fixtures from './fixtures/api.json'
import type { Stress } from './stress'

declare global {
  // eslint-disable-next-line no-var
  var __derateGalleryStress: Stress | undefined
}

const params = new URLSearchParams(location.search)
const theme = params.get('theme')
const stress = (params.get('stress') ?? 'none') as Stress
const only = params.get('only')
const variant = params.get('variant')

globalThis.__derateGalleryStress = stress
if (theme) document.documentElement.setAttribute('data-theme', theme)
if (params.get('zoom')) document.documentElement.style.setProperty('zoom', params.get('zoom')!)
if (params.get('font') === 'large') {
  for (const [name, value] of Object.entries(tokens.static)) {
    if (name.startsWith('size-') && typeof value === 'string' && value.endsWith('px')) document.documentElement.style.setProperty(`--${name}`, `${parseFloat(value) * 1.25}px`)
  }
}

// ---- fake coordinator
const responses = fixtures.responses as Record<string, { status: number; body: unknown }>
const realFetch = window.fetch.bind(window)
window.fetch = (input, init) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const u = new URL(url, location.href)
  if (!/^\/(api|v1)\//.test(u.pathname)) return realFetch(input, init)
  const hit = responses[u.pathname + u.search] ?? responses[u.pathname]
  const status = hit ? hit.status : 404
  const body = hit ? hit.body : { detail: `gallery: no fixture for ${u.pathname}` }
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } }))
}
class SilentEventSource extends EventTarget {
  readyState = 1
  url: string
  constructor(url: string) { super(); this.url = url }
  close() { this.readyState = 2 }
  onopen: null | (() => void) = null
  onmessage: null | (() => void) = null
  onerror: null | (() => void) = null
}
Object.defineProperty(window, 'EventSource', { value: SilentEventSource, writable: true })

// ---- states
const signInController = only === 'ProviderSignIn' && variant === 'controller'
const chosen = STATES.filter((s) => !signInController && (!only || s.component === only))

function Opener({ target }: { target: SheetTarget }) {
  const { openSheet } = useSelection()
  // One tick late: child effects run before the RouterProvider's own mount
  // effect, which canonicalises the URL and would overwrite the push.
  useEffect(() => { const t = setTimeout(() => openSheet(target), 0); return () => clearTimeout(t) }, [])
  return null
}
const SHEET_TARGETS: Record<string, SheetTarget> = {
  node: { kind: 'node', id: fixtures.node },
  dep: { kind: 'dep', id: 'nothing-here' },
  model: { kind: 'model', id: 'Qwen/Qwen3-30B-A3B', context: 32768, concurrency: 8 },
}

// The reveal is a body portal that covers the page, so it is mounted only on
// request. onDone unmounts it and counts the call on <body data-reveal-done>,
// which is how the check sees the handler fire.
function Reveal({ state }: { state: keyof typeof REVEAL_FIXTURES }) {
  const [open, setOpen] = useState(true)
  const f = REVEAL_FIXTURES[state]
  if (!open) return null
  return <RevealModal secret={f.secret} keyId={f.keyId} onDone={() => { document.body.dataset.revealDone = String(Number(document.body.dataset.revealDone ?? 0) + 1); setOpen(false) }} />
}

// Real controller with a replaceable HTTP boundary; never imported by the app.
function SignInControllerFixture() {
  const [flow, setFlow] = useState<ProviderSignInFlow>('chatgpt')
  const [provider, setProvider] = useState('new')
  const [open, setOpen] = useState(true)
  const [done, setDone] = useState(0)
  const [cancelled, setCancelled] = useState(0)
  return <section data-gallery="" data-sign-in-controller="" data-done={done} data-cancelled={cancelled}>
    <label htmlFor="fixture-flow">Flow</label>
    <select id="fixture-flow" value={flow} onChange={(e) => setFlow(e.target.value as ProviderSignInFlow)}>
      <option value="chatgpt">ChatGPT</option><option value="claude">Claude</option>
    </select>
    <label htmlFor="fixture-provider">Provider</label>
    <select id="fixture-provider" value={provider} onChange={(e) => setProvider(e.target.value)}>
      <option value="new">New provider</option><option value="existing-a">Existing A</option><option value="existing-b">Existing B</option>
    </select>
    {open ? <ProviderSignIn flow={flow} providerId={provider === 'new' ? undefined : provider}
      onDone={() => { setDone((n) => n + 1); setOpen(false) }}
      onCancel={() => { setCancelled((n) => n + 1); setOpen(false) }} />
      : <button type="button" onClick={() => setOpen(true)}>Open sign-in</button>}
  </section>
}

function Gallery() {
  return (
    <div style={{ padding: 'var(--space-12)', display: 'grid', gap: 'var(--space-12)' }}>
      {signInController ? <SignInControllerFixture /> : null}
      {chosen.map((s, i) => (
        <section
          key={i}
          data-gallery=""
          data-component={s.component}
          data-variant={s.variant}
          data-state={s.state}
          data-interactive={s.interactive ? 'true' : undefined}
          data-clicks="0"
          onClickCapture={(e) => {
            const btn = (e.target as HTMLElement).closest('button')
            if (btn) { const sec = e.currentTarget; sec.dataset.clicks = String(Number(sec.dataset.clicks ?? 0) + 1) }
          }}
          style={{ border: '1px solid var(--rule)', borderRadius: 'var(--radius)', padding: 'var(--space-12)', maxWidth: '100%', minWidth: 0 }}
        >
          <h2 className="label" style={{ margin: '0 0 var(--space-8)' }}>{s.component} · {s.variant} · {s.state}</h2>
          <div data-gallery-body="" style={{ minWidth: 0 }}>{s.render()}</div>
        </section>
      ))}
      {chosen.length === 0 && !signInController ? <p className="unit">no states for ?only={only}</p> : null}
    </div>
  )
}

function Providers({ children }: { children: ReactNode }) {
  return (
    <RouterProvider>
      <BackendProvider>
        <MetricsProvider>
          <TelemetryProvider>
            <SelectionProvider>
              {children}
              <Sheet />
              {only === 'Sheet' && variant && SHEET_TARGETS[variant] ? <Opener target={SHEET_TARGETS[variant]} /> : null}
              {only === 'RevealModal' && variant && variant in REVEAL_FIXTURES ? <Reveal state={variant as keyof typeof REVEAL_FIXTURES} /> : null}
            </SelectionProvider>
          </TelemetryProvider>
        </MetricsProvider>
      </BackendProvider>
    </RouterProvider>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Providers>
      <Gallery />
    </Providers>
  </StrictMode>,
)
