import { useCallback, useEffect, useId, useRef, useState } from 'react'
import type { ProviderSignInFlow, ProviderSignInStart } from '../../api/types'
import { ApiError } from '../../api/client'
import { useBackend } from '../../state/backend'
import { Icon } from '../../components/Icon'

export type ProviderSignInStep = 'start' | 'paste' | 'done'

export type ProviderSignInProps = {
  flow: ProviderSignInFlow
  /** Set to sign an existing provider in again; absent for a new provider. */
  providerId?: string
  onDone: () => void
  onCancel: () => void
}

export type ProviderSignInViewProps = {
  flow: ProviderSignInFlow
  step: ProviderSignInStep
  reauth: boolean
  authorizeUrl: string | null
  callbackUrl: string
  onCallbackUrl: (value: string) => void
  busy: boolean
  /** The coordinator's own sentence, rendered verbatim. */
  error: string | null
  onContinue: () => void
  onFinish: () => void
  onDone: () => void
  onCancel: () => void
}

const USAGE_URL = 'https://chatgpt.com/settings/usage'
const CLAUDE_POLICY_URL = 'https://code.claude.com/docs/en/legal-and-compliance'

/** Pure render shared by the application and the design gallery. */
export function ProviderSignInView({
  flow, step, reauth, authorizeUrl, callbackUrl, onCallbackUrl, busy, error,
  onContinue, onFinish, onDone, onCancel,
}: ProviderSignInViewProps) {
  const inputId = useId()
  const claude = flow === 'claude'
  const name = claude ? 'Claude' : 'ChatGPT'
  const row = { display: 'flex', gap: 'var(--space-8)', alignItems: 'center', flexWrap: 'wrap' } as const
  const link = {
    display: 'inline-flex', alignItems: 'center', gap: 'var(--space-4)',
    minHeight: 'var(--space-24)', color: 'inherit', textDecoration: 'underline',
  } as const
  return (
    <div
      role="group"
      aria-label={reauth ? `Sign in to ${name} again` : `Sign in with ${name}`}
      style={{ display: 'grid', gap: 'var(--space-8)' }}
    >
      {claude && (step === 'start' || step === 'done') ? (
        <>
          <div className="unit">
            Unofficial connection. Anthropic does not permit third-party routing with Claude subscription credentials and may restrict your account.
            {' '}<a href={CLAUDE_POLICY_URL} target="_blank" rel="noopener noreferrer" style={link}>
              Anthropic policy<Icon name="external-link" size={12} decorative />
            </a>
          </div>
          <div className="unit">
            derate will not fall back to an API key. Account-level extra usage may still be billed by Anthropic.
            This unofficial connection uses compatibility headers and an agent identity prefix; derate is not Anthropic’s CLI.
          </div>
        </>
      ) : null}
      {step === 'start' ? (
        <>
          <div className="unit">
            {reauth
              ? `Sign in with the same ${name} account this provider uses.`
              : claude
                ? 'Sign in with your Claude subscription. Tokens stay on the coordinator, not in this browser.'
                : 'Sign in with your ChatGPT account and this provider serves your plan’s models. No key is pasted or stored.'}
          </div>
          <div style={row}>
            <button type="button" onClick={onContinue} disabled={busy}>
              {busy ? 'Opening…' : `Continue with ${name}`}
            </button>
            <button type="button" className="ghost" onClick={onCancel}>Cancel</button>
          </div>
        </>
      ) : step === 'paste' ? (
        <>
          <div className="unit">
            {claude
              ? 'Approve the connection in the tab that opened. Paste the authorization code (code#state) or the full callback URL here. No loopback port is needed.'
              : 'Approve derate in the tab that opened. If that tab then shows a page that does not load — that is expected — copy its address (it starts 127.0.0.1:1455) and paste it here.'}
          </div>
          {authorizeUrl ? (
            <div className="unit">
              <a href={authorizeUrl} target="_blank" rel="noopener noreferrer" style={link}>
                No tab opened? Open the {name} sign-in page
                <Icon name="external-link" size={12} decorative />
              </a>
            </div>
          ) : null}
          <div className="fld">
            <label htmlFor={inputId}>{claude ? 'Authorization code or callback URL' : 'Address your browser landed on'}</label>
            <input
              id={inputId}
              type={claude ? 'password' : 'url'}
              autoComplete="off"
              spellCheck={false}
              value={callbackUrl}
              onChange={(e) => onCallbackUrl(e.target.value)}
              placeholder={claude ? 'code#state or https://…' : 'http://127.0.0.1:1455/auth/callback?code=…'}
            />
          </div>
          <div style={row}>
            <button type="button" onClick={onFinish} disabled={busy || !callbackUrl.trim()}>
              {busy ? 'Finishing…' : 'Finish sign-in'}
            </button>
            <button type="button" className="ghost" onClick={onContinue} disabled={busy}>Start again</button>
            <button type="button" className="ghost" onClick={onCancel}>Cancel</button>
          </div>
        </>
      ) : (
        <>
          <div className="unit">
            {claude
              ? 'Connected with your Claude subscription. Enable the models you want to serve in the existing Models catalogue; connecting does not enable any models.'
              : 'You’re using your ChatGPT plan. Requests derate routes to this provider use your ChatGPT plan allowance.'}
          </div>
          <div style={row}>
            {!claude ? (
              <a href={USAGE_URL} target="_blank" rel="noopener noreferrer" style={link}>
                Manage usage<Icon name="external-link" size={12} decorative />
              </a>
            ) : null}
            <button type="button" onClick={onDone}>Got it</button>
            <button type="button" className="ghost" onClick={onCancel}>Cancel</button>
          </div>
        </>
      )}
      {error ? (
        <div className="label" role="alert" style={{ color: 'var(--fault)', whiteSpace: 'pre-wrap' }}>{error}</div>
      ) : null}
    </div>
  )
}

