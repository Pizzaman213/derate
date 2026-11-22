import { useState } from 'react'
import type { Resource } from '../../state/backend'
import { useBackend } from '../../state/backend'
import { Verbatim } from '../../components/Verbatim'
import { relativeTime, sizeLabel } from '../../format'
import type { Replica, ReplicationProposal, ReplicationSkip, ReplicationState } from '../../api/types'

const GiB = 1024 ** 3

/** What Approve asks, word for word: the model, the node, the bytes, EVERY
 *  deletion, and when it runs. Nothing is approved without this answer. */
function confirmText(p: ReplicationProposal): string {
  const deletions = p.evict.length
    ? `To make room it first deletes derate's own copies on ${p.node_id}:\n` +
      p.evict.map((e) => `  - ${e.repo_id} (${sizeLabel(e.bytes)})`).join('\n')
    : 'Nothing is deleted.'
  return (
    `Copy ${p.repo_id} to ${p.node_id}?\n\n` +
    `This sends ${sizeLabel(p.bytes)} to ${p.node_id}` +
    (p.free_bytes != null ? `, which has ${sizeLabel(p.free_bytes)} free.` : '.') +
    `\n\n${deletions}\n\n` +
    'It runs in the background when nothing is launching; a launch pauses it.'
  )
}

/** Background copies: models derate would place on other nodes so a relaunch
 *  there does not wait on the weights, and the copies it already placed.
 *
 *  Every proposal is a warning, not a plan in motion. It names the bytes it
 *  would send and every copy it would delete to make room, and nothing moves
 *  until a person approves it through a `window.confirm` that restates all of
 *  that -- the server takes the exact deletions shown and answers 409 when
 *  its plan has changed underneath, which refreshes the card. */
export function ReplicationCard({ replication }: { replication: Resource<ReplicationState> }) {
  const { backend, invalidate } = useBackend()
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const state = replication.data
  const keyOf = (p: { folder: string; node_id: string }) => `${p.node_id}/${p.folder}`

  const act = async (p: ReplicationProposal, run: () => Promise<unknown>) => {
    setBusy(keyOf(p))
    setError(null)
    try {
      await run()
    } catch (e) {
      // 409 is the plan changing between the screen and the click: the
      // server's sentence says what moved, and the refresh below shows it.
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
      invalidate()
    }
  }

  const approve = (p: ReplicationProposal) => {
    if (!window.confirm(confirmText(p))) return
    void act(p, () =>
      backend.approveReplication(
        p.folder,
        p.node_id,
        p.evict.map((e) => e.folder),
      ),
    )
  }

  return (
    <div className="card2">
      <h3>Background copies</h3>
      <div className="unit" style={{ marginBottom: 'var(--space-10)' }}>
        derate proposes copying the models it serves on a schedule, or that you
        pinned, to the other nodes that would serve them, so a relaunch there
        does not wait on the download. Nothing is copied or deleted until you
        approve it.
        {state
          ? ` A copy never leaves a node with less free than a fifth of its disk or ${(state.floor_bytes / GiB).toFixed(0)} GiB, whichever is smaller.`
          : ''}
        {state?.checked_at != null ? ` Last planned ${relativeTime(state.checked_at)}.` : ''}
      </div>

      {replication.loading ? (
        <div className="unit">Reading…</div>
      ) : !state ? (
        replication.error ? <Verbatim text={replication.error.message} size="unit" /> : null
      ) : (
        <>
          {!state.enabled ? (
            <div className="unit">Background copies are switched off on this coordinator.</div>
          ) : !state.proposals.length ? (
            <div className="unit">Nothing to copy.</div>
          ) : (
            state.proposals.map((p) => (
              <Proposal
                key={keyOf(p)}
                p={p}
                busy={busy}
                pending={busy === keyOf(p)}
                onApprove={() => approve(p)}
                onDismiss={() => void act(p, () => backend.dismissReplication(p.folder, p.node_id))}
              />
            ))
          )}
          <Skipped rows={state.skipped} />
          <Replicas rows={state.replicas} />
        </>
      )}

      {error ? (
        <div
          className="label"
          style={{ color: 'var(--fault)', marginTop: 'var(--space-8)', whiteSpace: 'pre-wrap' }}
        >
          {error}
        </div>
      ) : null}
    </div>
  )
}

