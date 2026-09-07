import { useMemo, useRef, useState } from 'react'
import type { ChatMessage } from '../api/types'
import { useBackend } from '../state/backend'
import { useCluster, useModels } from '../state/resources'
import { ModelList, buildRows } from './chat/ModelList'
import { Transcript, type Turn } from './chat/Transcript'
import { Composer } from './chat/Composer'

// Why this exists against a named non-goal.
//
// 00-architecture.md §1 rules out "a chat interface, or chat history", and
// calls it one of the four boundaries most likely to be rebuilt by accident.
// The history half of that still holds here and is enforced by construction:
// the transcript below is React state and nothing else. No localStorage, no
// fetch on mount, no server-side surface, nothing added to the backend. Reload
// the page and it is gone.
//
// The interface half was reversed deliberately. Until this tab there was no
// way, from inside the product, to see the model list the gateway actually
// serves or to confirm that a deployment answers -- both meant leaving for
// curl. The tab is a client for two endpoints that already existed
// (`GET /v1/models`, `POST /v1/chat/completions`), and the readout under each
// answer is the point of it: which model served, the request id the gateway
// minted, and what it cost in time.
//
// Settings' "Deliberately out of scope" card was narrowed to match, so the
// product does not deny a feature it ships.

export function ChatTab() {
  const { backend } = useBackend()
  const models = useModels()
  const cluster = useCluster()

  const [turns, setTurns] = useState<Turn[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [streamingKey, setStreamingKey] = useState<number | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const nextKey = useRef(0)

  const rows = useMemo(
    () => buildRows(models.data ?? [], cluster.data?.deployments ?? []),
    [models.data, cluster.data],
  )

  // Derived rather than held in an effect: if the selected model stops being
  // servable -- its deployment was stopped, a provider went unhealthy -- the
  // selection falls back on its own instead of leaving Send pointed at a name
  // the gateway will now refuse.
  const firstServable = rows.find((r) => r.servable)?.name ?? null
  const active =
    selected && rows.some((r) => r.name === selected && r.servable) ? selected : firstServable

  const busy = streamingKey !== null

  const send = async (text: string) => {
    if (!active || busy) return

    // History is every turn that carries real text and did not fail. A refused
    // turn has no assistant content to send back, and a stopped one does --
    // a partial answer is still what the model said.
    const messages: ChatMessage[] = turns
      .filter((t) => t.error === null && t.content !== '')
      .map((t) => ({ role: t.role, content: t.content }))
    messages.push({ role: 'user', content: text })

    const userKey = nextKey.current++
    const replyKey = nextKey.current++
    setTurns((prev) => [
      ...prev,
      { key: userKey, role: 'user', content: text, meta: null, error: null, requestId: null },
      { key: replyKey, role: 'assistant', content: '', meta: null, error: null, requestId: null },
    ])
    setStreamingKey(replyKey)

    const ac = new AbortController()
    abortRef.current = ac
    const patch = (fn: (t: Turn) => Turn) =>
      setTurns((prev) => prev.map((t) => (t.key === replyKey ? fn(t) : t)))

    try {
      const meta = await backend.chatStream({
        model: active,
        messages,
        // The id lands before the body does, so a turn that goes on to be
        // refused still names the row that recorded the refusal.
        onOpen: (id) => patch((t) => ({ ...t, requestId: id })),
        onDelta: (chunk) => patch((t) => ({ ...t, content: t.content + chunk })),
        signal: ac.signal,
      })
      patch((t) => ({ ...t, meta }))
    } catch (e) {
      // ApiError already unwrapped the gateway's `{error:{message}}` envelope,
      // so this is the sentence the gateway wrote. Transcript renders it
      // through Verbatim; it is not reworded here or anywhere downstream.
      patch((t) => ({ ...t, error: e instanceof Error ? e.message : String(e) }))
    } finally {
      abortRef.current = null
      setStreamingKey(null)
    }
  }

  return (
    <div className="chat">
      <ModelList
        rows={rows}
        selected={active}
        onSelect={setSelected}
        loading={models.loading}
        error={models.error}
      />

      <div className="chatpane">
        <div className="chatbody">
          <div className="chathead">
            <span className="mono">{active ?? '—'}</span>
            <span className="unit">not stored · this transcript is gone on reload</span>
          </div>
          <Transcript turns={turns} streamingKey={streamingKey} model={active} />
        </div>

        <Composer
          disabled={active === null}
          busy={busy}
          onSend={(text) => void send(text)}
          onStop={() => abortRef.current?.abort()}
          onClear={() => setTurns([])}
          canClear={turns.length > 0}
        />
      </div>
    </div>
  )
}
