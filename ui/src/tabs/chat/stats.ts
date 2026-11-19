import type { ChatTurnMeta } from '../../api/types'
import type { ReasoningSetup } from './reasoning'
import { suggestedParser } from './reasoning'
import type { Turn } from './turn'

// Every figure the chat tab prints about a turn or a session, derived from
// what `chatStream` measured and nothing else. A figure that cannot be derived
// is `null` and renders as an em dash; none is defaulted to 0.

export interface TurnFigures {
  ttftMs: number | null
  /** Time spent reasoning: first reasoning token to first answer token, or to
   *  the end of a stream that never got past thinking. */
  thinkMs: number | null
  /** Decode time, thinking included: first token to end of stream. */
  genMs: number | null
  tokPerS: number | null
  promptTokens: number | null
  completionTokens: number | null
  reasoningTokens: number | null
  estimated: boolean
  reasoningEstimated: boolean
  finishReason: string | null
  stopped: boolean
}

/** `ChatTurnMeta` as the client fills it, with the fields this build's
 *  `api/types.ts` still declares optional resolved to `null`. */
function complete(m: ChatTurnMeta) {
  return {
    ...m,
    firstContentMs: m.firstContentMs ?? null,
    promptTokens: m.promptTokens ?? null,
    reasoningTokens: m.reasoningTokens ?? null,
    reasoningEstimated: m.reasoningEstimated ?? false,
    reasoningSource: m.reasoningSource ?? null,
    finishReason: m.finishReason ?? null,
  }
}

export function turnFigures(meta: ChatTurnMeta): TurnFigures {
  const m = complete(meta)
  const thinkMs =
    m.reasoningSource === null || m.ttftMs === null
      ? null
      : m.firstContentMs !== null
        ? m.firstContentMs - m.ttftMs
        : m.elapsedMs !== null
          ? m.elapsedMs - m.ttftMs
          : null
  const genMs = m.elapsedMs !== null && m.ttftMs !== null ? m.elapsedMs - m.ttftMs : null
  const tokPerS =
    m.completionTokens !== null && genMs !== null && genMs > 0
      ? m.completionTokens / (genMs / 1000)
      : null
  return {
    ttftMs: m.ttftMs,
    thinkMs,
    genMs,
    tokPerS,
    promptTokens: m.promptTokens,
    completionTokens: m.completionTokens,
    reasoningTokens: m.reasoningTokens,
    estimated: m.tokensEstimated,
    reasoningEstimated: m.reasoningEstimated,
    finishReason: m.finishReason,
    stopped: m.stopped,
  }
}

export interface SessionFigures {
  /** Assistant turns that finished or were stopped. */
  turns: number
  promptTokens: number | null
  completionTokens: number | null
  reasoningTokens: number | null
  /** Any turn's completion count came from counting frames. */
  estimated: boolean
  meanTtftMs: number | null
  /** Σ completion tokens over Σ decode seconds, over turns that have both --
   *  a per-token rate, not a mean of per-turn rates. */
  tokPerS: number | null
  totalMs: number | null
  /** Prompt + completion of the last exchange that reported both: what the
   *  next prompt will carry, before the new message. */
  contextUsed: number | null
}

const sum = (xs: (number | null)[]): number | null => {
  const present = xs.filter((x): x is number => x !== null)
  return present.length === 0 ? null : present.reduce((a, b) => a + b, 0)
}

export function sessionFigures(turns: Turn[]): SessionFigures {
  const metas = turns
    .filter((t) => t.role === 'assistant' && t.meta !== null)
    .map((t) => complete(t.meta as ChatTurnMeta))
  const ttfts = metas.map((m) => m.ttftMs).filter((x): x is number => x !== null)
  let rateTokens = 0
  let rateSeconds = 0
  for (const m of metas) {
    const f = turnFigures(m)
    if (f.completionTokens !== null && f.genMs !== null && f.genMs > 0) {
      rateTokens += f.completionTokens
      rateSeconds += f.genMs / 1000
    }
  }
  let contextUsed: number | null = null
  for (const m of metas) {
    if (m.promptTokens !== null && m.completionTokens !== null) {
      contextUsed = m.promptTokens + m.completionTokens
    }
  }
  return {
    turns: metas.length,
    promptTokens: sum(metas.map((m) => m.promptTokens)),
    completionTokens: sum(metas.map((m) => m.completionTokens)),
    reasoningTokens: sum(metas.map((m) => m.reasoningTokens)),
    estimated: metas.some((m) => m.completionTokens !== null && m.tokensEstimated),
    meanTtftMs: ttfts.length === 0 ? null : ttfts.reduce((a, b) => a + b, 0) / ttfts.length,
    tokPerS: rateSeconds > 0 ? rateTokens / rateSeconds : null,
    totalMs: sum(metas.map((m) => m.elapsedMs)),
    contextUsed,
  }
}

/** One sentence on whether reasoning came back for this turn and how, or
 *  `null` when there is nothing to say: no request, nothing came, or the turn
 *  never finished (a refusal says nothing about reasoning). */
export function reasoningVerdict(turn: Turn, setup: ReasoningSetup): string | null {
  if (turn.meta === null) return null
  const source = turn.meta.reasoningSource
  const requested = turn.requested
  if (source !== null && requested === 'off') {
    return 'reasoning: arrived although thinking was off — the switch did not take'
  }
  if (source === 'server') {
    return setup.local
      ? `reasoning: separated by the server (parser ${setup.launchedParser ?? 'unknown'})`
      : 'reasoning: returned by the provider as its own stream'
  }
  if (source === 'inline') {
    const split = 'reasoning: arrived inline in the answer text (<think>…</think>) and was split in the browser'
    return setup.local
      ? `${split} — this deployment runs without --reasoning-parser; add "--reasoning-parser ${suggestedParser(setup)}" to its extra args so the server separates it and counts reasoning tokens`
      : `${split} — the provider does not separate it`
  }
  if (requested === 'off') return 'reasoning: none (thinking off)'
  // On, or any level: thinking was asked for.
  if (requested !== null && requested !== 'default') {
    return 'reasoning: nothing came back although thinking was requested — the model may not expose it, or its template ignored the switch'
  }
  return null
}
