import { Fragment, useEffect, useRef, useState, type UIEvent } from 'react'
import { plainClick, useRouter } from '../../state/router'
import type { Modality } from '../../api/types'
import { Disclosure } from '../../components/Panel'
import { Verbatim } from '../../components/Verbatim'
import { fmt } from '../../format'
import { Clip } from './Clip'
import { Markdown } from './Markdown'
import { reasoningVerdict, turnFigures } from './stats'
import { tagInk, tagStyle, tagsFor } from './tags'
import type { Turn } from './turn'

/** The model a block is about: what served it, else what it was sent to. The
 *  two are the same string in every case seen so far, and the order matters
 *  anyway -- if the gateway ever answers under a different name than it was
 *  asked for, the block should be coloured as the model that actually spoke. */
function destination(turn: Turn): string | null {
  return turn.meta?.model ?? turn.model
}

/** The block under an answer that makes this a control-plane surface rather
 *  than a chat window: what served it, the id of the row that recorded it, why
 *  it stopped, what it cost in time and tokens, and whether reasoning came
 *  back and how.
 *
 *  Missing figures print as an em dash, never as zero, and a token count that
 *  came from counting delta frames says so — the gateway makes the same
 *  distinction on its own records and it is not ours to quietly drop. */
function TurnMeta({ turn }: { turn: Turn }) {
  const { navigate, linkTo } = useRouter()
  const m = turn.meta
  const id = turn.requestId ?? m?.requestId ?? '—'
  const idNode =
    id === '—' ? (
      id
    ) : (
      <a
        href={linkTo({ dest: 'usage', sub: 'requests', request: id })}
        onClick={(e) => {
          if (!plainClick(e)) return
          e.preventDefault()
          navigate({ dest: 'usage', sub: 'requests', request: id })
        }}
      >
        {id}
      </a>
    )
  const join = (parts: (string | null | undefined | typeof ID)[]) =>
    parts
      .filter((p) => p)
      .map((p, i) => (
        <Fragment key={i}>
          {i > 0 ? ' · ' : null}
          {p === ID ? idNode : p}
        </Fragment>
      ))
  if (!m) return <div className="turnmeta unit mono">{idNode}</div>
  const head = join([
    m.model,
    ID,
    m.stopped ? 'stopped' : m.finishReason ? `finish ${m.finishReason}` : null,
  ])
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
    return <div className="turnmeta unit mono"><div>{head}</div></div>
  }
  const f = turnFigures(m)
  const reasoned = m.reasoningSource !== null
  const figures = join([
    `ttft ${fmt(f.ttftMs)} ms`,
    reasoned ? `think ${fmt(f.thinkMs == null ? null : f.thinkMs / 1000, 1)} s` : null,
    `gen ${fmt(f.genMs == null ? null : f.genMs / 1000, 1)} s`,
    `${fmt(f.tokPerS, 1)} tok/s`,
    `in ${fmt(f.promptTokens)}`,
    `out ${fmt(f.completionTokens)}${f.estimated && f.completionTokens !== null ? ' (est.)' : ''}`,
    reasoned
      ? `think ${fmt(f.reasoningTokens)}${f.reasoningEstimated && f.reasoningTokens !== null ? ' (est.)' : ''} tok`
      : null,
  ])
  const verdict = turn.setup ? reasoningVerdict(turn, turn.setup) : null
  return (
    <div className="turnmeta unit mono">
      <div>{head}</div>
      <div>{figures}</div>
      {verdict ? <div>{verdict}</div> : null}
    </div>
  )
}

/** Where the request id sits in a meta line; rendered as a link to it. */
const ID = Symbol('request id')

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
  // A thinking block someone has opened or closed stays that way; until then
  // it follows the stream -- open while the model is still thinking, closed
  // once the answer starts, so the answer is what is on screen.
  const [pinned, setPinned] = useState<Map<number, boolean>>(() => new Map())
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

            {turn.reasoning !== '' ? (
              <ThinkingBlock
                turn={turn}
                streaming={streaming}
                open={pinned.get(turn.key) ?? (streaming && turn.content === '')}
                onToggle={(open) => setPinned((prev) => new Map(prev).set(turn.key, open))}
              />
            ) : null}

            {turn.content ? (
              turn.role === 'user' ? (
                <p style={{ margin: 'var(--space-4) 0 0', whiteSpace: 'pre-wrap' }}>{turn.content}</p>
              ) : (
                <div className="md" aria-live={streaming ? 'polite' : undefined}>
                  <Markdown text={turn.content} />
                </div>
              )
            ) : null}

            {streaming && !turn.content && !turn.audio ? (
              <p className="unit" style={{ margin: 'var(--space-4) 0 0' }} aria-live="polite">
                {/* A speech model does not stream, so "the first token" is
                    not the thing being waited for and never arrives. It is
                    one whole file at the end. */}
                {turn.reasoning !== '' ? 'thinking…' : waiting}
              </p>
            ) : null}

            {turn.audio ? <Clip clip={turn.audio} /> : null}

            {turn.error ? (
              <div style={{ marginTop: 'var(--space-6)', color: 'var(--fault)' }}>
                <Verbatim text={turn.error} />
              </div>
            ) : null}

            {turn.role === 'assistant' && (turn.meta || turn.error || turn.audio) ? (
              <TurnMeta turn={turn} />
            ) : null}
          </div>
        )
      })}
    </div>
  )
}

/** The model's reasoning, collapsed under one line that says how much of it
 *  there was. Plain text: it is a working, not an answer, and rendering it as
 *  markdown would dress it as one. */
function ThinkingBlock({
  turn,
  streaming,
  open,
  onToggle,
}: {
  turn: Turn
  streaming: boolean
  open: boolean
  onToggle: (open: boolean) => void
}) {
  const m = turn.meta
  let summary = streaming && turn.content === '' ? 'Thinking…' : 'Thinking'
  if (m) {
    const f = turnFigures(m)
    summary = `Thinking · ${fmt(f.reasoningTokens)} tok${
      f.reasoningEstimated && f.reasoningTokens !== null ? ' (est.)' : ''
    } · ${fmt(f.thinkMs == null ? null : f.thinkMs / 1000, 1)} s`
  }
  return (
    <div style={{ marginTop: 'var(--space-4)' }}>
      <Disclosure summary={summary} open={open} onToggle={() => onToggle(!open)}>
        <div className="think">{turn.reasoning.trimEnd()}</div>
      </Disclosure>
    </div>
  )
}
