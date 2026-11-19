import { useState, type FormEvent } from 'react'
import { PASSWORD_MIN_LEN, type AdminRow } from '../../api/types'
import { Icon } from '../../components/Icon'
import { PasswordField } from '../../components/PasswordField'
import { Verbatim } from '../../components/Verbatim'
import { useAuth } from '../../state/auth'
import { useBackend, useResource } from '../../state/backend'
import { passwordProblem } from './accounts'

const ABSENT = '\u2014'

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

/** Who can sign in to this coordinator, and the signed-in admin's own
 *  password.
 *
 *  The server is the authority on both refusals that matter: it will not
 *  remove the last admin, and it enforces `PASSWORD_MIN_LEN` itself. Its
 *  sentences render verbatim. The client-side length check only saves a round
 *  trip for the obvious case.
 *
 *  Changing your own password ends every session you hold, this one included;
 *  the form says so before it is submitted, and afterwards asks the auth state
 *  again, which lands on /login. */
export function AdminsCard() {
  const admins = useResource<AdminRow[]>((b) => b.admins(), 30000)
  const { backend, invalidate } = useBackend()
  const auth = useAuth()
  const me = auth.state?.username ?? null

  const [confirming, setConfirming] = useState<string | null>(null)
  const [removing, setRemoving] = useState(false)
  const [removeError, setRemoveError] = useState<string | null>(null)
  /** Removing an admin re-checks the signed-in admin's own password. */
  const [removePw, setRemovePw] = useState('')

  const remove = async (username: string) => {
    setRemoving(true)
    setRemoveError(null)
    try {
      await backend.removeAdmin(username, removePw)
      setRemovePw('')
      setConfirming(null)
      invalidate()
      if (username === me) await auth.refresh()
    } catch (e) {
      setRemoveError(message(e))
    } finally {
      setRemoving(false)
    }
  }

  const rows = admins.data

  return (
    <div className="card2">
      {admins.error ? <Verbatim text={admins.error.message} size="unit" /> : null}
      {!rows ? (
        admins.error ? null : (
          <div className="unit" aria-busy="true" aria-live="polite">
            Reading admins…
          </div>
        )
      ) : rows.length === 0 ? (
        <div className="unit">No admins.</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th scope="col">Username</th>
              <th scope="col">Email</th>
              <th scope="col">Added</th>
              <th scope="col">Added by</th>
              <th scope="col">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((a) => (
              <tr key={a.username}>
                <td>
                  {/* Baseline, so the name reads on the row's line; centring
                      it against the taller "you" pill lifted it 2.8px. */}
                  <span style={{ display: 'inline-flex', gap: 'var(--space-6)', alignItems: 'baseline' }}>
                    <Icon name="user" size={14} decorative className="icon-inline" />
                    <span className="mono">{a.username}</span>
                    {a.username === me ? <span className="pill">you</span> : null}
                  </span>
                </td>
                <td className="mono">{a.email ?? ABSENT}</td>
                <td className="mono">{new Date(a.created_at * 1000).toLocaleString()}</td>
                <td className="mono">{a.created_by ?? ABSENT}</td>
                <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                  {confirming === a.username ? (
                    <span style={{ display: 'inline-flex', gap: 'var(--space-6)', alignItems: 'center' }}>
                      <span className="unit">
                        {a.username === me ? 'Remove yourself and sign out?' : `Remove ${a.username}?`}
                      </span>
                      <label htmlFor="adm-remove-pw" className="sr-only">
                        Your password
                      </label>
                      <PasswordField
                        id="adm-remove-pw"
                        value={removePw}
                        autoComplete="current-password"
                        autoFocus
                        onChange={setRemovePw}
                      />
                      <button
                        type="button"
                        disabled={removing || !removePw}
                        onClick={() => void remove(a.username)}
                      >
                        {removing ? 'Removing…' : 'Remove'}
                      </button>
                      <button
                        type="button"
                        className="ghost"
                        disabled={removing}
                        onClick={() => {
                          setConfirming(null)
                          setRemoveError(null)
                          setRemovePw('')
                        }}
                      >
                        Cancel
                      </button>
                    </span>
                  ) : (
                    <button
                      type="button"
                      className="ghost"
                      onClick={() => {
                        setConfirming(a.username)
                        setRemoveError(null)
                      }}
                    >
                      <Icon name="trash" size={14} decorative />
                      Remove
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {removeError ? <Verbatim text={removeError} size="unit" /> : null}

      <AddAdminForm onAdded={invalidate} />
      {me ? <ChangePasswordForm username={me} onChanged={() => void auth.refresh()} /> : null}
    </div>
  )
}

function AddAdminForm({ onAdded }: { onAdded: () => void }) {
  const { backend } = useBackend()
  const [username, setUsername] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [currentPw, setCurrentPw] = useState('')
  const [fieldError, setFieldError] = useState<string | null>(null)
  const [serverError, setServerError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [added, setAdded] = useState<string | null>(null)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setServerError(null)
    setAdded(null)
    const problem =
      username.trim() && email.trim() ? passwordProblem(password, confirm, PASSWORD_MIN_LEN) : null
    if (!username.trim()) {
      setFieldError('Enter a username.')
      return
    }
    if (!email.trim()) {
      setFieldError('Enter an email.')
      return
    }
    setFieldError(problem)
    if (problem) return
    if (!currentPw) {
      setFieldError('Enter your own password to confirm.')
      return
    }
    setBusy(true)
    try {
      const row = await backend.addAdmin(username.trim(), email.trim(), password, currentPw)
      setAdded(row.username)
      setUsername('')
      setEmail('')
      setPassword('')
      setConfirm('')
      setCurrentPw('')
      onAdded()
    } catch (err) {
      setServerError(message(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    // noValidate: an email's shape is the server's sentence, rendered verbatim.
    <form noValidate onSubmit={(e) => void submit(e)} style={{ marginTop: 'var(--space-14)' }}>
      <h4 className="label">Add an admin</h4>
      <div style={{ display: 'flex', gap: 'var(--space-10)', alignItems: 'flex-end', flexWrap: 'wrap' }}>
        <div className="fld">
          <label htmlFor="adm-user">Username</label>
          <input
            id="adm-user"
            className="mono"
            value={username}
            autoComplete="off"
            onChange={(e) => setUsername(e.target.value)}
          />
        </div>
        <div className="fld">
          <label htmlFor="adm-email">Email</label>
          <input
            id="adm-email"
            type="email"
            className="mono"
            value={email}
            autoComplete="off"
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>
        <div className="fld">
          <label htmlFor="adm-pw">Password</label>
          <PasswordField
            id="adm-pw"
            value={password}
            autoComplete="new-password"
            aria-invalid={fieldError ? true : undefined}
            aria-describedby={fieldError ? 'adm-err' : 'adm-hint'}
            onChange={setPassword}
          />
        </div>
        <div className="fld">
          <label htmlFor="adm-pw2">Confirm password</label>
          <PasswordField
            id="adm-pw2"
            value={confirm}
            autoComplete="new-password"
            aria-invalid={fieldError ? true : undefined}
            aria-describedby={fieldError ? 'adm-err' : undefined}
            onChange={setConfirm}
          />
        </div>
        <div className="fld">
          <label htmlFor="adm-current-pw">Your password</label>
          <PasswordField
            id="adm-current-pw"
            value={currentPw}
            autoComplete="current-password"
            aria-invalid={fieldError ? true : undefined}
            aria-describedby={fieldError ? 'adm-err' : undefined}
            onChange={setCurrentPw}
          />
        </div>
        <button type="submit" disabled={busy}>
          {busy ? 'Adding…' : 'Add admin'}
        </button>
      </div>
      {fieldError ? (
        <div id="adm-err" className="unit" style={{ color: 'var(--fault)' }}>
          {fieldError}
        </div>
      ) : (
        <div id="adm-hint" className="unit">
          At least <span className="mono">{PASSWORD_MIN_LEN}</span> characters.
        </div>
      )}
      {serverError ? <Verbatim text={serverError} size="unit" /> : null}
      {added ? (
        <div className="unit" aria-live="polite">
          Added <span className="mono">{added}</span>.
        </div>
      ) : null}
    </form>
  )
}

function ChangePasswordForm({ username, onChanged }: { username: string; onChanged: () => void }) {
  const { backend } = useBackend()
  const [oldPw, setOldPw] = useState('')
  const [newPw, setNewPw] = useState('')
  const [confirm, setConfirm] = useState('')
  const [fieldError, setFieldError] = useState<string | null>(null)
  const [serverError, setServerError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setServerError(null)
    const problem = oldPw ? passwordProblem(newPw, confirm, PASSWORD_MIN_LEN) : 'Enter your current password.'
    setFieldError(problem)
    if (problem) return
    setBusy(true)
    try {
      await backend.changePassword(oldPw, newPw)
      setOldPw('')
      setNewPw('')
      setConfirm('')
      // Every session this admin held has ended, this one too. Asking the auth
      // state again is what moves the screen to /login.
      onChanged()
    } catch (err) {
      setServerError(message(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={(e) => void submit(e)} style={{ marginTop: 'var(--space-14)' }}>
      <h4 className="label" style={{ display: 'flex', gap: 'var(--space-6)', alignItems: 'center' }}>
        <Icon name="lock" size={14} decorative />
        Change your password
      </h4>
      {/* For password managers: which account this form is changing. */}
      <input type="text" name="username" value={username} autoComplete="username" readOnly hidden />
      <div style={{ display: 'flex', gap: 'var(--space-10)', alignItems: 'flex-end', flexWrap: 'wrap' }}>
        <div className="fld">
          <label htmlFor="pw-old">Current password</label>
          <PasswordField
            id="pw-old"
            value={oldPw}
            autoComplete="current-password"
            onChange={setOldPw}
          />
        </div>
        <div className="fld">
          <label htmlFor="pw-new">New password</label>
          <PasswordField
            id="pw-new"
            value={newPw}
            autoComplete="new-password"
            aria-invalid={fieldError ? true : undefined}
            aria-describedby={fieldError ? 'pw-err' : 'pw-hint'}
            onChange={setNewPw}
          />
        </div>
        <div className="fld">
          <label htmlFor="pw-new2">Confirm new password</label>
          <PasswordField
            id="pw-new2"
            value={confirm}
            autoComplete="new-password"
            aria-invalid={fieldError ? true : undefined}
            aria-describedby={fieldError ? 'pw-err' : undefined}
            onChange={setConfirm}
          />
        </div>
        <button type="submit" disabled={busy}>
          {busy ? 'Changing…' : 'Change password'}
        </button>
      </div>
      {fieldError ? (
        <div id="pw-err" className="unit" style={{ color: 'var(--fault)' }}>
          {fieldError}
        </div>
      ) : (
        <div id="pw-hint" className="unit">
          This signs <span className="mono">{username}</span> out everywhere, including here. You
          will sign in again with the new password.
        </div>
      )}
      {serverError ? <Verbatim text={serverError} size="unit" /> : null}
    </form>
  )
}
