// The arithmetic behind every figure under a chat answer and in the session
// strip, and the verdict sentence that says whether reasoning came back. A
// rate divided by the wrong interval, or a zero where a dash belongs, is a
// plausible number on screen and nothing else will catch it.
import { load, report } from '../../check/harness.mjs'

const S = await load(import.meta.url, './stats.ts')
const R = await load(import.meta.url, './reasoning.ts')
const { check, done } = report()

const meta = (o = {}) => ({
  model: 'm', requestId: 'r', ttftMs: null, firstContentMs: null, elapsedMs: null,
  promptTokens: null, completionTokens: null, reasoningTokens: null,
  tokensEstimated: false, reasoningEstimated: false, reasoningSource: null,
  finishReason: null, stopped: false, ...o,
})
const turn = (m, requested = 'default') => ({
  key: 0, role: 'assistant', content: '', reasoning: '', requested, meta: m,
  error: null, requestId: null, audio: null, model: 'm',
})

{
  const f = S.turnFigures(meta({ ttftMs: 100, firstContentMs: 2200, elapsedMs: 6200, completionTokens: 305, reasoningSource: 'server' }))
  check(f.thinkMs === 2100, `think time is first content minus first token (${f.thinkMs})`)
  check(f.genMs === 6100, `decode time is elapsed minus ttft (${f.genMs})`)
  check(Math.abs(f.tokPerS - 305 / 6.1) < 0.01, `tok/s over decode time (${f.tokPerS})`)
}
{
  const f = S.turnFigures(meta({ ttftMs: 100, elapsedMs: 900, reasoningSource: 'inline' }))
  check(f.thinkMs === 800, 'a stream that ended still thinking spent all of its decode time thinking')
}
{
  const f = S.turnFigures(meta({ ttftMs: 100, firstContentMs: 100, elapsedMs: 100, completionTokens: 1 }))
  check(f.thinkMs === null, 'no reasoning, no think time')
  check(f.genMs === 0 && f.tokPerS === null, 'zero decode time gives no rate, not Infinity')
}
check(S.turnFigures(meta()).tokPerS === null && S.turnFigures(meta()).genMs === null, 'nothing measured, nothing derived')

{
  const turns = [
    { ...turn(null), role: 'user' },
    turn(meta({ ttftMs: 100, elapsedMs: 1100, promptTokens: 50, completionTokens: 100, reasoningTokens: 40 })),
    { ...turn(null), role: 'user' },
    turn(meta({ ttftMs: 300, elapsedMs: 2300, promptTokens: 170, completionTokens: 200, tokensEstimated: true })),
    turn(null), // still streaming
  ]
  const s = S.sessionFigures(turns)
  check(s.turns === 2, `only finished assistant turns count (${s.turns})`)
  check(s.promptTokens === 220 && s.completionTokens === 300, `tokens summed (${s.promptTokens} in / ${s.completionTokens} out)`)
  check(s.reasoningTokens === 40, 'reasoning summed over the turns that had it')
  check(s.estimated === true, 'one estimated turn marks the session estimated')
  check(s.meanTtftMs === 200, `mean ttft (${s.meanTtftMs})`)
  check(Math.abs(s.tokPerS - 300 / 3) < 1e-9, `session rate is Σtokens / Σdecode seconds (${s.tokPerS})`)
  check(s.totalMs === 3400, `total elapsed (${s.totalMs})`)
  check(s.contextUsed === 370, `context used is the last exchange's prompt + completion (${s.contextUsed})`)
}
{
  const s = S.sessionFigures([])
  check(s.turns === 0 && s.promptTokens === null && s.tokPerS === null && s.contextUsed === null, 'an empty session is all dashes')
}

const dep = (extra_args) => ({ deployment_id: 'd', served_name: 'x', model_id: 'Qwen/Qwen3-8B', extra_args })
const withParser = R.reasoningSetup('x', dep(['--reasoning-parser', 'qwen3']), false)
const without = R.reasoningSetup('x', dep([]), false)

check(S.reasoningVerdict(turn(meta({ reasoningSource: 'server' }), 'on'), withParser) === 'reasoning: separated by the server (parser qwen3)', 'server verdict names the parser')
check(S.reasoningVerdict(turn(meta({ reasoningSource: 'server' })), without) === 'reasoning: separated by the server (parser unknown)', 'server verdict with no flag on record says unknown')
check(S.reasoningVerdict(turn(meta({ reasoningSource: 'inline' }), 'on'), without) === 'reasoning: arrived inline in the answer text (<think>…</think>) and was split in the browser — this deployment runs without --reasoning-parser; add "--reasoning-parser qwen3" to its extra args so the server separates it and counts reasoning tokens', 'inline verdict names the parser to add')
check(S.reasoningVerdict(turn(meta(), 'high'), without) === 'reasoning: nothing came back although thinking was requested — the model may not expose it, or its template ignored the switch', 'requested but nothing came')
check(S.reasoningVerdict(turn(meta(), 'off'), without) === 'reasoning: none (thinking off)', 'off and nothing came')
check(S.reasoningVerdict(turn(meta({ reasoningSource: 'server' }), 'off'), withParser) === 'reasoning: arrived although thinking was off — the switch did not take', 'off but it reasoned anyway')
check(S.reasoningVerdict(turn(meta(), 'default'), without) === null && S.reasoningVerdict(turn(meta(), null), without) === null, 'default and nothing came: no line')
check(S.reasoningVerdict(turn(null, 'on'), without) === null, 'a turn that never finished gets no verdict')
const provider = R.reasoningSetup('gpt-6.2-sol', null, true)
check(S.reasoningVerdict(turn(meta({ reasoningSource: 'server' }), 'high'), provider) === 'reasoning: returned by the provider as its own stream', 'provider reasoning is credited to the provider, not a parser')
check(S.reasoningVerdict(turn(meta({ reasoningSource: 'inline' })), provider) === 'reasoning: arrived inline in the answer text (<think>…</think>) and was split in the browser — the provider does not separate it',
  'inline reasoning from a provider does not tell anyone to add a launch flag')

done()
