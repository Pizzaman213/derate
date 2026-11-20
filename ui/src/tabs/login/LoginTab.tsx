import { useEffect, useState, type FormEvent } from 'react'
import { ApiError } from '../../api/client'
import { Icon } from '../../components/Icon'
import { PasswordField } from '../../components/PasswordField'
import { Verbatim } from '../../components/Verbatim'
import { useAuth } from '../../state/auth'
import { useBackend } from '../../state/backend'
import { errorCode, formatRetry, retryAfterSeconds } from './lockout'
import '../setup/setup.css'
import './login.css'

// Sign-in.
//
// Drawn before the frame, like first run: there is no session yet, so every
// panel the chrome would mount answers 401. AppShell's gate shows this for any
// path while an admin exists and this browser holds no session, and moves on
// by itself once `refresh()` says the session is there -- so this screen never
// navigates, it only asks the coordinator again.
//
// The gateway's refusal is rendered verbatim. The one thing computed here is
// the lockout's wait (lockout.ts), because "too many attempts" without a time
// sends people back to try again while the lock still holds -- and every
// failure inside the window makes the next lock longer.

interface Refusal {
  message: string
  /** Epoch ms at which a `login_locked` refusal lifts; null for any other. */
  until: number | null
}

export function LoginTab() {
  const { backend } = useBackend()
  const { refresh } = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [refusal, setRefusal] = useState<Refusal | null>(null)
  const [now, setNow] = useState(() => Date.now())

  const locked = refusal?.until != null && refusal.until > now

  // A one-second tick, only while a lock is counting down.
  useEffect(() => {
    if (!locked) return
    const t = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(t)
  }, [locked])

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (busy || locked || !username || !password) return
    setBusy(true)
    setRefusal(null)
    try {
      await backend.login(username, password)
      setPassword('')
      await refresh()
    } catch (err) {
      setPassword('')
      setRefusal(refusalOf(err))
      setNow(Date.now())
    } finally {
      setBusy(false)
    }
  }

  const waitLeft = refusal?.until != null ? (refusal.until - now) / 1000 : null

  return (
    <div className="setup">
      <header className="setup-head">
        <Icon name="derate-mark" label="derate" size={24} className="brand-mark" />
        <span className="setup-wordmark">derate</span>
        <span className="setup-where setup-note mono">sign in</span>
      </header>
      <main className="setup-main">
        <div className="setup-auth">
          <h1 className="login-title">
            <Icon name="lock" decorative size={24} />
            Sign in
          </h1>
          <p className="setup-lede">
            This coordinator answers its screens only to an admin. Sign in with the username or
            email of an admin account.
          </p>

          <form className="login-form" onSubmit={(e) => void submit(e)}>
            <label className="label">
              Username or email
              <input
                className="setup-field"
                name="username"
                autoComplete="username"
                autoFocus
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              />
            </label>
            <label className="label">
              Password
              <PasswordField
                inputClassName="setup-field"
                name="password"
                autoComplete="current-password"
                value={password}
                onChange={setPassword}
              />
            </label>

            {refusal && (
              <div className="setup-warn setup-fault" role="alert">
                <Verbatim text={refusal.message} size="label" />
                {waitLeft !== null && (
                  <p className="login-wait">{formatRetry(Math.max(0, waitLeft))}</p>
                )}
              </div>
            )}

            <div className="setup-row">
              <button
                type="submit"
                className="btn primary"
                disabled={busy || locked || !username || !password}
              >
                {busy ? 'Signing in…' : 'Sign in'}
              </button>
            </div>
          </form>
        </div>
      </main>
    </div>
  )
}

/** A failed `POST /api/auth/login` as the form shows it. 401
 *  `invalid_credentials` and every other refusal carry the gateway's own
 *  sentence; 429 `login_locked` also carries when it lifts. A network failure
 *  is not an ApiError and says what the browser said. */
function refusalOf(err: unknown): Refusal {
  const message = String((err as Error)?.message ?? err)
  if (!(err instanceof ApiError)) return { message, until: null }
  if (err.status !== 429 && errorCode(err.body) !== 'login_locked') return { message, until: null }
  // `headers` is read when the client carries it; until then the body's
  // `retry_after_s` is the source (lockout.ts names the precedence).
  const header = (err as ApiError & { headers?: Headers }).headers?.get('retry-after') ?? null
  const s = retryAfterSeconds(header, err.body)
  return { message, until: s === null ? null : Date.now() + s * 1000 }
}
