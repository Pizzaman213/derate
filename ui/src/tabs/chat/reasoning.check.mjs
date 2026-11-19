// What the chat composer's thinking control offers and sends, per model. A
// wrong family row sends a switch the template never reads; a wrong options
// shape is a 400 from vLLM or, sent to a provider, a refusal for an unknown
// field. Both are invisible to types: every value here is a valid string.
import { load, report } from '../../check/harness.mjs'

const M = await load(import.meta.url, './reasoning.ts')
const { check, done } = report()

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b)

{
  const p = M.profileFor('Qwen/Qwen3-8B')
  check(p.kind === 'toggle' && p.key === 'enable_thinking' && p.defaultOn === true && p.parser === 'qwen3', 'Qwen3-8B: enable_thinking switch, default on, parser qwen3')
}
check(M.profileFor('Qwen/Qwen3-235B-A22B-Thinking-2507').kind === 'always', 'Qwen3 Thinking-2507 always thinks (no switch)')
check(M.profileFor('Qwen/Qwen3-30B-A3B-Instruct-2507').kind === 'none', 'Qwen3 Instruct-2507 never thinks')
check(M.profileFor('openai/gpt-oss-20b').kind === 'effort', 'gpt-oss is an effort ladder')
check(M.profileFor('Qwen/Qwen2.5-0.5B-Instruct').kind === 'none', 'Qwen2.5 has no known switch')
check(M.profileFor('deepseek-ai/DeepSeek-V3.1').key === 'thinking', 'DeepSeek-V3.1 reads the `thinking` key')
check(M.profileFor('o3-mini').kind === 'effort' && M.profileFor('openai/o4-mini').kind === 'effort', 'OpenAI o-series is an effort ladder')
check(M.profileFor('olmo-3').kind === 'none', 'a name merely containing o3 is not the o-series')

check(M.launchedParser({ extra_args: ['--reasoning-parser', 'qwen3'] }) === 'qwen3', 'parser read from the space form')
check(M.launchedParser({ extra_args: ['--max-num-seqs', '4', '--reasoning-parser=qwen3'] }) === 'qwen3', 'parser read from the = form')
check(M.launchedParser({ custom_command: ['vllm', 'serve', 'x', '--reasoning-parser', 'deepseek_r1'] }) === 'deepseek_r1', 'parser read from a custom command')
check(M.launchedParser({ extra_args: [] }) === null && M.launchedParser({}) === null && M.launchedParser(null) === null, 'no flag, no field, no deployment: null')

const dep = (model_id, extra_args = []) => ({ deployment_id: 'd', served_name: 'x', model_id, extra_args })
const localQwen3 = M.reasoningSetup('Qwen3-8B', dep('Qwen/Qwen3-8B'), false)
const remoteO3 = M.reasoningSetup('o3', null, true)
const gptOss = M.reasoningSetup('gpt-oss-20b', dep('openai/gpt-oss-20b', ['--reasoning-parser', 'openai_gptoss']), false)
const remoteQwen = M.reasoningSetup('qwen/qwen3-32b', null, true)
const remoteNone = M.reasoningSetup('mistral-small', null, true)
const localNone = M.reasoningSetup('tiny', dep('Qwen/Qwen2.5-0.5B-Instruct'), false)

check(localQwen3.local && localQwen3.profile.kind === 'toggle', 'a local deployment is profiled from its HF id, not its served name')
check(gptOss.launchedParser === 'openai_gptoss', 'the deployment launch flags reach the setup')
check(same(M.requestOptions(localQwen3, 'off'), { chat_template_kwargs: { enable_thinking: false, thinking: false } }), 'local off sends both template keys false')
check(same(M.requestOptions(localQwen3, 'on'), { chat_template_kwargs: { enable_thinking: true, thinking: true } }), 'local on sends both template keys true')
check(M.requestOptions(localQwen3, 'default') === undefined, 'default sends nothing')
check(same(M.requestOptions(remoteQwen, 'on'), { reasoning_effort: 'medium' }) && !('chat_template_kwargs' in M.requestOptions(remoteQwen, 'off')),
  'a template switch is never sent to a provider; on is reasoning_effort medium')
