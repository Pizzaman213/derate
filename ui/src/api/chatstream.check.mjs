// `chatStream` against a real HTTP server speaking the gateway's SSE, frame by
// frame with real gaps between them: the only place the chat wire is read, and
// every figure under an answer comes out of it. Covers server and inline
// reasoning, Stop, and adapted-provider errors before and after partial text.
import { build } from 'esbuild'
import { createServer } from 'node:http'
import { fileURLToPath } from 'node:url'
import { report } from '../check/harness.mjs'

// One bundle for both modules: `setCoordinatorBase` has to move the base the
// client's own `apiUrl` reads, and two `load()` calls would be two copies.
const out = await build({
  stdin: {
    contents: "export { httpBackend, ApiError } from './client'\nexport { setCoordinatorBase } from './origin'\n",
    resolveDir: fileURLToPath(new URL('.', import.meta.url)),
    loader: 'ts',
  },
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'neutral',
  logLevel: 'silent',
})
const M = await import('data:text/javascript;base64,' + Buffer.from(out.outputFiles[0].text).toString('base64'))
const { check, note, done } = report()

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const frame = (o) => `data: ${JSON.stringify(o)}\n\n`
const delta = (d, finish = null) => ({ choices: [{ index: 0, delta: d, finish_reason: finish }] })
const refusal = 'Claude refused this request.\nChoose another model: café 🛑.'
const secret = `derate-abcdefghij_${'x'.repeat(32)}`
const truncated = `Claude stream ended before message_stop.\nRequest credential: ${secret}`

const SCRIPTS = {
  server: [
    delta({ role: 'assistant' }),
    delta({ reasoning: 'Two ' }),
    delta({ reasoning: 'plus two ' }),
    delta({ reasoning: 'is four.' }),
    delta({ content: 'It is ' }),
    delta({ content: '4.' }),
    delta({}, 'stop'),
    { choices: [], usage: { prompt_tokens: 512, completion_tokens: 318, completion_tokens_details: { reasoning_tokens: 214 } } },
  ],
  inline: [
    delta({ role: 'assistant' }),
    delta({ content: '<think>\n' }),
    delta({ content: 'x' }),
    delta({ content: '</think>\n\nY' }),
    delta({}, 'stop'),
  ],
  reasoning_content: [
    delta({ reasoning_content: 'hm' }),
    delta({ content: 'ok' }),
    delta({}, 'length'),
  ],
  // The template opened the block in the prompt: no `<think>`, a bare close.
  implicit: [
    delta({ content: 'Let me' }),
    delta({ content: ' think.' }),
    delta({ content: '\n</think>\n\n' }),
    delta({ content: 'Hi' }),
    delta({}, 'stop'),
  ],
  plain: [
    delta({ content: 'Hello' }),
    delta({ content: ' there' }),
    delta({}, 'stop'),
  ],
  slow: [
    delta({ content: 'a' }),
    delta({ content: 'b' }),
    delta({ content: 'c' }),
    delta({ content: 'd' }),
  ],
  error_before: [
    { error: { message: refusal, type: 'permission_error', code: 'permission_denied' } },
    delta({ content: 'This must not appear.' }, 'stop'),
    { choices: [], usage: { prompt_tokens: 100, completion_tokens: 200 } },
  ],
  error_partial: [
    delta({ content: 'Partial answer' }),
    // Pending inline markup must not be flushed as if the answer completed.
    delta({ content: '</thi' }),
    { error: { message: truncated, type: 'api_error', code: 'upstream_stream_truncated', access_token: secret } },
    delta({ content: 'This must not appear.' }, 'stop'),
    { choices: [], usage: { prompt_tokens: 100, completion_tokens: 200 } },
  ],
}

