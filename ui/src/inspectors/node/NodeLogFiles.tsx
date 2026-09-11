import { useEffect, useRef, useState } from 'react'
import type { NodeLogTail } from '../../api/types'
import { useBackend } from '../../state/backend'
import { Verbatim } from '../../components/Verbatim'

const FILES = ['node', 'proxy'] as const
type File = (typeof FILES)[number]

const LIMITS = [200, 500, 2000] as const

/** `node.log`/`proxy.log`, read straight off the machine.
 *
 *  Distinct from `EventsAndLogs` above it and `DeploymentLog`: those are the
 *  structured archive and a served model's own stdout. This is the control
 *  plane's own process log -- what an operator would otherwise need a shell
 *  on the machine to `tail`.
 *
 *  Same rule as `EventsAndLogs`: no query box, no logger filter. A file
 *  toggle and a line-count choice are the only controls, because a text
 *  search box is exactly the thing this project has already decided a log
 *  panel does not get.
 *
 *  Read on demand, like `DeploymentLog`: a file tail has no "is this
 *  actively streaming" signal to poll against, so a reader asks for it and
 *  refreshes by hand. */
export function NodeLogFiles({ nodeId }: { nodeId: string }) {
  const { backend } = useBackend()
  const [which, setWhich] = useState<File>('node')
  const [limit, setLimit] = useState<(typeof LIMITS)[number]>(500)
  const [logs, setLogs] = useState<NodeLogTail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const [nonce, setNonce] = useState(0)
  const boxRef = useRef<HTMLPreElement>(null)
  const followRef = useRef(true)

  useEffect(() => {
    let live = true
    setPending(true)
    backend
      .nodeLogTail(nodeId, which, limit)
      .then((answer) => {
        if (!live) return
        setLogs(answer)
        setError(null)
      })
      .catch((e) => {
        if (!live) return
        setError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (live) setPending(false)
      })
    return () => {
      live = false
    }
  }, [backend, nodeId, which, limit, nonce])

  useEffect(() => {
    const box = boxRef.current
    if (!box || !followRef.current) return
    box.scrollTop = box.scrollHeight
  }, [logs])

  const lines = logs?.lines ?? []

  return (
    <div className="logbox">
      <h4>
        <span>
          log files
          {logs ? <span className="unit"> · {lines.length} lines</span> : null}
        </span>
        <span className="chips">
          {FILES.map((f) => (
            <button key={f} aria-pressed={f === which} onClick={() => setWhich(f)}>
              {f}.log
            </button>
          ))}
          {LIMITS.map((n) => (
            <button key={n} aria-pressed={n === limit} onClick={() => setLimit(n)}>
              {n}
            </button>
          ))}
          <button onClick={() => setNonce((n) => n + 1)} disabled={pending}>
            {pending ? 'Reading…' : 'Refresh'}
          </button>
        </span>
      </h4>

      {lines.length > 0 && !error ? (
        <pre
          ref={boxRef}
          className="mono body"
          onScroll={(e) => {
            const el = e.currentTarget
            followRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24
          }}
        >
          {lines.join('\n')}
        </pre>
      ) : (
        <div className="body">
          {error ? (
            <p className="label" style={{ color: 'var(--fault)', fontWeight: 400, margin: 0 }}>
              {error}
            </p>
          ) : !logs ? (
            <div className="unit">Reading…</div>
          ) : !logs.available ? (
            <Verbatim text={logs.reason ?? `${which}.log is not available.`} size="unit" />
          ) : (
            <div className="unit">Nothing logged yet.</div>
          )}
        </div>
      )}

      {logs?.available && lines.length > 0 ? (
        <div className="foot unit">
          {logs.truncated
            ? 'The byte cap on this read was hit before it reached this far back, so there is more history in the file than this shows.'
            : 'The live file. Rotated backups are not read.'}
        </div>
      ) : null}
    </div>
  )
}