/** Keying the controller also protects callers that change flow/provider in place. */
export function ProviderSignIn(props: ProviderSignInProps) {
  return <ProviderSignInController key={`${props.flow}:${props.providerId === undefined ? 'add' : `provider:${props.providerId}`}`} {...props} />
}

function ProviderSignInController({ flow, providerId, onDone, onCancel }: ProviderSignInProps) {
  const { backend, invalidate } = useBackend()
  const [step, setStep] = useState<ProviderSignInStep>('start')
  const [attempt, setAttempt] = useState<ProviderSignInStart | null>(null)
  const [callbackUrl, setCallbackUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Invalidate requests synchronously on cancel/restart and on unmount. A late
  // completion must never close the expander for a different provider.
  const generation = useRef(0)
  const active = useRef(true)
  const submitting = useRef(false)
  const onDoneRef = useRef(onDone)
  onDoneRef.current = onDone
  useEffect(() => {
    active.current = true
    return () => { active.current = false; generation.current++ }
  }, [])
  const current = useCallback((run: number) => active.current && generation.current === run, [])
  const succeeded = useCallback(() => {
    generation.current++
    submitting.current = false
    setCallbackUrl('')
    setAttempt(null)
    setBusy(false)
    invalidate()
    if (providerId) onDoneRef.current()
    else setStep('done')
  }, [invalidate, providerId])

  // ChatGPT's loopback redirect can complete without a paste. Both flows also
  // surface expired/consumed attempts instead of leaving an unusable form.
  const attemptId = step === 'paste' ? attempt?.attempt_id : undefined
  useEffect(() => {
    if (!attemptId) return
    let live = true
    const run = generation.current
    const timer = window.setInterval(() => {
      if (!live || !current(run) || submitting.current) return
      if (attempt && Date.now() / 1000 >= attempt.expires_at) {
        live = false
        window.clearInterval(timer)
        setError('This sign-in attempt has expired. Start again.')
        return
      }
      backend.providerSignInStatus(flow, attemptId).then(
        (s) => {
          if (!live || !current(run) || submitting.current || s.status === 'pending') return
          live = false
          window.clearInterval(timer)
          if (s.status === 'failed') setError(s.message)
          else succeeded()
        },
        (e: unknown) => {
          if (!live || !current(run) || submitting.current || !(e instanceof ApiError) || e.status !== 404) return
          live = false
          window.clearInterval(timer)
          setError(e.message)
        },
      )
    }, 2000)
    return () => { live = false; window.clearInterval(timer) }
  }, [attempt, attemptId, backend, current, flow, succeeded])

  const begin = async () => {
    if (submitting.current) return
    const run = ++generation.current
    submitting.current = true
    setBusy(true)
    setError(null)
    setCallbackUrl('')
    setAttempt(null)
    setStep('start')
    try {
      const started = await backend.startProviderSignIn(flow, providerId)
      if (!current(run)) return
      setAttempt(started)
      setStep('paste')
      window.open(started.authorize_url, '_blank', 'noopener,noreferrer')
    } catch (e) {
      if (current(run)) setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (current(run)) { submitting.current = false; setBusy(false) }
    }
  }

  const finish = async () => {
    if (!attempt || submitting.current) return
    const run = generation.current
    submitting.current = true
    setBusy(true)
    setError(null)
    try {
      await backend.completeProviderSignIn(flow, attempt.attempt_id, callbackUrl.trim())
      if (current(run)) succeeded()
    } catch (e) {
      if (current(run)) setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (current(run)) { submitting.current = false; setBusy(false) }
    }
  }

  const cancel = () => {
    generation.current++
    setCallbackUrl('')
    setAttempt(null)
    onCancel()
  }

  return <ProviderSignInView
    flow={flow} step={step} reauth={providerId !== undefined}
    authorizeUrl={attempt?.authorize_url ?? null} callbackUrl={callbackUrl}
    onCallbackUrl={setCallbackUrl} busy={busy} error={error}
    onContinue={() => void begin()} onFinish={() => void finish()}
    onDone={onDone} onCancel={cancel}
  />
}
