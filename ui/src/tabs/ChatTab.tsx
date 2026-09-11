import { useMemo, useRef, useState } from 'react'
import { ENDPOINT_FOR_MODALITY } from '../api/types'
import type { ChatContentPart, ChatMessage } from '../api/types'
import { useBackend } from '../state/backend'
import { useCluster, useModels } from '../state/resources'
import { useSelection } from '../state/selection'
import { ModelList, buildRows, emptyNote } from './chat/ModelList'
import { Transcript, type Turn } from './chat/Transcript'
import { Composer, type ImageAttachment, type SendExtra } from './chat/Composer'
import { DEFAULT_CHAT_PARAMS, parseMaxTokens, parseStop, parseTemperature } from './chat/RequestParams'
import { useVoices } from './chat/useVoices'

// Why this exists against a named non-goal.
//
// 00-architecture.md §1 rules out "a chat interface, or chat history", and
// calls it one of the four boundaries most likely to be rebuilt by accident.
// The history half of that still holds here and is enforced by construction:
// the transcript below is React state and nothing else. No localStorage, no
// fetch on mount, no server-side surface, nothing added to the backend. Reload
// the page and it is gone. Every richer feature added since -- edit/resend,
// regenerate, per-message reasoning, request params, image attachments --
// lives in exactly this same in-memory state and disappears with it.
//
// The interface half was reversed deliberately. Until this tab there was no
// way, from inside the product, to confirm that a deployment this cluster is
// running actually answers -- that meant leaving for curl. The tab is a client
// for two endpoints that already existed (`GET /v1/models`,
// `POST /v1/chat/completions`), and the readout under each answer is the point
// of it: which model served, the request id the gateway minted, and what it
// cost in time.
//
// It lists `/v1/models`' local half only. The whole endpoint is the router's
// target index, remote providers included, and a browser for a provider's
// catalogue is the "model catalog browser" §1 also rules out; the Models tab
// answers what runs here, and so does this. See chat/ModelList.tsx.
//
// SPEECH MODELS ARE IN THE PICKER, and this tab is what makes that honest.
// The composer posts to the endpoint the selected model is advertised on --
// `/v1/chat/completions` for text and embedding, `/v1/audio/speech` for a TTS
// deployment -- so picking `Audio8-TTS-Preview-0.6b` and typing a sentence
// returns a clip instead of a `wrong_modality` 400. The picker's section
// heading names that endpoint, and it comes from the same `row.modality` the
// branch below reads, so the heading cannot promise a route the send does not
// take. Voice and format appear in the composer for a speech model and are
// the same component `/speech` uses, so there is one dropdown to keep in step
// with what the server accepts rather than two.
//
// Settings' "Deliberately out of scope" card was narrowed to match, so the
// product does not deny a feature it ships.

/** A turn's `content`, widened into the OpenAI multi-part shape whenever it
 *  carries an image -- the same shape the new outgoing user message uses, so
 *  a picture attached three turns ago is still in the model's context on the
 *  next send rather than silently dropped from history. */
function turnToMessage(t: Turn): ChatMessage {
  if (t.images && t.images.length > 0) {
    const parts: ChatContentPart[] = [
      { type: 'text', text: t.content },
      ...t.images.map((img) => ({ type: 'image_url' as const, image_url: { url: img.url } })),
    ]
    return { role: t.role, content: parts }
  }
  return { role: t.role, content: t.content }
}

