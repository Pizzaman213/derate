import { useState } from 'react'
import type { Candidate } from '../api/types'
import { gbytes, shortGpu } from '../format'

interface Props {
  candidates: Candidate[]
  coordinatorAddress: string | null
  onAdmit: (nodeId: string) => Promise<void>
}

/** Discovery proposes, a human accepts. Found machines sit here greyed with the
 *  hardware we detected, and joining is one click. There is no IP field in this
 *  path on purpose. */
export function Discovery({ candidates, coordinatorAddress, onAdmit }: Props) {
  const [busy, setBusy] = useState<string | null>(null)
  const [manual, setManual] = useState(false)

  const admit = async (id: string) => {
    setBusy(id)
    try {
      await onAdmit(id)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div style={{ display: 'grid', gap: 'var(--s1)' }}>
      {candidates.length === 0 ? (
        <p className="label muted" style={{ fontWeight: 400, margin: 0 }}>
          Nothing new on the local subnet. Discovery is running.
        </p>
      ) : (
        candidates.map((c) => (
          <div key={c.node_id} style={{ display: 'grid', gap: 4 }}>
            <div style={{ color: 'var(--ink-muted)', fontWeight: 500 }}>
              {c.hostname}
            </div>
            <div className="unit">
              {shortGpu(c.gpu_name)} · {gbytes(c.total_memory, 0)} GB · {c.address}
            </div>
            {c.note ? (
              <div
                className="label"
                style={{ fontWeight: 400, color: 'var(--ink-muted)' }}
              >
                {c.note}
              </div>
            ) : null}
            <div>
              <button
                onClick={() => void admit(c.node_id)}
                disabled={busy === c.node_id}
              >
                {busy === c.node_id ? 'Adding…' : 'Add to cluster'}
              </button>
            </div>
          </div>
        ))
      )}

      <div>
        <button
          onClick={() => setManual((m) => !m)}
          aria-expanded={manual}
          className="label"
          style={{
            border: 0,
            padding: 0,
            color: 'var(--ink-muted)',
            textDecoration: 'underline',
            textUnderlineOffset: 3,
          }}
        >
          A machine on another subnet
        </button>
        {manual ? (
          <div style={{ paddingTop: 8, display: 'grid', gap: 8 }}>
            <p className="label" style={{ fontWeight: 400, margin: 0 }}>
              mDNS does not cross subnets. Run this on that machine and it will
              join directly:
            </p>
            <code
              className="mono"
              style={{
                fontSize: 12,
                lineHeight: 1.5,
                display: 'block',
                background: 'var(--panel-recessed)',
                border: '1px solid var(--rule)',
                padding: 8,
                overflowX: 'auto',
                whiteSpace: 'pre',
              }}
            >
              {`docker run --network host \\\n  -e SPARKPLANE_JOIN=${coordinatorAddress ?? '<coordinator-ip>'}:8080 \\\n  -v sparkplane:/data sparkplane/node`}
            </code>
            <p className="label muted" style={{ fontWeight: 400, margin: 0 }}>
              It will appear here as a candidate, and you admit it the same way.
            </p>
          </div>
        ) : null}
      </div>
    </div>
  )
}
