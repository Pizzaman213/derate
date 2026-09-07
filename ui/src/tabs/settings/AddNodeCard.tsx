import { useEffect, useMemo, useRef, useState } from 'react'
import type { Enrollment } from '../../api/types'
import { useBackend } from '../../state/backend'
import { useCandidates, useCluster, useEnrollments } from '../../state/resources'
import { Copyable } from '../../components/Copyable'
import { gbytes, shortGpu } from '../../format'
import { fromState, nodeName } from '../../state/names'

/** Settings -> Add a node: the install command, and what turns up after it runs.
 *
 *  This card is the thing `NodesCard`'s header comment says did not exist. The
 *  mockup had an address form; it was dropped because join is
 *  worker-to-coordinator and token-gated, so there was no endpoint an address
 *  field could call. That is still true — the fix is not to call the
 *  coordinator from here, but to hand the operator the line that makes the
 *  *other* machine call it.
 *
 *  Two commands, and they are not interchangeable:
 *
 *  - The first node has no coordinator to fetch a script from, so its command
 *    names the repo. It is static, it carries no credential, and it is shown
 *    always — including on a cluster that already has a coordinator, because
 *    that is the command for the machine you have not set up yet.
 *  - Every node after fetches the script from this coordinator and carries an
 *    enrollment token, which admits it on arrival. Composed by the server:
 *    the address in it must be one another machine can reach, and the only
 *    thing this page knows is `window.location`, which is right in production
 *    and a lie in dev.
 *
 *  The token is shown once. There is no reveal control and no endpoint that
 *  returns a token you already minted (see `api/redact.ts`); minting another
 *  costs nothing, so losing one is not a state worth building around.
 */