let lastBody = null
const server = createServer(async (req, res) => {
  let body = ''
  for await (const chunk of req) body += chunk
  lastBody = JSON.parse(body)
  const script = SCRIPTS[lastBody.model]
  res.writeHead(200, { 'content-type': 'text/event-stream', 'X-Request-Id': 'r-abc' })
  if (lastBody.model.startsWith('error_')) {
    // Real HTTP chunks split every SSE/JSON character, CRLF pair and UTF-8
    // sequence. Include trailing success frames: an error must win over them.
    const wire = Buffer.from((script.map(frame).join('') + 'data: [DONE]\n\n').replace(/\n/g, '\r\n'))
    for (const byte of wire) {
      if (res.destroyed) return
      res.write(Buffer.from([byte]))
      await sleep(1)
    }
    res.end()
    return
  }
  for (const f of script) {
    if (res.destroyed) return
    res.write(frame(f))
    await sleep(lastBody.model === 'slow' ? 150 : 20)
  }
  res.end('data: [DONE]\n\n')
})
await new Promise((r) => server.listen(0, '127.0.0.1', r))
M.setCoordinatorBase(`http://127.0.0.1:${server.address().port}`)

async function run(model, { options, abortAfter, thinkOpen } = {}) {
  let reasoning = ''
  let content = ''
  let firstReasoning = null
  let requestId = null
  let meta = null
  let error = null
  const ctl = new AbortController()
  let deltas = 0
  try {
    meta = await M.httpBackend.chatStream({
      model,
      messages: [{ role: 'user', content: 'hi' }],
      options,
      thinkOpen,
      onOpen: (id) => { requestId = id },
      onReasoning: (t) => {
        reasoning += t
        firstReasoning ??= t
      },
      onRewrite: (r, c) => {
        reasoning = r
        content = c
      },
      onDelta: (t) => {
        content += t
        deltas += 1
        if (abortAfter && deltas === abortAfter) ctl.abort()
      },
      signal: ctl.signal,
    })
  } catch (e) {
    // The same public boundary Chat uses: keep partial deltas, show the thrown
    // sentence, and attach successful metadata only when the promise resolves.
    error = e
  }
  return { meta, error, requestId, reasoning, content, firstReasoning }
}