check(same(M.requestOptions(remoteNone, 'off'), { reasoning_effort: 'none' }), 'off on a provider is reasoning_effort none')
check(same(M.requestOptions(remoteNone, 'high'), { reasoning_effort: 'high' }), 'a level on any provider row is reasoning_effort')
check(same(M.requestOptions(gptOss, 'high'), { reasoning_effort: 'high' }), 'effort sends reasoning_effort')
check(same(M.requestOptions(gptOss, 'on'), { reasoning_effort: 'medium' }), 'on for an effort ladder is its middle level')
check(same(M.requestOptions(remoteO3, 'low'), { reasoning_effort: 'low' }), 'effort is sent to a provider o-series row')

const qwenWithParser = M.reasoningSetup('x', dep('Qwen/Qwen3-8B', ['--reasoning-parser', 'qwen3']), false)
const r1 = M.reasoningSetup('x', dep('deepseek-ai/DeepSeek-R1-0528'), false)

check(same(M.requestOptions(localQwen3, 'medium'), { chat_template_kwargs: { enable_thinking: true, thinking: true }, reasoning_effort: 'medium' }),
  'a level without a parser turns thinking on and sends the level, but no token cap vLLM would refuse')
check(same(M.requestOptions(qwenWithParser, 'low'), { chat_template_kwargs: { enable_thinking: true, thinking: true }, reasoning_effort: 'low', thinking_token_budget: 1024 }),
  'a level with a parser adds the hard token cap')
const caps = ['minimal', 'low', 'medium', 'high', 'xhigh'].map((l) => M.THINKING_BUDGET[l])
check(caps.every((n, i) => i === 0 || n > caps[i - 1]) && M.requestOptions(qwenWithParser, 'xhigh').thinking_token_budget === M.THINKING_BUDGET.xhigh,
  `the caps rise with the level (${caps.join(' < ')})`)
check(same(M.requestOptions(qwenWithParser, 'max'), { chat_template_kwargs: { enable_thinking: true, thinking: true }, reasoning_effort: 'max' }),
  'max is thinking on with no cap, even where a cap could be enforced')
check(same(M.requestOptions(remoteNone, 'xhigh'), { reasoning_effort: 'xhigh' }) && same(M.requestOptions(remoteNone, 'minimal'), { reasoning_effort: 'minimal' }),
  'a provider gets the level exactly as spelled')
check(!('thinking_token_budget' in M.requestOptions(gptOss, 'high')), 'an effort ladder reads the level natively and gets no cap')

check(same(M.THINKING_CHOICES, ['default', 'off', 'on', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']), 'every text model is offered off, on and every reasoning_effort level vLLM accepts')

check(M.expectsInlineThinking(localQwen3, 'high'), 'a level is thinking on')
check(M.expectsInlineThinking(localQwen3, 'default') && M.expectsInlineThinking(localQwen3, 'on'), 'a default-on Qwen3 without a parser is read as starting inside the block')
check(!M.expectsInlineThinking(localQwen3, 'off'), 'not when thinking was switched off')
check(!M.expectsInlineThinking(qwenWithParser, 'on'), 'not when the server parser takes the block out first')
check(M.expectsInlineThinking(r1, 'default'), 'an always-thinking model is')
check(!M.expectsInlineThinking(remoteQwen, 'default') && !M.expectsInlineThinking(localNone, 'on') && !M.expectsInlineThinking(gptOss, 'high'),
  'not for a provider row, an unknown family, or an effort ladder')
check(M.describeSetup(localQwen3) === 'Thinking: Qwen3 switch (enable_thinking, model default on) · levels send reasoning_effort only — a token cap needs --reasoning-parser · parser: none — reasoning arrives inline', 'head sentence for a local Qwen3 without a parser')
check(M.describeSetup(qwenWithParser) === 'Thinking: Qwen3 switch (enable_thinking, model default on) · levels cap thinking: minimal 256 · low 1k · medium 4k · high 16k · xhigh 32k · max uncapped · parser: qwen3', 'head sentence for a local Qwen3 with a parser')
check(M.describeSetup(gptOss) === 'Thinking: gpt-oss effort ladder low · medium · high (reasoning_effort), on is medium, no off · parser: openai_gptoss', 'head sentence for gpt-oss with its parser')
check(M.describeSetup(remoteNone) === 'Thinking: through a provider — off · on · levels send reasoning_effort (none · medium · the level as spelled); the provider decides whether it takes the level and shows its thinking', 'head sentence for an unknown provider model')
check(M.describeSetup(remoteQwen).startsWith('Thinking: Qwen3 through a provider'), 'a provider row still names a family it recognises')
check(M.suggestedParser(localQwen3) === 'qwen3' && M.suggestedParser(localNone) === '<name>', 'suggested parser from the family, placeholder when unknown')

done()