export function AddNodeCard() {
  const { backend, invalidate } = useBackend()
  const cluster = useCluster()
  const candidates = useCandidates()
  const live = useEnrollments()

  const [minted, setMinted] = useState<Enrollment | null>(null)
  const [mintedAt, setMintedAt] = useState(0)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [tick, setTick] = useState(0)

  // Nodes that were already here when the command was generated. Anything that
  // appears afterwards is what the operator is standing there waiting for.
  const before = useRef<Set<string> | null>(null)

  // Only while a countdown is on screen. A card with no live token does not
  // need a 1 Hz re-render for the rest of the session.
  useEffect(() => {
    if (!minted) return
    const id = window.setInterval(() => setTick((t) => t + 1), 1000)
    return () => window.clearInterval(id)
  }, [minted])

  const nodes = cluster.data?.nodes ?? []
  const found = candidates.data ?? []
  const rows = live.data ?? []

  const arrived = useMemo(() => {
    const seen = before.current
    if (!seen) return []
    return nodes.filter((n) => !seen.has(n.profile.node_id))
  }, [nodes])

  // The server's own countdown, anchored to when we received it. Trusting
  // `expires_at` against the browser's clock would show a negative timer on a
  // machine whose time is off, which is most of them.
  const remaining = minted
    ? Math.max(0, minted.expires_in_s - (Date.now() - mintedAt) / 1000)
    : 0
  const expired = Boolean(minted) && remaining <= 0
  void tick

  const mint = async () => {
    setBusy('mint')
    setError(null)
    try {
      const token = await backend.mintEnrollment()
      before.current = new Set(nodes.map((n) => n.profile.node_id))
      setMinted(token)
      setMintedAt(Date.now())
      invalidate()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  const revoke = async (tokenId: string) => {
    setBusy(tokenId)
    setError(null)
    try {
      await backend.revokeEnrollment(tokenId)
      if (minted?.token_id === tokenId) setMinted(null)
      invalidate()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  const admit = async (nodeId: string) => {
    setBusy(nodeId)
    setError(null)
    try {
      await backend.admit(nodeId)
      invalidate()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  // Tokens minted in another tab, or before a refresh. The command cannot be
  // shown again, so these are listed only so they can be revoked.
  const elsewhere = rows.filter((r) => r.token_id !== minted?.token_id)

  return (
    <div className="card2">
      <h3>Add a node</h3>
      <div className="unit" style={{ marginBottom: 12 }}>
        One command per machine. Every node runs the same image and decides its own
        role: the first becomes the coordinator, the rest join it.
      </div>

      <div className="sub">The first node</div>
      <div className="unit" style={{ marginTop: 6 }}>
        Run this on a machine that has no cluster yet. It has nothing to fetch from,
        so it takes the script from the repository.
      </div>
      <Copyable text={firstNodeCommand(minted)} />

      <div className="sub" style={{ marginTop: 16 }}>Add to this cluster</div>

      {minted && !expired ? (
        <>
          <div className="unit" style={{ marginTop: 6 }}>
            Run this on the new machine. It installs, joins, and is admitted — there
            is nothing to click afterwards.
          </div>
          <Copyable text={minted.command} />
          <div
            style={{
              display: 'flex',
              alignItems: 'baseline',
              gap: 8,
              flexWrap: 'wrap',
              marginTop: 8,
            }}
          >
            <span className="pill mono">{minted.token_id}</span>
            <span className="unit">
              expires in {clock(remaining)} ·{' '}
              {minted.uses_remaining === null
                ? 'unlimited machines'
                : `${minted.uses_remaining} machine${minted.uses_remaining === 1 ? '' : 's'}`}
            </span>
            <button
              style={{ padding: '3px 9px', marginLeft: 'auto' }}
              onClick={() => void revoke(minted.token_id)}
              disabled={busy === minted.token_id}
            >
              {busy === minted.token_id ? 'Revoking…' : 'Revoke'}
            </button>
            <button
              style={{ padding: '3px 9px' }}
              onClick={() => void mint()}
              disabled={busy === 'mint'}
            >
              {busy === 'mint' ? 'Generating…' : 'Regenerate'}
            </button>
          </div>
        </>
      ) : (
        <>
          <div className="unit" style={{ marginTop: 6 }}>
            {expired
              ? 'That command has expired. Generate another — it costs nothing.'
              : 'Generates a command with a one-hour token in it. The token admits one machine and is spent when it does.'}
          </div>
          <div style={{ marginTop: 8 }}>
            <button onClick={() => void mint()} disabled={busy === 'mint'}>
              {busy === 'mint' ? 'Generating…' : 'Generate join command'}
            </button>
          </div>
        </>
      )}

      {elsewhere.length > 0 ? (
        <div style={{ marginTop: 12, display: 'grid', gap: 6 }}>
          {elsewhere.map((row) => (
            <div
              key={row.token_id}
              style={{ display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' }}
            >
              <span className="pill mono">{row.token_id}</span>
              <span className="unit">
                another live token · expires in {clock(row.expires_in_s)} · its command
                cannot be shown again
              </span>
              <button
                style={{ padding: '3px 8px', marginLeft: 'auto' }}
                onClick={() => void revoke(row.token_id)}
                disabled={busy === row.token_id}
              >
                {busy === row.token_id ? 'Revoking…' : 'Revoke'}
              </button>
            </div>
          ))}
        </div>
      ) : null}

      {minted || arrived.length > 0 || found.length > 0 ? (
        <div style={{ marginTop: 14, paddingTop: 10, borderTop: '1px solid var(--rule)' }}>
          <div className="label" style={{ marginBottom: 8 }}>
            {arrived.length === 0 && found.length === 0
              ? 'Waiting for nodes…'
              : 'Since you generated that command'}
          </div>

          {arrived.map((n) => (
            <div
              key={n.profile.node_id}
              style={{
                display: 'flex',
                alignItems: 'baseline',
                gap: 8,
                flexWrap: 'wrap',
                padding: '3px 0',
              }}
            >
              <span className="pill" style={{ color: 'var(--live)' }}>
                joined
              </span>
              <span className="mono">{nodeName(fromState(n))}</span>
              <span className="unit">
                {shortGpu(n.profile.gpu_name)} ·{' '}
                {gbytes(n.profile.addressable_memory, 0)} GB · {n.profile.address}
              </span>
            </div>
          ))}

          {/* A node that reached the coordinator without a live enrollment token
              — an mDNS sighting, or the permanent cluster token. It still needs
              the click, which is the same one NodesCard offers. */}
          {found.map((c) => (
            <div
              key={c.node_id}
              style={{
                display: 'flex',
                alignItems: 'baseline',
                gap: 8,
                flexWrap: 'wrap',
                padding: '3px 0',
              }}
            >
              <span className="pill">discovered</span>
              <span className="mono">{c.hostname}</span>
              <span className="unit">
                {shortGpu(c.gpu_name)} · {gbytes(c.addressable_memory, 0)} GB · {c.address}
              </span>
              {c.eligible === false && c.ineligible_reason ? (
                <span className="unit">{c.ineligible_reason}</span>
              ) : null}
              <button
                style={{ padding: '3px 8px', marginLeft: 'auto' }}
                onClick={() => void admit(c.node_id)}
                disabled={busy === c.node_id || c.eligible === false}
              >
                {busy === c.node_id ? 'Adding…' : 'Admit'}
              </button>
            </div>
          ))}

          {arrived.length === 0 && found.length === 0 ? (
            <div className="unit">
              Run the command above on the other machine. It appears here within a few
              seconds of the container starting.
            </div>
          ) : null}
        </div>
      ) : null}

      {error ? (
        <div
          className="label"
          style={{ color: 'var(--fault)', marginTop: 8, whiteSpace: 'pre-wrap' }}
        >
          {error}
        </div>
      ) : null}
    </div>
  )
}

/** mm:ss. Hours are possible only for a token minted with a custom TTL. */
function clock(seconds: number): string {
  const total = Math.max(0, Math.round(seconds))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const secs = total % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(secs)}` : `${minutes}:${pad(secs)}`
}

/** The repository URL, which only the coordinator knows for certain.
 *
 *  Hardcoded here as the fallback and taken from the mint response when there
 *  is one: the branch moves when this lands on main, and `enroll_api.py` is
 *  where that constant lives. A UI that guessed it would go stale silently.
 */
const FALLBACK_INSTALL_URL =
  'https://raw.githubusercontent.com/Pizzaman213/derate/integration/install.sh'

function firstNodeCommand(minted: Enrollment | null): string {
  return `curl -fsSL ${minted?.public_install_url ?? FALLBACK_INSTALL_URL} | sh`
}
