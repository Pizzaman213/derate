import { useState } from 'react'
import { useBackend } from '../../state/backend'
import { apiToken, setApiToken } from '../../api/apiToken'
import { apiUrl } from '../../api/origin'

/** Settings -> API token: the credential this browser tab sends on every
 *  `/api` request, for the coordinator that was started with
 *  `DERATE_API_TOKEN` set (`gateway/auth.py`).
 *
 *  Unset is the ordinary case -- `/api` has no authentication by default, and
 *  this field does nothing on a coordinator that was never given a token to
 *  check. It exists for the operator who did set one: without somewhere to
 *  put it, every panel in this UI would start reading 401 the moment the
 *  coordinator came up requiring a credential nothing here could offer.
 *
 *  Lives in localStorage, per browser, same as `CoordinatorCard`'s base URL
 *  and for the same reason: it is what this tab uses to reach the
 *  coordinator, so it cannot be a setting read through that connection. */
export function ApiTokenCard() {
  const { invalidate } = useBackend()
  const [draft, setDraft] = useState(apiToken())
  const [verifying, setVerifying] = useState(false)
  const [result, setResult] = useState<{ ok: boolean; detail: string } | null>(null)

  const saved = apiToken()
  const dirty = draft.trim() !== saved

  const verify = async () => {
    setVerifying(true)
    setResult(null)
    try {
      const res = await fetch(apiUrl('/api/providers'), {
        headers: draft.trim() ? { authorization: `Bearer ${draft.trim()}` } : {},
      })
      setResult(
        res.ok
          ? { ok: true, detail: 'Accepted.' }
          : res.status === 401
            ? { ok: false, detail: 'Refused: this token does not match the coordinator.' }
            : { ok: false, detail: `Answered ${res.status} ${res.statusText}.` },
      )
    } catch {
      setResult({ ok: false, detail: 'The browser could not reach the coordinator.' })
    } finally {
      setVerifying(false)
    }
  }

  const save = () => {
    setApiToken(draft)
    invalidate()
    setResult(null)
  }

  return (
    <div className="card2">
      <h3>API token</h3>
      <div className="unit" style={{ marginBottom: 10 }}>
        Sent as <span className="mono">Authorization: Bearer …</span> on every{' '}
        <span className="mono">/api</span> request. Leave it blank unless this coordinator
        was started with <span className="mono">DERATE_API_TOKEN</span> set — most are not,
        and <span className="mono">/api</span> has no authentication by default.
      </div>

      <div
        style={{
          display: 'flex',
          gap: 8,
          alignItems: 'flex-end',
          flexWrap: 'wrap',
        }}
      >
        <div className="fld" style={{ flex: 1, minWidth: 240 }}>
          <label htmlFor="api-token">Token</label>
          <input
            id="api-token"
            type="password"
            autoComplete="off"
            value={draft}
            onChange={(e) => {
              setDraft(e.target.value)
              setResult(null)
            }}
            placeholder="blank — this coordinator needs none"
            spellCheck={false}
          />
        </div>
        <button onClick={() => void verify()} disabled={verifying}>
          {verifying ? 'Checking…' : 'Verify'}
        </button>
        <button onClick={save} disabled={!dirty}>
          Save
        </button>
      </div>

      {result ? (
        <div
          className="label"
          style={{ marginTop: 8, color: result.ok ? 'var(--live)' : 'var(--fault)' }}
        >
          {result.ok ? '✓ ' : '✕ '}
          {result.detail}
        </div>
      ) : null}
    </div>
  )
}
