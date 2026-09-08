import { useEffect, useRef, type UIEvent } from 'react'
import type { ChatRole, ChatTurnMeta, Modality, SpeechResult } from '../../api/types'
import { Verbatim } from '../../components/Verbatim'
import { fmt } from '../../format'
import { Clip } from './Clip'
import { tagInk, tagStyle, tagsFor } from './tags'

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
  /** The answer, when the model this turn went to answers in audio rather
   *  than in text. Mutually exclusive with `content` in practice -- a speech
   *  reply has no tokens and a chat reply has no clip -- but kept as a
   *  separate field rather than a tagged union, because the turn's identity
   *  is the request and not the media type of what came back. */
  audio: SpeechResult | null
  /** The model this turn was SENT to, recorded at send time on both halves of
   *  the exchange. The answer also carries `meta.model` -- what actually
   *  served -- and that one wins where both exist; this is what a question has
   *  instead, and what an answer has before the first byte comes back. */
  model: string | null
}

/** The model a block is about: what served it, else what it was sent to. The
 *  two are the same string in every case seen so far, and the order matters
 *  anyway -- if the gateway ever answers under a different name than it was
 *  asked for, the block should be coloured as the model that actually spoke. */
function destination(turn: Turn): string | null {
  return turn.meta?.model ?? turn.model
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
  // Nothing token-denominated exists on this turn, so none of it is printed.
  //
  // Both audio endpoints land here: a speech turn's real figures are duration
  // and sample rate and `Clip` renders them, and a transcription turn has no
  // rate at all -- the gateway deliberately counts no tokens over a binary
  // body either way. The test is on the figures rather than on the modality
  // so that it stays true for whatever the fourth endpoint turns out to be.
  //
  // This is the dash rule, not an exception to it: an unmeasured figure is a
  // dash, and a figure that does not exist for this kind of request is not a
  // row at all. `ttft — ms · — s · — tok` reads as three failed measurements
  // rather than as a request that was never going to have them.
  if (m.ttftMs === null && m.elapsedMs === null && m.completionTokens === null) {
    const line = [m.model, id].filter(Boolean).join(' · ')
    return m.stopped ? `${line} · stopped` : line
  }
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
  /** The endpoint family the selected model answers on. Only changes two
   *  captions -- what an in-flight turn is waiting for, and what the empty
   *  transcript promises -- because everything else about a turn is the same
   *  shape whatever came back. */
  mode?: Modality
}

export function Transcript({ turns, streamingKey, model, mode = 'text' }: Props) {
  // Only one of the three streams, and only one of the three is waiting for a
  // token. Naming the wrong thing is not cosmetic here: "waiting for the
  // first token" in front of a request that produces no tokens reads as a
  // stall in a request that is working.
  const waiting =
    mode === 'speech'
      ? 'generating audio'
      : mode === 'transcription'
        ? 'transcribing'
        : 'waiting for the first token'
  const boxRef = useRef<HTMLDivElement>(null)
  // Whether the reader is at the bottom, sampled on scroll. Follow the tail as
  // tokens land, unless the reader has scrolled up to read something -- same
  // pattern as DeploymentLog's followRef, and for the same reason.
  const followRef = useRef(true)

  useEffect(() => {
    const box = boxRef.current
    if (!box || !followRef.current) return
    box.scrollTop = box.scrollHeight
  }, [turns, streamingKey])

  const onScroll = (e: UIEvent<HTMLDivElement>) => {
    const el = e.currentTarget
    followRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24
  }

  if (turns.length === 0) {
    return (
      <div className="chatlog" ref={boxRef} onScroll={onScroll}>
        <div className="unit">
          {model
            ? mode === 'transcription'
              ? `Nothing sent yet. Choose an audio file below and ${model} will transcribe it, through the gateway, the same path any client takes.`
              : `Nothing sent yet. Messages go to ${model} through the gateway, the same path any client takes.${
                  mode === 'speech'
                    ? ' It answers in audio, so the reply is a clip rather than text.'
                    : ''
                }`
            : 'Pick a model to send it something.'}
        </div>
      </div>
    )
  }

  // One pass over the transcript, so both halves of an exchange read the same
  // map and a question is drawn in the colour of the answer it is waiting for.
  const tags = tagsFor(turns.map(destination))

  return (
    <div className="chatlog" ref={boxRef} onScroll={onScroll}>
      {turns.map((turn) => {
        const streaming = turn.key === streamingKey
        const dest = destination(turn)
        const slot = dest === null ? undefined : tags.get(dest)
        return (
          <div
            key={turn.key}
            className={turn.role === 'user' ? 'turn me' : 'turn'}
            style={tagStyle(slot)}
          >
            {/* The colour is a shorthand for this line, never a replacement
                for it: a question NAMES where it is going, which nothing on
                screen said before, and an answer names what served. Together
                they are what makes two models on one prompt legible as two
                models rather than as four turns. */}
            <div className="label" style={{ color: tagInk(slot) }}>
              {turn.role === 'user'
                ? dest === null
                  ? 'you'
                  : `you \u2192 ${dest}`
                : (dest ?? model ?? 'assistant')}
            </div>

            {turn.content ? (
              <p
                style={{ margin: '4px 0 0', whiteSpace: 'pre-wrap' }}
                aria-live={streaming ? 'polite' : undefined}
              >
                {turn.content}
              </p>
            ) : null}

            {streaming && !turn.content && !turn.audio ? (
              <p className="unit" style={{ margin: '4px 0 0' }} aria-live="polite">
                {/* A speech model does not stream, so "the first token" is
                    not the thing being waited for and never arrives. It is
                    one whole file at the end. */}
                {waiting}
              </p>
            ) : null}

            {turn.audio ? <Clip clip={turn.audio} /> : null}

            {turn.error ? (
              <div style={{ marginTop: 6, color: 'var(--fault)' }}>
                <Verbatim text={turn.error} />
              </div>
            ) : null}

            {turn.role === 'assistant' && (turn.meta || turn.error || turn.audio) ? (
              <div className="unit mono" style={{ marginTop: 6 }}>
                {metaLine(turn)}
              </div>
            ) : null}
          </div>
        )
      })}
    </div>
  )
}