{
  const opts = { chat_template_kwargs: { enable_thinking: true, thinking: true } }
  const { meta, reasoning, content } = await run('server', { options: opts })
  note(`server: ttft ${meta.ttftMs?.toFixed(0)} ms, first content ${meta.firstContentMs?.toFixed(0)} ms, elapsed ${meta.elapsedMs?.toFixed(0)} ms`)
  check(JSON.stringify(lastBody.chat_template_kwargs) === JSON.stringify(opts.chat_template_kwargs) && lastBody.stream === true,
    'options are spread into the request body beside stream:true')
  check(reasoning === 'Two plus two is four.', `delta.reasoning reaches onReasoning (${JSON.stringify(reasoning)})`)
  check(content === 'It is 4.', `delta.content reaches onDelta (${JSON.stringify(content)})`)
  check(meta.reasoningSource === 'server', 'reasoningSource is server')
  check(meta.ttftMs !== null && meta.firstContentMs !== null && meta.ttftMs < meta.firstContentMs, 'ttft (first reasoning) comes before the first answer text')
  check(meta.promptTokens === 512 && meta.completionTokens === 318 && meta.reasoningTokens === 214,
    `usage read: in ${meta.promptTokens} / out ${meta.completionTokens} / reasoning ${meta.reasoningTokens}`)
  check(meta.tokensEstimated === false && meta.reasoningEstimated === false, 'a usage block makes both counts measured')
  check(meta.finishReason === 'stop', `finish_reason read (${meta.finishReason})`)
  check(meta.requestId === 'r-abc', 'X-Request-Id read')
  check(meta.stopped === false, 'a stream that ran to [DONE] is not stopped')
}
{
  const { meta, reasoning, content } = await run('inline')
  check(!('chat_template_kwargs' in lastBody), 'no options sends no extra fields')
  check(meta.reasoningSource === 'inline', 'inline <think> tags mark the source inline')
  check(reasoning === 'x' && content === 'Y', `inline block split out (${JSON.stringify({ reasoning, content })})`)
  check(meta.reasoningTokens === 2 && meta.reasoningEstimated === true, `reasoning frames counted (${meta.reasoningTokens}, est.)`)
  check(meta.completionTokens === 3 && meta.tokensEstimated === true, `completion frames counted (${meta.completionTokens}, est.)`)
  check(meta.promptTokens === null, 'no usage block leaves prompt tokens unknown, not 0')
}
{
  const { meta, content } = await run('slow', { abortAfter: 2 })
  check(meta.stopped === true && content === 'ab', `abort mid-stream resolves stopped with the partial text (${JSON.stringify(content)})`)
  check(meta.elapsedMs > 0, 'a stopped turn still has an elapsed time')
}
{
  const { meta, reasoning, content } = await run('reasoning_content')
  check(meta.reasoningSource === 'server' && reasoning === 'hm' && content === 'ok', 'delta.reasoning_content is read as server-separated reasoning')
  check(meta.finishReason === 'length', 'finish_reason length read')
}
{
  // No thinkOpen: the reasoning streams as the answer until the bare close,
  // then the rewrite moves it.
  const { meta, reasoning, content } = await run('implicit')
  check(reasoning === 'Let me think.' && content === 'Hi', `a bare </think> rewrites the split (${JSON.stringify({ reasoning, content })})`)
  check(meta.reasoningSource === 'inline', 'and marks the source inline')
  check(meta.reasoningTokens === 3 && meta.completionTokens === 4, `frames before the close are recounted as reasoning (${meta.reasoningTokens} of ${meta.completionTokens})`)
  check(meta.firstContentMs !== null && meta.ttftMs < meta.firstContentMs, 'first content is the answer after the close, not the first reasoning token')
}
{
  const { meta, reasoning, content, firstReasoning } = await run('implicit', { thinkOpen: true })
  check(firstReasoning === 'Let me', 'thinkOpen streams the reasoning into its own channel from the first token')
  check(reasoning.trimEnd() === 'Let me think.' && content === 'Hi', `thinkOpen splits at the close (${JSON.stringify({ reasoning, content })})`)
  check(meta.reasoningSource === 'inline' && meta.reasoningTokens === 3 && meta.completionTokens === 4, 'thinkOpen counts the same frames')
}
{
  const { meta, reasoning, content } = await run('plain', { thinkOpen: true })
  check(reasoning === '' && content === 'Hello there', `thinkOpen on an answer that never thought hands it all back (${JSON.stringify({ reasoning, content })})`)
  check(meta.reasoningSource === null && meta.reasoningTokens === null && meta.completionTokens === 2, 'and leaves no reasoning on the record')
  check(meta.firstContentMs === meta.ttftMs, 'its first content is its first token')
}
{
  // A server that separates reasoning bypasses the splitter: expecting an
  // inline block must not wipe its answer at the end of the stream.
  const { meta, reasoning, content } = await run('server', { thinkOpen: true })
  check(meta.reasoningSource === 'server' && reasoning === 'Two plus two is four.' && content === 'It is 4.',
    `thinkOpen leaves server-separated reasoning and its answer alone (${JSON.stringify({ reasoning, content })})`)
}
{
  const { meta, error, content, reasoning, requestId } = await run('error_before')
  check(error instanceof M.ApiError && error.message === refusal,
    'an error before content rejects with the exact upstream sentence, including newlines and split UTF-8')
  check(content === '' && reasoning === '' && meta === null,
    'a refused turn has no blank-success metadata, synthesized usage, or trailing content')
  check(requestId === 'r-abc', 'an in-stream refusal retains the request id for the failed turn')
}
{
  const { meta, error, content, reasoning } = await run('error_partial')
  check(error instanceof M.ApiError && error.message === truncated.replace(secret, '***'),
    'an error after partial content rejects with the redacted upstream sentence')
  check(content === 'Partial answer' && reasoning === '' && meta === null,
    'a failed partial turn keeps delivered text without flushing pending markup or synthesizing successful usage')
  check(error instanceof M.ApiError && !error.body.includes(secret)
    && JSON.parse(error.body).error.access_token === '***',
    'the error body follows normal API credential redaction too')
}

server.close()
done()
