import { useState } from 'react'
import { useBackend } from '../../state/backend'
import {
  describeBase,
  normalizeBase,
  probeCoordinator,
  setCoordinatorBase,
  type ProbeResult,
} from '../../api/origin'

/** Settings -> Coordinator: which gateway this browser tab talks to, and a
 *  button that finds out whether it answers.
 *
 *  `ClusterCard` next door drops a "Gateway" row on the grounds that the
 *  coordinator does not report its own address and `window.location.host` is
 *  a deployment convention that lies under `npm run dev`. Both halves of that
 *  are still true, and neither applies here: this is not a coordinator-reported
 *  fact being guessed at, it is the operator's own instruction about where to
 *  send requests, and the only place it can be stated. It lives in
 *  localStorage, per browser, and is not part of `/api/settings` -- a setting
 *  you have to already be connected to read cannot be the one that says how
 *  to connect.
 *
 *  Test and Save are separate on purpose. Testing probes the address in the
 *  field without adopting it, so a typo is an error message rather than an app
 *  that has just pointed itself at nothing and cannot load the page you would
 *  fix it on.
 */
export function CoordinatorCard() {
  // `origin` rather than calling `coordinatorBase()` here: the same value, but
  // it arrives through the store subscription, so the "In use" row re-renders
  // with the rest of the app instead of being whatever it read on the render
  // that happened to mount this card.
  const { invalidate, origin: saved } = useBackend()

  const [draft, setDraft] = useState(saved)
  const [testing, setTesting] = useState(false)
  const [result, setResult] = useState<ProbeResult | null>(null)
  const [savedNote, setSavedNote] = useState<string | null>(null)

  // `null` means the field is not a URL. Derived on every keystroke rather than
  // latched on submit, so the complaint appears while there is still a cursor
  // in the field -- and so Save can simply be unavailable rather than being a
  // button that silently does nothing.
  const normalized = normalizeBase(draft)
  const malformed = normalized === null
  const dirty = !malformed && normalized !== saved

  const test = async () => {
    if (normalized === null) return
    setSavedNote(null)
    setTesting(true)
    setResult(null)
    try {
      setResult(await probeCoordinator(normalized))
    } finally {
      setTesting(false)
    }
  }

  const apply = () => {
    if (normalized === null) return
    setCoordinatorBase(normalized)
    // Every poll and the metrics stream re-establish off the provider's new
    // backend identity; this only spares them the rest of their interval.
    invalidate()
    setSavedNote(
      normalized
        ? `Now reading from ${normalized}. Every panel in this UI has been repointed.`
        : 'Back to this page’s own origin.',
    )
  }

  const useThisPage = () => {
    setDraft('')
    setResult(null)
    setCoordinatorBase('')
    invalidate()
    setSavedNote('Back to this page’s own origin.')
  }

  return (
    <div className="card2">
      <h3>Coordinator</h3>
      <div className="unit" style={{ marginBottom: 10 }}>
        Where this browser sends <span className="mono">/api</span> and{' '}
        <span className="mono">/v1</span>. Leave it blank in a normal install — the
        coordinator serves this page, so its own origin is the right answer. Set it to
        look at a coordinator somewhere else.
      </div>

      <div className="row">
        <span>In use</span>
        <span className="mono">{describeBase(saved)}</span>
      </div>

      <div
        style={{
          display: 'flex',
          gap: 8,
          alignItems: 'flex-end',
          marginTop: 12,
          flexWrap: 'wrap',
        }}
      >
        <div className="fld" style={{ flex: 1, minWidth: 240 }}>
          <label htmlFor="coord-base">Base URL</label>
          <input
            id="coord-base"
            value={draft}
            onChange={(e) => {
              setDraft(e.target.value)
              setResult(null)
              setSavedNote(null)
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void test()
            }}
            placeholder="blank for this page's origin — or http://192.168.1.20:8080"
            spellCheck={false}
            autoComplete="off"
          />
        </div>
        <button onClick={() => void test()} disabled={testing || malformed}>
          {testing ? 'Testing…' : 'Test connection'}
        </button>
        <button onClick={apply} disabled={!dirty}>
          {saved && normalized === '' ? 'Use this page' : 'Save'}
        </button>
        {saved ? (
          <button onClick={useThisPage} style={{ padding: '5px 9px' }}>
            Reset
          </button>
        ) : null}
      </div>

      {malformed ? (
        <div className="label" style={{ color: 'var(--fault)', marginTop: 8 }}>
          That is not an address. Expected something like{' '}
          <span className="mono">192.168.1.20:8080</span> or{' '}
          <span className="mono">https://coordinator.lan</span>.
        </div>
      ) : null}

      {result ? (
        <div
          className="label"
          style={{
            marginTop: 8,
            color: result.ok ? 'var(--live)' : 'var(--fault)',
            whiteSpace: 'pre-wrap',
          }}
        >
          {result.ok ? '✓ ' : '✕ '}
          {describeBase(result.base)} — {result.detail}
          {result.ok && result.base !== saved ? ' Press Save to use it.' : ''}
        </div>
      ) : null}

      {savedNote ? (
        <div className="label" style={{ marginTop: 8 }}>
          {savedNote}
        </div>
      ) : null}

      <div className="unit" style={{ marginTop: 10 }}>
        A coordinator on another origin has to allow this page before a browser will
        let the requests through. Start it with{' '}
        <span className="mono">DERATE_ALLOWED_ORIGINS={window.location.origin}</span>;
        without that, every request here fails with no detail, which is the browser
        withholding it rather than the coordinator being down.
      </div>
    </div>
  )
}
