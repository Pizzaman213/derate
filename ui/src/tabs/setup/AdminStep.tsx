import { useState, type FormEvent } from 'react'
import { ApiError } from '../../api/client'
import { PASSWORD_MIN_LEN } from '../../api/types'
import { PasswordField } from '../../components/PasswordField'
import { RevealModal } from '../../components/RevealModal'
import { Verbatim } from '../../components/Verbatim'
import { useAuth } from '../../state/auth'
import { useBackend } from '../../state/backend'
import { useRouter } from '../../state/router'
import { errorCode } from '../login/lockout'
import { adminFormProblem } from './admin'
import '../login/login.css'

// The first step of first run: the account every later screen answers to.
//
// Until it exists the gateway answers 401 to every /api route except the auth
// state and this one POST, so this step is the whole screen -- SetupTab mounts
// nothing else, and none of the wizard's prefetches run, until "I saved it".
//
// `POST /api/setup/admin` answers with the default API key's secret, once
// (RevealModal is the only place a `derate-` secret is ever drawn). It is held in
// this component's state only until the person says they saved it, then
// dropped; it is never logged, never put in a URL and never stored.

export function AdminStep({
  onMinted,
  onSaved,
}: {
  /** The admin and its key exist; the reveal is on screen. */
  onMinted: (username: string) => void
  /** "I saved it" was pressed, the secret is dropped and the auth state re-read. */
  onSaved: () => void
}) {
  const { backend } = useBackend()
  const { refresh } = useAuth()
  const { navigate } = useRouter()
  const [username, setUsername] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [again, setAgain] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [closed, setClosed] = useState(false)
  const [reveal, setReveal] = useState<{ secret: string; keyId: string } | null>(null)

  const problem = adminFormProblem(username, email, password, again)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (busy || problem) return
    setBusy(true)
    setError(null)
    try {
      const res = await backend.setupAdmin(username.trim(), email.trim(), password)
      setPassword('')
      setAgain('')
      onMinted(username.trim())
      setReveal({ secret: res.secret_once, keyId: res.key.key_id })
    } catch (err) {
      setPassword('')
      setAgain('')
      // 410 `setup_closed`: another browser created the first admin between
      // this page loading and the submit. Nothing here can be retried; the
      // way in is that admin's sign-in.
      if (err instanceof ApiError && (err.status === 410 || errorCode(err.body) === 'setup_closed')) {
        setClosed(true)
      }
      setError(String((err as Error)?.message ?? err))
    } finally {
      setBusy(false)
    }
  }

  const saved = async () => {
    // Dropped first: once the modal is gone nothing in the tree holds it.
    setReveal(null)
    // The session cookie came back with the admin; the gate reads it here,
    // and only then lets the wizard mount.
    try {
      await refresh()
    } finally {
      onSaved()
    }
  }

  const toLogin = async () => {
    // Re-read first: the gate still believes no admin exists, and would bounce
    // /login straight back to /setup on that stale answer.
    await refresh()
    navigate({ dest: 'login' }, { replace: true })
  }

  if (closed) {
    return (
      <div className="setup-step setup-auth">
        <h1>Setup was already finished in another browser.</h1>
        <p className="setup-lede">
          The first admin exists now, so this step is closed for good. Sign in with that
          account to continue.
        </p>
        {error && (
          <div className="setup-warn setup-fault">
            <Verbatim text={error} size="label" />
          </div>
        )}
        <button type="button" className="btn primary" onClick={() => void toLogin()}>
          Go to sign in
        </button>
      </div>
    )
  }

  return (
    <div className="setup-step setup-auth">
      <h1>Create the admin account.</h1>
      <p className="setup-lede">
        Every screen here answers only to an admin, and every request to the endpoint needs
        an API key. This makes the first of each: your sign-in, and a default key.
      </p>

      {/* noValidate: an email's shape is the server's sentence, rendered verbatim,
          not the browser's own bubble. */}
      <form className="login-form" noValidate onSubmit={(e) => void submit(e)}>
        <label className="label">
          Username
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
          Email
          <input
            className="setup-field"
            name="email"
            type="email"
            autoComplete="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        <label className="label">
          Password
          <PasswordField
            inputClassName="setup-field"
            name="new-password"
            autoComplete="new-password"
            value={password}
            onChange={setPassword}
          />
        </label>
        <label className="label">
          Password again
          <PasswordField
            inputClassName="setup-field"
            name="new-password-again"
            autoComplete="new-password"
            value={again}
            onChange={setAgain}
          />
        </label>
        <p className="setup-note" aria-live="polite">
          {problem ?? `At least ${PASSWORD_MIN_LEN} characters.`}
        </p>

        {error && (
          <div className="setup-warn setup-fault" role="alert">
            <Verbatim text={error} size="label" />
          </div>
        )}

        <div className="setup-row">
          <button type="submit" className="btn primary" disabled={busy || problem !== null}>
            {busy ? 'Creating…' : 'Create admin'}
          </button>
        </div>
      </form>

      {reveal && (
        <RevealModal secret={reveal.secret} keyId={reveal.keyId} onDone={() => void saved()} />
      )}
    </div>
  )
}
