import { useState } from 'react'
import type { ApiKeyRow } from '../../api/types'
import { PasswordField } from '../../components/PasswordField'
import { RevealModal } from '../../components/RevealModal'
import { Verbatim } from '../../components/Verbatim'
import { useBackend } from '../../state/backend'

type Action = 'disable' | 'enable' | 'rotate' | 'delete' | 'raise' | 'reset'

const CONFIRM: Record<Action, { button: string; busy: string; sentence: string }> = {
  disable: {
    button: 'Disable',
    busy: 'Disabling…',
    sentence: 'Requests with this key are refused with api_key_disabled until it is enabled again.',
  },
  enable: { button: 'Enable', busy: 'Enabling…', sentence: 'The key is accepted again from the next request.' },
  rotate: {
    button: 'Rotate',
    busy: 'Rotating…',
    sentence: 'The current secret stops working at once; the new one is shown once.',
  },
  delete: {
    button: 'Delete',
    busy: 'Deleting…',
    sentence: 'The key stops working for good. Its usage history stays on this page.',
  },
  raise: {
    button: 'Raise limit',
    busy: 'Raising…',
    sentence: 'The raise lasts until this cycle ends; the counter is not reset.',
  },
  reset: {
    button: 'Reset cycle',
    busy: 'Resetting…',
    sentence: "This cycle's counters go to zero. The cycle's end date does not move.",
  },
}

/** Every mutation a key has, each behind a confirm step that says what it
 *  does. Admin keys can be rotated but not deleted here. */
export function KeyActions({ row }: { row: ApiKeyRow }) {
  const { backend, invalidate } = useBackend()
  const [pending, setPending] = useState<Action | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [reason, setReason] = useState('')
  /** An admin key's rotate re-checks the signed-in admin's password. */
  const [currentPassword, setCurrentPassword] = useState('')
  const needsPassword = row.is_admin_key
  const [raiseType, setRaiseType] = useState('')
  const [raiseValue, setRaiseValue] = useState('')
  const limitTypes = [
    ...new Set([
      ...Object.keys(row.limits),
      ...(row.limit_states ?? []).filter((s) => s.scope === 'key').map((s) => s.limit_type),
    ]),
  ]
  const [raiseErr, setRaiseErr] = useState<string | null>(null)
  const [minted, setMinted] = useState<{ secret: string; keyId: string } | null>(null)

  const deleted = row.deleted_at != null
  const available: Action[] = deleted
    ? []
    : [
        ...(row.status === 'disabled' ? (['enable'] as const) : row.status === 'active' ? (['disable'] as const) : []),
        'rotate',
        ...(limitTypes.length ? (['raise'] as const) : []),
        'reset',
        ...(row.is_admin_key ? [] : (['delete'] as const)),
      ]

  const choose = (a: Action) => {
    setPending(pending === a ? null : a)
    setError(null)
    setRaiseErr(null)
  }

  const run = async () => {
    if (!pending) return
    const id = row.key_id
    let value = 0
    const type = raiseType || limitTypes[0] || ''
    if (pending === 'raise') {
      value = Number(raiseValue)
      if (raiseValue.trim() === '' || !Number.isFinite(value) || value < 0) {
        setRaiseErr('Enter the limit for the rest of this cycle.')
        return
      }
    }
    setBusy(true)
    setError(null)
    try {
      if (pending === 'disable') await backend.disableKey(id, reason.trim() || undefined)
      else if (pending === 'enable') await backend.enableKey(id)
      else if (pending === 'delete') await backend.deleteKey(id)
      else if (pending === 'reset') await backend.resetCycle(id)
      else if (pending === 'raise') await backend.raiseLimit(id, type, value)
      else if (pending === 'rotate') {
        const res = await backend.rotateKey(id, needsPassword ? currentPassword : undefined)
        setMinted({ secret: res.secret_once, keyId: res.key.key_id })
      }
      setPending(null)
      setReason('')
      setCurrentPassword('')
      setRaiseValue('')
      invalidate()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  if (deleted) {
    return <div className="unit">This key was deleted. Its stats stay readable below.</div>
  }

  return (
    <div>
      <div style={{ display: 'flex', gap: 'var(--space-8)', flexWrap: 'wrap' }}>
        {available.map((a) => (
          <button
            type="button"
            key={a}
            className={pending === a ? undefined : 'ghost'}
            aria-expanded={pending === a}
            onClick={() => choose(a)}
          >
            {CONFIRM[a].button}
          </button>
        ))}
      </div>
      {pending ? (
        <div className="card2" style={{ marginTop: 'var(--space-8)' }}>
          <div className="unit">{CONFIRM[pending].sentence}</div>
          {pending === 'disable' ? (
            <div className="fld" style={{ marginTop: 'var(--space-8)' }}>
              <label htmlFor="ka-reason">Reason (optional)</label>
              <input id="ka-reason" value={reason} onChange={(e) => setReason(e.target.value)} />
            </div>
          ) : null}
          {pending === 'rotate' && needsPassword ? (
            <div className="fld" style={{ marginTop: 'var(--space-8)' }}>
              <label htmlFor="ka-current-pw">Your password</label>
              <PasswordField
                id="ka-current-pw"
                value={currentPassword}
                autoComplete="current-password"
                onChange={setCurrentPassword}
              />
              <span className="unit">An admin key is an admin&rsquo;s credential: confirm it is you.</span>
            </div>
          ) : null}
          {pending === 'raise' ? (
            <div style={{ display: 'flex', gap: 'var(--space-10)', flexWrap: 'wrap', marginTop: 'var(--space-8)' }}>
              <div className="fld">
                <label htmlFor="ka-rtype">Limit</label>
                <select id="ka-rtype" value={raiseType || limitTypes[0]} onChange={(e) => setRaiseType(e.target.value)}>
                  {limitTypes.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
              </div>
              <div className="fld">
                <label htmlFor="ka-rvalue">New value this cycle</label>
                <input
                  id="ka-rvalue"
                  className="mono"
                  inputMode="decimal"
                  value={raiseValue}
                  aria-invalid={raiseErr ? true : undefined}
                  aria-describedby={raiseErr ? 'ka-rvalue-err' : undefined}
                  onChange={(e) => setRaiseValue(e.target.value)}
                />
                {raiseErr ? (
                  <span id="ka-rvalue-err" className="unit" style={{ color: 'var(--fault)' }}>
                    {raiseErr}
                  </span>
                ) : null}
              </div>
            </div>
          ) : null}
          <div style={{ display: 'flex', gap: 'var(--space-8)', marginTop: 'var(--space-8)' }}>
            <button
              type="button"
              disabled={busy || (pending === 'rotate' && needsPassword && !currentPassword)}
              onClick={() => void run()}
            >
              {busy ? CONFIRM[pending].busy : `${CONFIRM[pending].button} ${row.name}`}
            </button>
            <button type="button" className="ghost" disabled={busy} onClick={() => setPending(null)}>
              Cancel
            </button>
          </div>
          {error ? (
            <div style={{ color: 'var(--fault)', marginTop: 'var(--space-8)' }}>
              <Verbatim text={error} />
            </div>
          ) : null}
        </div>
      ) : null}
      {minted ? (
        <RevealModal secret={minted.secret} keyId={minted.keyId} onDone={() => setMinted(null)} />
      ) : null}
    </div>
  )
}
