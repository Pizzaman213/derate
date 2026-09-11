import { useEffect, useRef, useState, type UIEvent } from 'react'
import type { ChatRole, ChatTurnMeta, Modality, SpeechResult } from '../../api/types'
import { Verbatim } from '../../components/Verbatim'
import { copyToClipboard } from '../../components/clipboard'
import { fmt } from '../../format'
import { Clip } from './Clip'
import { MessageBody } from './markdown'
import { metaLine } from './meta'
import { tagInk, tagStyle, tagsFor } from './tags'

export interface Turn {
  key: number
  role: ChatRole
  content: string
  /** A model's "thinking" text, kept apart from `content` so it can render
   *  collapsed. Empty string when the turn carried none -- same convention as
   *  `content` itself, which is empty before the first delta arrives. */
  reasoning: string
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
  /** Images attached to a user turn. Object URLs, revoked when the turn
   *  leaves the transcript (see `ChatTab`'s cleanup). */
  images: { url: string; name: string }[] | null
  /** The model this turn was SENT to, recorded at send time on both halves of
   *  the exchange. The answer also carries `meta.model` -- what actually
   *  served -- and that one wins where both exist; this is what a question has
   *  instead, and what an answer has before the first byte comes back. */
  model: string | null
  /** Which endpoint this turn's send actually went to. Edit and Regenerate
   *  only make sense for a plain chat turn -- resending a transcription's
   *  audio file or a speech reply's spoken line as chat text is not the same
   *  request, so those are hidden per-turn rather than inferred from the
   *  picker's CURRENT modality, which may have moved on since this turn sent. */
  kind: 'chat' | 'speech' | 'transcription'
}

/** The model a block is about: what served it, else what it was sent to. The
 *  two are the same string in every case seen so far, and the order matters
 *  anyway -- if the gateway ever answers under a different name than it was
 *  asked for, the block should be coloured as the model that actually spoke. */
function destination(turn: Turn): string | null {
  return turn.meta?.model ?? turn.model
}

function reasoningLabel(turn: Turn): string {
  const ms = turn.meta?.reasoningMs
  return ms == null ? 'Reasoning' : `Reasoning · ${fmt(ms / 1000, 1)} s`
}

/** A small text button that reports its own copy outcome, same pattern
 *  `CodeBlock` in `markdown.tsx` uses for a code fence. Kept local rather than
 *  shared -- the two only share the clipboard call, not any layout. */
function CopyButton({ text }: { text: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const timer = useRef<number | undefined>(undefined)
  const copy = async () => {
    const result = await copyToClipboard(text)
    setState(result)
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setState('idle'), 2000)
  }
  return (
    <button type="button" className="turnaction" onClick={() => void copy()}>
      {state === 'copied' ? 'Copied' : state === 'failed' ? 'Select to copy' : 'Copy'}
    </button>
  )
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
  /** A turn's own `kind` decides whether Edit/Regenerate show at all; this
   *  disables every action while any turn is in flight, so a resend cannot
   *  race the one already running. */
  busy?: boolean
  onEdit?: (key: number, text: string) => void
  onRegenerate?: (key: number) => void
  onDelete?: (key: number) => void
}

export function Transcript({
  turns,
  streamingKey,
  model,
  mode = 'text',
  busy = false,
  onEdit,
  onRegenerate,
  onDelete,
}: Props) {
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

  // Only one turn editable at a time -- a second Edit click on another turn
  // simply moves the same inline box rather than opening a second one.
  const [editingKey, setEditingKey] = useState<number | null>(null)
  const [draft, setDraft] = useState('')

  useEffect(() => {
    const box = boxRef.current
    if (!box || !followRef.current) return
    box.scrollTop = box.scrollHeight
  }, [turns, streamingKey])

  const onScroll = (e: UIEvent<HTMLDivElement>) => {
    const el = e.currentTarget
    followRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24
  }

  const startEdit = (turn: Turn) => {
    setEditingKey(turn.key)
    setDraft(turn.content)
  }
  const cancelEdit = () => setEditingKey(null)
  const saveEdit = () => {
    if (editingKey === null) return
    const text = draft.trim()
    setEditingKey(null)
    if (text) onEdit?.(editingKey, text)
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
        const editable = turn.kind === 'chat' && turn.audio === null
        const editing = editingKey === turn.key

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
                  : `you → ${dest}`
                : (dest ?? model ?? 'assistant')}
            </div>

            {turn.images && turn.images.length > 0 ? (
              <div className="turnimages">
                {turn.images.map((img) => (
                  <img key={img.url} src={img.url} alt={img.name} />
                ))}
              </div>
            ) : null}

            {turn.reasoning ? (
              <details className="reasoning">
                <summary className="unit">{reasoningLabel(turn)}</summary>
                <MessageBody text={turn.reasoning} />
              </details>
            ) : null}

            {editing ? (
              <div className="turnedit">
                <textarea
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  rows={3}
                  autoFocus
                />
                <div style={{ display: 'flex', gap: 'var(--s-2)' }}>
                  <button type="button" onClick={saveEdit} disabled={draft.trim() === ''}>
                    Save &amp; resend
                  </button>
                  <button type="button" onClick={cancelEdit}>
                    Cancel
                  </button>
                </div>
              </div>
            ) : (
              <>
                {turn.content ? (
                  <MessageBody text={turn.content} live={streaming} />
                ) : null}

                {streaming && !turn.content && !turn.audio ? (
                  <p className="unit" style={{ margin: '4px 0 0' }} aria-live="polite">
                    {/* A speech model does not stream, so "the first token" is
                        not the thing being waited for and never arrives. It is
                        one whole file at the end. */}
                    {waiting}
                  </p>
                ) : null}
              </>
            )}

            {turn.audio ? <Clip clip={turn.audio} /> : null}

            {turn.error ? (
              <div style={{ marginTop: 6, color: 'var(--fault)' }}>
                <Verbatim text={turn.error} />
              </div>
            ) : null}

            {turn.role === 'assistant' && (turn.meta || turn.error || turn.audio) ? (
              <div className="unit mono" style={{ marginTop: 6 }}>
                {metaLine(turn.meta, turn.requestId)}
              </div>
            ) : null}

            {!editing && !streaming ? (
              <div className="turnactions">
                {turn.role === 'assistant' && turn.content ? (
                  <CopyButton text={turn.content} />
                ) : null}
                {turn.role === 'user' && editable ? (
                  <button
                    type="button"
                    className="turnaction"
                    disabled={busy}
                    onClick={() => startEdit(turn)}
                  >
                    Edit
                  </button>
                ) : null}
                {turn.role === 'assistant' && editable ? (
                  <button
                    type="button"
                    className="turnaction"
                    disabled={busy}
                    onClick={() => onRegenerate?.(turn.key)}
                  >
                    Regenerate
                  </button>
                ) : null}
                <button
                  type="button"
                  className="turnaction"
                  disabled={busy}
                  onClick={() => onDelete?.(turn.key)}
                >
                  Delete
                </button>
              </div>
            ) : null}
          </div>
        )
      })}
    </div>
  )
}