function Proposal({
  p,
  busy,
  pending,
  onApprove,
  onDismiss,
}: {
  p: ReplicationProposal
  busy: string | null
  pending: boolean
  onApprove: () => void
  onDismiss: () => void
}) {
  const waiting = p.status === 'awaiting_approval'
  return (
    <div
      style={{
        borderLeft: `var(--space-2) solid ${waiting ? 'var(--warn)' : 'var(--rule)'}`,
        paddingLeft: 'var(--space-6)',
        marginBottom: 'var(--space-10)',
      }}
    >
      <div className="row">
        <span className="mono">
          {p.repo_id} → {p.node_id}
        </span>
        <span className="unit">
          sends {sizeLabel(p.bytes)}
          {p.free_bytes != null ? ` · ${sizeLabel(p.free_bytes)} free there` : ''}
          {p.floor_bytes != null ? ` · keeps ${sizeLabel(p.floor_bytes)} free` : ''}
        </span>
      </div>
      {p.reasons.map((r) => (
        <Verbatim key={r} text={r} size="unit" />
      ))}
      {p.evict.map((e) => (
        <div key={e.folder} className="unit" style={{ color: 'var(--warn)' }}>
          to make room it will delete its own copy of {e.repo_id} ({sizeLabel(e.bytes)})
        </div>
      ))}
      {p.last_error ? (
        <div
          style={{
            borderLeft: 'var(--space-2) solid var(--fault)',
            paddingLeft: 'var(--space-6)',
            marginTop: 'var(--space-4)',
          }}
        >
          <div className="label" style={{ color: 'var(--fault)' }}>
            last attempt failed
          </div>
          <Verbatim text={p.last_error} size="unit" />
        </div>
      ) : null}
      {waiting ? (
        <div className="row" style={{ marginTop: 'var(--space-4)' }}>
          <span style={{ display: 'inline-flex', gap: 'var(--space-4)', alignItems: 'center' }}>
            <button type="button" onClick={onApprove} disabled={busy != null}>
              {pending ? 'Working…' : 'Approve'}
            </button>
            <button type="button" className="ghost" onClick={onDismiss} disabled={busy != null}>
              Not now
            </button>
          </span>
        </div>
      ) : (
        <div className="unit" style={{ color: 'var(--live)', marginTop: 'var(--space-4)' }}>
          {p.status === 'copying'
            ? 'copying — progress under Downloads'
            : 'approved — starts when nothing is launching'}
        </div>
      )}
    </div>
  )
}

function Skipped({ rows }: { rows: ReplicationSkip[] }) {
  if (!rows.length) return null
  return (
    <details style={{ marginBottom: 'var(--space-10)' }}>
      <summary className="unit">Not proposed ({rows.length})</summary>
      {rows.map((s) => (
        <div key={`${s.node_id}/${s.folder}`} style={{ marginTop: 'var(--space-4)' }}>
          <span className="mono">
            {s.repo_id} → {s.node_id}
          </span>
          <Verbatim text={s.sentence} size="unit" />
        </div>
      ))}
    </details>
  )
}

function Replicas({ rows }: { rows: Replica[] }) {
  if (!rows.length) return null
  return (
    <div>
      <div className="label" style={{ marginBottom: 'var(--space-4)' }}>
        derate's copies
      </div>
      {rows.map((r) => (
        <div key={`${r.node_id}/${r.folder}`} className="row">
          <span className="mono">{r.repo_id}</span>
          <span className="unit">
            {r.node_id} · {sizeLabel(r.bytes)}
            {r.complete ? '' : ' · partial'}
          </span>
        </div>
      ))}
    </div>
  )
}
