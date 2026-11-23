import { useEffect, useRef, useState, type FormEvent } from 'react'
import { createPortal } from 'react-dom'
import type { KeyCreate as KeyCreateSpec, RoutingPolicy, ServedModel } from '../../api/types'
import { KEY_EXPIRY_DAYS } from '../../api/types'
import { containTab } from '../../components/focusTrap'
import { Icon } from '../../components/Icon'
import { Disclosure } from '../../components/Panel'
import { PasswordField } from '../../components/PasswordField'
import { RevealModal } from '../../components/RevealModal'
import { Verbatim } from '../../components/Verbatim'
import { useBackend } from '../../state/backend'
import { useRouter } from '../../state/router'
import { LimitsEditor } from './LimitsEditor'
import { ROUTING_POLICIES } from './rows'

/** Mint a key. A "New key" button -- it sits in the API keys card's heading --
 *  opens the form as a modal over the page; Escape, the scrim and Cancel close
 *  it and drop what was typed. On success the form gives way to the
 *  RevealModal: the secret is held in state only while that is up, and is
 *  dropped on "I saved it" before navigating to the new key's page. Server
 *  refusals render verbatim under the form. */
export function KeyCreate({ models }: { models: ServedModel[] }) {
  const { backend, invalidate } = useBackend()
  const { navigate } = useRouter()
  const [open, setOpen] = useState(false)
  const [routingOpen, setRoutingOpen] = useState(false)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const cardRef = useRef<HTMLDivElement>(null)
  const nameRef = useRef<HTMLInputElement>(null)

  const [name, setName] = useState('')
  const [nameErr, setNameErr] = useState<string | null>(null)
  const [expiry, setExpiry] = useState<number | null>(null)
  const [limits, setLimits] = useState<Record<string, number>>({})
  const [inherit, setInherit] = useState(true)
  const [allowed, setAllowed] = useState<string[]>([])
  const [policy, setPolicy] = useState<RoutingPolicy | ''>('')
  const [fallback, setFallback] = useState('')
  const [notes, setNotes] = useState('')
  /** Minting re-authenticates: sent as `current_password`, cleared with the draft. */
  const [currentPassword, setCurrentPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [minted, setMinted] = useState<{
    secret: string
    keyId: string
  } | null>(null)

  const expiryDays = expiry ?? 30
  const localModels = models.filter((m) => m.target_kinds.includes('local'))

  const reset = () => {
    setName('')
    setNameErr(null)
    setExpiry(null)
    setLimits({})
    setInherit(true)
    setAllowed([])
    setPolicy('')
    setFallback('')
    setNotes('')
    setCurrentPassword('')
    setError(null)
    setRoutingOpen(false)
  }

  const cancel = () => {
    if (busy) return
    reset()
    setOpen(false)
  }

  // The form is the modal while it is up: page scroll locked, focus on the
  // name field, Tab held inside, Escape closes, focus back on the trigger
  // after. Not while the reveal is up -- RevealModal owns all of that then,
  // and deliberately ignores Escape.
  const formUp = open && !minted
  useEffect(() => {
    if (!formUp) return
    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    nameRef.current?.focus()
    const trigger = triggerRef.current
    return () => {
      document.body.style.overflow = prevOverflow
      trigger?.focus()
    }
  }, [formUp])

  useEffect(() => {
    if (!formUp) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        cancel()
        return
      }
      containTab(e, cardRef.current)
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  })

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (!name.trim()) {
      setNameErr('Name the key so its rows can be told apart.')
      return
    }
    setNameErr(null)
    setBusy(true)
    setError(null)
    const spec: KeyCreateSpec = {
      name: name.trim(),
      expiry_days: expiryDays,
      limits,
      allowed_models: inherit ? null : allowed,
      routing_policy: policy || null,
      notes,
      cloud_fallback_local_model: fallback || null,
      current_password: currentPassword,
    }
    try {
      const res = await backend.createKey(spec)
      setMinted({ secret: res.secret_once, keyId: res.key.key_id })
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const done = () => {
    const id = minted?.keyId
    // Dropped before anything else happens: once this state is null nothing
    // in the app holds the secret.
    setMinted(null)
    reset()
    setOpen(false)
    invalidate()
    if (id) navigate({ dest: 'usage', sub: 'keys', key: id })
  }

  const toggleAllowed = (id: string) =>
    setAllowed((cur) => (cur.includes(id) ? cur.filter((m) => m !== id) : [...cur, id]))

  return (
    <>
      <button ref={triggerRef} type="button" aria-haspopup="dialog" onClick={() => setOpen(true)}>
        <Icon name="plus" size={12} decorative /> New key
      </button>
      {formUp
        ? createPortal(
            <div
              className="sheet on"
              role="presentation"
              onClick={(e) => {
                if (e.target === e.currentTarget) cancel()
              }}
            >
              <div
                ref={cardRef}
                className="card wide"
                role="dialog"
                aria-modal="true"
                aria-labelledby="kc-title"
                tabIndex={-1}
              >
                <div
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'baseline',
                    gap: 'var(--space-8)',
                  }}
                >
                  <h2 id="kc-title">New API key</h2>
                  <button type="button" onClick={cancel} disabled={busy}>
                    Close
                  </button>
                </div>
                <form onSubmit={(e) => void submit(e)} noValidate>
                  <div
                    style={{
                      display: 'flex',
                      gap: 'var(--space-10)',
                      flexWrap: 'wrap',
                      alignItems: 'flex-start',
                    }}
                  >
                    <div className="fld">
                      <label htmlFor="kc-name">Name</label>
                      <input
                        ref={nameRef}
                        id="kc-name"
                        value={name}
                        aria-invalid={nameErr ? true : undefined}
                        aria-describedby={nameErr ? 'kc-name-err' : undefined}
                        onChange={(e) => setName(e.target.value)}
                        onBlur={() => setNameErr(name.trim() ? null : nameErr)}
                      />
                      {nameErr ? (
                        <span id="kc-name-err" className="unit" style={{ color: 'var(--fault)' }}>
                          {nameErr}
                        </span>
                      ) : null}
                    </div>
                    <div className="fld">
                      <label htmlFor="kc-expiry">Expires after</label>
                      <select id="kc-expiry" value={expiryDays} onChange={(e) => setExpiry(Number(e.target.value))}>
                        {KEY_EXPIRY_DAYS.map((d) => (
                          <option key={d} value={d}>
                            {d === 1 ? '1 day' : `${d} days`}
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>

                  <h4 className="label" style={{ marginTop: 'var(--space-12)' }}>
                    Limits
                  </h4>
                  <LimitsEditor value={limits} onChange={setLimits} models={models} idPrefix="kc" />

                  <div style={{ marginTop: 'var(--space-12)' }}>
                    <Disclosure
                      summary="Models & routing"
                      open={routingOpen}
                      onToggle={() => setRoutingOpen((o) => !o)}
                    >
                      <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
                        <legend className="label">Allowed models</legend>
                        <label className="sw">
                          <input type="radio" name="kc-allow" checked={inherit} onChange={() => setInherit(true)} />
                          <span>Every served model</span>
                        </label>
                        <label className="sw">
                          <input type="radio" name="kc-allow" checked={!inherit} onChange={() => setInherit(false)} />
                          <span>Only these</span>
                        </label>
                        {!inherit ? (
                          <div style={{ marginLeft: 'var(--space-24)' }}>
                            {models.length === 0 ? (
                              <div className="unit">Nothing is served right now.</div>
                            ) : (
                              models.map((m) => (
                                <label className="sw" key={m.id}>
                                  <input
                                    type="checkbox"
                                    checked={allowed.includes(m.id)}
                                    onChange={() => toggleAllowed(m.id)}
                                  />
                                  <span className="mono">{m.id}</span>
                                </label>
                              ))
                            )}
                          </div>
                        ) : null}
                      </fieldset>
                      <div
                        style={{
                          display: 'flex',
                          gap: 'var(--space-10)',
                          flexWrap: 'wrap',
                          marginTop: 'var(--space-8)',
                        }}
                      >
                        <div className="fld">
                          <label htmlFor="kc-policy">Routing policy</label>
                          <select
                            id="kc-policy"
                            value={policy}
                            onChange={(e) => setPolicy(e.target.value as RoutingPolicy | '')}
                          >
                            <option value="">inherit</option>
                            {ROUTING_POLICIES.map((p) => (
                              <option key={p} value={p}>
                                {p}
                              </option>
                            ))}
                          </select>
                        </div>
                        <div className="fld">
                          <label htmlFor="kc-fallback">Cloud fallback model</label>
                          <select id="kc-fallback" value={fallback} onChange={(e) => setFallback(e.target.value)}>
                            <option value="">none</option>
                            {localModels.map((m) => (
                              <option key={m.id} value={m.id}>
                                {m.id}
                              </option>
                            ))}
                          </select>
                          <span className="unit">Served locally once a cloud limit is reached.</span>
                        </div>
                      </div>
                    </Disclosure>
                  </div>

                  <div className="fld" style={{ marginTop: 'var(--space-12)' }}>
                    <label htmlFor="kc-notes">Notes</label>
                    <textarea id="kc-notes" value={notes} onChange={(e) => setNotes(e.target.value)} />
                  </div>

                  <div className="fld" style={{ marginTop: 'var(--space-12)' }}>
                    <label htmlFor="kc-current-pw">Your password</label>
                    <PasswordField
                      id="kc-current-pw"
                      value={currentPassword}
                      autoComplete="current-password"
                      onChange={setCurrentPassword}
                    />
                    <span className="unit">Minting a key is an admin action: confirm it is you.</span>
                  </div>

                  <div
                    style={{
                      marginTop: 'var(--space-12)',
                      display: 'flex',
                      gap: 'var(--space-8)',
                    }}
                  >
                    <button type="submit" disabled={busy || !currentPassword}>
                      {busy ? 'Creating…' : 'Create key'}
                    </button>
                    <button type="button" className="ghost" onClick={cancel} disabled={busy}>
                      Cancel
                    </button>
                  </div>
                  {error ? (
                    <div
                      style={{
                        color: 'var(--fault)',
                        marginTop: 'var(--space-8)',
                      }}
                    >
                      <Verbatim text={error} />
                    </div>
                  ) : null}
                </form>
              </div>
            </div>,
            document.body,
          )
        : null}
      {minted ? <RevealModal secret={minted.secret} keyId={minted.keyId} onDone={done} /> : null}
    </>
  )
}
