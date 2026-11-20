import { useState } from 'react'
import type { AdminSession } from '../../api/types'
import { Icon } from '../../components/Icon'
import { Verbatim } from '../../components/Verbatim'
import { useAuth } from '../../state/auth'
import { useBackend, useResource } from '../../state/backend'

const ABSENT = '\u2014'

/** Every live admin session, and the containment step for each admin: sign
 *  them out everywhere (`DELETE /api/admins/<name>/sessions`). Any admin may
 *  do it to any admin; the server writes `sessions_revoked` with the caller as
 *  actor. "Signed in from" is where the session logged in and never moves;
 *  "Last seen from" moves with it, and a session that moved is the thing to
 *  look at -- the server has already written `session_ip_changed` for it. */
export function SessionsCard() {
  const sessions = useResource<AdminSession[]>((b) => b.sessions(), 30000)
  const { backend, invalidate } = useBackend()
  const auth = useAuth()
  const me = auth.state?.username ?? null

  const [confirming, setConfirming] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const revoke = async (username: string) => {
    setBusy(true)
    setError(null)
    try {
      await backend.revokeAdminSessions(username)
      setConfirming(null)
      invalidate()
      if (username === me) await auth.refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const rows = [...(sessions.data ?? [])].sort(
    (a, b) => a.username.localeCompare(b.username) || b.last_seen_at - a.last_seen_at,
  )

  return (
    <div className="card2">
      <h3>Sessions</h3>
      {sessions.error ? <Verbatim text={sessions.error.message} size="unit" /> : null}
      {!sessions.data ? (
        sessions.error ? null : (
          <div className="unit" aria-busy="true" aria-live="polite">
            Reading sessions…
          </div>
        )
      ) : rows.length === 0 ? (
        <div className="unit">No live sessions.</div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table>
            <thead>
              <tr>
                <th scope="col">Admin</th>
                <th scope="col">Session</th>
                <th scope="col">Signed in from</th>
                <th scope="col">Last seen from</th>
                <th scope="col">Browser</th>
                <th scope="col">Last seen</th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((s, i) => {
                const first = rows[i - 1]?.username !== s.username
                const moved = s.login_ip != null && s.ip !== s.login_ip
                return (
                  <tr key={s.session_id}>
                    <td>
                      <span style={{ display: 'inline-flex', gap: 'var(--space-6)', alignItems: 'baseline' }}>
                        <Icon name="user" size={14} decorative className="icon-inline" />
                        <span className="mono">{s.username}</span>
                        {s.current ? <span className="pill">this browser</span> : null}
                      </span>
                    </td>
                    <td className="mono">{s.session_id}</td>
                    <td className="mono">{s.login_ip ?? ABSENT}</td>
                    <td>
                      <span style={{ display: 'inline-flex', gap: 'var(--space-6)', alignItems: 'baseline' }}>
                        <span className="mono">{s.ip ?? ABSENT}</span>
                        {moved ? <span className="pill">moved</span> : null}
                      </span>
                    </td>
                    <td className="unit" title={s.user_agent ?? undefined}>
                      {s.user_agent ?? ABSENT}
                    </td>
                    <td className="mono">{new Date(s.last_seen_at * 1000).toLocaleString()}</td>
                    <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                      {!first ? null : confirming === s.username ? (
                        <span style={{ display: 'inline-flex', gap: 'var(--space-6)', alignItems: 'center' }}>
                          <span className="unit">
                            {s.username === me
                              ? 'End every session of yours, this one included?'
                              : `End every session of ${s.username}?`}
                          </span>
                          <button type="button" disabled={busy} onClick={() => void revoke(s.username)}>
                            {busy ? 'Signing out…' : 'Sign out everywhere'}
                          </button>
                          <button
                            type="button"
                            className="ghost"
                            disabled={busy}
                            onClick={() => {
                              setConfirming(null)
                              setError(null)
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
                            setConfirming(s.username)
                            setError(null)
                          }}
                        >
                          <Icon name="log-out" size={14} decorative />
                          Sign out everywhere
                        </button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
      {error ? <Verbatim text={error} size="unit" /> : null}
    </div>
  )
}