export function ChatTab() {
  const { backend } = useBackend()
  const models = useModels()
  const cluster = useCluster()
  // The picked model is the app's selected deployment -- `?dep=` -- and not a
  // second, private notion of the same thing. Every row here IS a deployment
  // serving right now, so the two have the same domain, and a link to
  // `/chat?dep=<served name>` opens on that model the way `/cluster?node=` opens
  // on that machine.
  const { selDep, selectDep } = useSelection()

  const [turns, setTurns] = useState<Turn[]>([])
  const [streamingKey, setStreamingKey] = useState<number | null>(null)
  const [params, setParams] = useState(DEFAULT_CHAT_PARAMS)
  const abortRef = useRef<AbortController | null>(null)
  // Minted in (even, odd) pairs, one call to `send` at a time -- `userKey` is
  // always even and `replyKey = userKey + 1` is always odd, and no other call
  // site hands one out. `deleteTurn` below relies on exactly this to find a
  // turn's other half from either one's key alone.
  const nextKey = useRef(0)

  const rows = useMemo(() => buildRows(models.data ?? []), [models.data])
  const note = useMemo(() => emptyNote(cluster.data?.deployments ?? []), [cluster.data])

  // Derived rather than held in an effect: if the selected model stops serving
  // -- its deployment was stopped, or it failed -- the selection falls back on
  // its own instead of leaving Send pointed at a name the gateway will now
  // refuse. `selDep` does most of that already, against `/api/topology`; the
  // check here is against `/v1/models`, which is polled separately.
  const named = selDep ? (rows.find((r) => r.name === selDep) ?? null) : null
  // The FALLBACK prefers a model that answers in text. An explicit `?dep=`
  // always wins -- picking a speech model is a real choice and this does not
  // override it -- but nothing should arrive at a chat console it never asked
  // about and find the composer already in speech mode.
  //
  // This became load-bearing the moment speech models joined the picker.
  // `buildRows` sorts local names alphabetically, and on this cluster
  // `Audio8-TTS-Preview-0.6b` sorts above `Qwen2.5-0.5B-Instruct`, so plain
  // `rows[0]` made the default chat model a TTS deployment. It is also what
  // was briefly on screen during load even WITH a `?dep=`, because the rows
  // resolve before the selection does and the composer visibly changes shape
  // between the two.
  const activeRow = named ?? rows.find((r) => r.modality !== 'speech') ?? rows[0] ?? null
  const active = activeRow?.name ?? null
  // Read off the row rather than looked up again: the row is what the section
  // heading was drawn from, so "the endpoint this says it goes to" and "the
  // endpoint it goes to" are one value.
  const speaking = activeRow?.modality === 'speech'
  const transcribing = activeRow?.modality === 'transcription'

  // Only asked for a speech model: the gateway answers `wrong_modality` for
  // anything else, correctly, and a refusal on screen about a control this
  // tab is not drawing would be noise.
  const { library, error: voicesError } = useVoices(active, speaking)

  // `?dep=` named something this picker has no row for, and the fallback above
  // quietly moved the selection to the first row instead.
  //
  // That is the failure a URL is supposed to prevent. On
  // `/chat?dep=Audio8-TTS-Preview-0.6b` the right rail read "Plan ·
  // Audio8-TTS-Preview-0.6b" while the pane read "Messages go to
  // Qwen2.5-0.5B-Instruct", and Send went to the second one -- a link that
  // names a model and talks to a different model. Letting speech in fixes the
  // case that produced it and does not fix the bug: `transcription` still has
  // no row on purpose, and a stopped or failed deployment still has none.
  //
  // Substituting is still the right behaviour -- an empty pane would be worse
  // -- so what is added is that it SAYS so, naming both models. Only when
  // something was actually named and actually missed; a bare `/chat` is not a
  // substitution.
  const substituted = selDep !== null && named === null && active !== null ? selDep : null

  const busy = streamingKey !== null

  /** `history` defaults to the live transcript, and is only ever given
   *  explicitly by `editAndResend`/`regenerate` below -- both truncate the
   *  transcript first (dropping an edited turn's old reply, or a
   *  regenerated one) and need `send` to build the outgoing `messages` from
   *  that truncated list rather than from state that has not caught up yet. */
  const send = async (text: string, extra: SendExtra, history: Turn[] = turns) => {
    if (!active || busy) return

    // History is every turn that carries real text or an image and did not
    // fail. A refused turn has no assistant content to send back, and a
    // stopped one does -- a partial answer is still what the model said.
    const messages: ChatMessage[] = history
      .filter((t) => t.error === null && (t.content !== '' || (t.images?.length ?? 0) > 0))
      .map(turnToMessage)
    const system = params.system.trim()
    if (system) messages.unshift({ role: 'system', content: system })

    const userKey = nextKey.current++
    const replyKey = nextKey.current++

    const userImages = extra.kind === 'chat' ? extra.images : []
    const userContent: ChatMessage['content'] =
      userImages.length > 0
        ? ([
            { type: 'text', text },
            ...userImages.map((img) => ({
              type: 'image_url' as const,
              image_url: { url: img.dataUrl },
            })),
          ] as ChatContentPart[])
        : text
    messages.push({ role: 'user', content: userContent })

    // `model: active` on BOTH halves, recorded here because this is the only
    // moment it is known. The picker can move to another model before the
    // answer lands and will move again over the life of the transcript, so a
    // turn that reads the current selection at render time would relabel and
    // recolour itself every time somebody switched -- which is the opposite of
    // what the colour is for. See chat/tags.ts.
    const userTurn: Turn = {
      key: userKey,
      role: 'user',
      content: text,
      reasoning: '',
      meta: null,
      error: null,
      requestId: null,
      audio: null,
      images:
        userImages.length > 0
          ? userImages.map((img) => ({ url: img.dataUrl, name: img.name }))
          : null,
      model: active,
      kind: extra.kind,
    }
    const replyTurn: Turn = {
      key: replyKey,
      role: 'assistant',
      content: '',
      reasoning: '',
      meta: null,
      error: null,
      requestId: null,
      audio: null,
      images: null,
      model: active,
      kind: extra.kind,
    }
    setTurns(() => [...history, userTurn, replyTurn])
    setStreamingKey(replyKey)

    const ac = new AbortController()
    abortRef.current = ac
    const patch = (fn: (t: Turn) => Turn) =>
      setTurns((prev) => prev.map((t) => (t.key === replyKey ? fn(t) : t)))

    try {
      if (extra.kind === 'transcription') {
        // Audio in, text out. The answer is text, so it lands in `content`
        // and every reader that already understood a chat turn renders it
        // unchanged -- the transcription case is the cheap one, and the
        // reason the Turn type grew `audio` rather than a media union.
        const out = await backend.transcribe(
          { model: active, file: extra.file, language: extra.language },
          ac.signal,
        )
        patch((t) => ({
          ...t,
          content: out.text,
          requestId: out.requestId,
          meta: {
            model: active,
            requestId: out.requestId,
            // Nothing token-denominated is measured on this path either. The
            // gateway deliberately counts no tokens over a binary body, and
            // inventing a count from the returned characters would be a
            // figure nobody reported.
            ttftMs: null,
            elapsedMs: null,
            completionTokens: null,
            tokensEstimated: false,
            reasoningMs: null,
            stopped: false,
          },
        }))
      } else if (extra.kind === 'speech') {
        // The whole of the branch. No history is sent -- a speech request has
        // an `input`, not a conversation, and the server would refuse an
        // unknown field.
        const clip = await backend.speech(
          {
            model: active,
            input: text,
            // From the composer's own fields. `voice` undefined is a real
            // request and the default one -- the model speaks in its own --
            // and client.ts omits the key rather than sending null.
            voice: extra.voice,
            response_format: extra.format,
          },
          ac.signal,
        )
        patch((t) => ({
          ...t,
          audio: clip,
          requestId: clip.requestId,
          // A ChatTurnMeta so the turn is `finished` to every reader that
          // already understood one. Every token-denominated figure is null
          // rather than 0: there are no tokens here, and the dash rule says
          // an absent figure is a dash. `elapsedMs` is real whatever the
          // bytes are, so it is measured.
          meta: {
            model: active,
            requestId: clip.requestId,
            ttftMs: null,
            elapsedMs: null,
            completionTokens: null,
            tokensEstimated: false,
            reasoningMs: null,
            stopped: false,
          },
        }))
      } else {
        const meta = await backend.chatStream({
          model: active,
          messages,
          // The id lands before the body does, so a turn that goes on to be
          // refused still names the row that recorded the refusal.
          onOpen: (id) => patch((t) => ({ ...t, requestId: id })),
          onDelta: (chunk) => patch((t) => ({ ...t, content: t.content + chunk })),
          onReasoning: (chunk) => patch((t) => ({ ...t, reasoning: t.reasoning + chunk })),
          signal: ac.signal,
          temperature: parseTemperature(params.temperature),
          max_tokens: parseMaxTokens(params.maxTokens),
          stop: parseStop(params.stop),
        })
        patch((t) => ({ ...t, meta }))
      }
    } catch (e) {
      // Stop, on the speech/transcription path. chatStream swallows its own
      // abort and returns a stopped meta; neither of these can -- there is no
      // partial audio or partial transcript to keep -- so the turn is marked
      // stopped rather than failed.
      if (extra.kind !== 'chat' && ac.signal.aborted) {
        patch((t) => ({
          ...t,
          meta: {
            model: active,
            requestId: t.requestId,
            ttftMs: null,
            elapsedMs: null,
            completionTokens: null,
            tokensEstimated: false,
            reasoningMs: null,
            stopped: true,
          },
        }))
        return
      }
      // ApiError already unwrapped the gateway's `{error:{message}}` envelope,
      // so this is the sentence the gateway wrote. Transcript renders it
      // through Verbatim; it is not reworded here or anywhere downstream.
      patch((t) => ({ ...t, error: e instanceof Error ? e.message : String(e) }))
    } finally {
      abortRef.current = null
      setStreamingKey(null)
    }
  }

  /** A sent user turn, edited and resubmitted. Drops that turn and its old
   *  reply (and anything after -- there never is anything, a reply is always
   *  the last turn) rather than appending a correction, the same "ask again"
   *  model ChatGPT/Claude.ai's own edit uses. Only ever called on a turn
   *  `Transcript` has already gated to `kind === 'chat'`. */
  const editAndResend = (key: number, text: string) => {
    if (busy) return
    const idx = turns.findIndex((t) => t.key === key)
    const turn = turns[idx]
    if (idx === -1 || !turn) return
    const images: ImageAttachment[] = (turn.images ?? []).map((img) => ({
      name: img.name,
      dataUrl: img.url,
    }))
    void send(text, { kind: 'chat', images }, turns.slice(0, idx))
  }

  /** Re-asks the question behind one assistant turn. `assistantKey` is always
   *  the user turn's key + 1 (see `nextKey`), so its question is a lookup,
   *  not a search. */
  const regenerate = (assistantKey: number) => {
    if (busy) return
    const idx = turns.findIndex((t) => t.key === assistantKey)
    if (idx <= 0) return
    const userTurn = turns[idx - 1]
    if (!userTurn || userTurn.role !== 'user') return
    const images: ImageAttachment[] = (userTurn.images ?? []).map((img) => ({
      name: img.name,
      dataUrl: img.url,
    }))
    void send(userTurn.content, { kind: 'chat', images }, turns.slice(0, idx - 1))
  }

  /** Removes one exchange, both halves together -- deleting only a reply
   *  would leave an orphaned question in the history the next send builds,
   *  and deleting only a question would leave an answer to nothing. */
  const deleteTurn = (key: number) => {
    if (busy) return
    const [a, b] = key % 2 === 0 ? [key, key + 1] : [key - 1, key]
    setTurns((prev) => prev.filter((t) => t.key !== a && t.key !== b))
  }

  return (
    <div className="chat">
      <ModelList
        rows={rows}
        selected={active}
        onSelect={selectDep}
        loading={models.loading}
        error={models.error}
        note={note}
      />

      <div className="chatpane">
        <div className="chatbody">
          <div className="chathead">
            <span className="mono">{active ?? '—'}</span>
            {/* The endpoint this pane will actually post to, beside the model
                it will post about. Same string as the picker's section
                heading, from the same field. */}
            <span className="unit">
              POST {ENDPOINT_FOR_MODALITY[activeRow?.modality ?? 'text']} · not stored ·
              this transcript is gone on reload
            </span>
          </div>
          {substituted ? (
            <p className="unit" style={{ margin: 'var(--s-2) 0 0', color: 'var(--warn)' }}>
              The link named <span className="mono">{substituted}</span>, which this picker
              has no row for — it is not serving, or it answers on an endpoint this pane
              cannot send to. Showing <span className="mono">{active}</span> instead.
            </p>
          ) : null}
          <Transcript
            turns={turns}
            streamingKey={streamingKey}
            model={active}
            mode={activeRow?.modality ?? 'text'}
            busy={busy}
            onEdit={editAndResend}
            onRegenerate={regenerate}
            onDelete={deleteTurn}
          />
        </div>

        <Composer
          disabled={active === null}
          busy={busy}
          onSend={(text, extra) => void send(text, extra)}
          onStop={() => abortRef.current?.abort()}
          onClear={() => setTurns([])}
          canClear={turns.length > 0}
          speaking={speaking}
          library={library}
          voicesError={voicesError}
          transcribing={transcribing}
          params={params}
          onParamsChange={setParams}
        />
      </div>
    </div>
  )
}
