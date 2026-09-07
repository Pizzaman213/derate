import { useEffect, useRef } from 'react'
import type { ChatRole, ChatTurnMeta } from '../../api/types'
import { Verbatim } from '../../components/Verbatim'
import { fmt } from '../../format'

export interface Turn {
  key: number
  role: ChatRole
  content: string
  /** Set when the turn finished normally, or was stopped. */
  meta: ChatTurnMeta | null
  /** The gateway's own sentence when it refused. Rendered verbatim. */
  error: string | null
  /** Known from the response headers, so an errored turn still names the row
   *  that recorded it. */
  requestId: string | null
}

/** The one line under an answer that makes this a control-plane surface rather
 *  than a chat window: what served it, the id of the row that recorded it, and
 *  what it cost in time.
 *
 *  Missing figures print as an em dash, never as zero, and a token count that
 *  came from counting delta frames says so — the gateway makes the same
 *  distinction on its own records and it is not ours to quietly drop. */
function metaLine(turn: Turn): string {
  const m = turn.meta
  const id = turn.requestId ?? m?.requestId ?? '—'
  if (!m) return id
  const tokens =
    m.completionTokens == null
      ? '—'
      : `${m.completionTokens}${m.tokensEstimated ? ' (est.)' : ''}`
  const parts = [
    m.model,
    id,
    `ttft ${fmt(m.ttftMs)} ms`,
    `${fmt(m.elapsedMs == null ? null : m.elapsedMs / 1000, 1)} s`,
    `${tokens} tok`,
  ]
  if (m.stopped) parts.push('stopped')
  return parts.join(' · ')
}

interface Props {
  turns: Turn[]
  /** The key of the turn currently being streamed into, if any. */
  streamingKey: number | null
  model: string | null
}

export function Transcript({ turns, streamingKey, model }: Props) {
  const endRef = useRef<HTMLDivElement>(null)

  // Follow the tail as tokens land. `block: 'nearest'` keeps it from yanking
  // the whole page when the pane is already in view, and base.css's
  // reduced-motion rule turns the smooth scroll off for anyone who asked.
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }, [turns, streamingKey])

  if (turns.length === 0) {
    return (
      <div className="chatlog">
        <div className="unit">
          {model
            ? `Nothing sent yet. Messages go to ${model} through the gateway, the same path any client takes.`
            : 'Pick a model to send it something.'}
        </div>
      </div>
    )
  }

  return (
    <div className="chatlog">
      {turns.map((turn) => {
        const streaming = turn.key === streamingKey
        return (
          <div key={turn.key} className={turn.role === 'user' ? 'turn me' : 'turn'}>
            <div className="label" style={{ color: 'var(--ink-muted)' }}>
              {turn.role === 'user' ? 'you' : (turn.meta?.model ?? model ?? 'assistant')}
            </div>

            {turn.content ? (
              <p
                style={{ margin: '4px 0 0', whiteSpace: 'pre-wrap' }}
                aria-live={streaming ? 'polite' : undefined}
              >
                {turn.content}
              </p>
            ) : null}

            {streaming && !turn.content ? (
              <p className="unit" style={{ margin: '4px 0 0' }} aria-live="polite">
                waiting for the first token
              </p>
            ) : null}

            {turn.error ? (
              <div style={{ marginTop: 6, color: 'var(--fault)' }}>
                <Verbatim text={turn.error} />
              </div>
            ) : null}

            {turn.role === 'assistant' && (turn.meta || turn.error) ? (
              <div className="unit mono" style={{ marginTop: 6 }}>
                {metaLine(turn)}
              </div>
            ) : null}
          </div>
        )
      })}
      <div ref={endRef} />
    </div>
  )
}
