import type { ChatRequestOptions, DeploymentDTO, ThinkingChoice, ThinkingLevel } from '../../api/types'

// How the picked model is set up to think, and what the composer's thinking
// control may send it.
//
// Two facts, both already fetched by the tab: the model's FAMILY, from its
// HuggingFace id, which decides which per-request switch its chat template
// reads; and the deployment's LAUNCH FLAGS, which say whether the runtime was
// started with `--reasoning-parser` and so whether it separates the reasoning
// or leaves `<think>` tags inline. Neither is asked of the runtime. The family
// table is vLLM's documented behaviour per family, and when it is wrong for a
// checkpoint the verdict line under each answer (stats.ts) is what shows it --
// fix the row here, never the verdict.

export type ReasoningProfile =
  | { kind: 'toggle'; family: string; key: 'enable_thinking' | 'thinking'; defaultOn: boolean; parser: string }
  | { kind: 'effort'; family: string; parser: string | null }
  | { kind: 'always'; family: string; parser: string }
  | { kind: 'none'; family: null; parser: null }

export interface ReasoningSetup {
  profile: ReasoningProfile
  /** The value after `--reasoning-parser` (space or `=` form) in the
   *  deployment's extra_args/custom_command; null when absent or remote. */
  launchedParser: string | null
  /** Served by a deployment on this cluster. Template switches are only sent
   *  to one: a remote OpenAI-compatible provider refuses unknown fields. */
  local: boolean
}

const NONE: ReasoningProfile = { kind: 'none', family: null, parser: null }

/** Ordered: first match wins, so the specific Qwen3 variants sit above the
 *  general row. Parser names are the ones registered in the shipped image. */
const FAMILIES: readonly (readonly [RegExp, ReasoningProfile])[] = [
  [/qwen3[^/]*-thinking/i, { kind: 'always', family: 'Qwen3 Thinking', parser: 'qwen3' }],
  [/qwen3[^/]*-instruct-2507/i, NONE],
  [/qwen3/i, { kind: 'toggle', family: 'Qwen3', key: 'enable_thinking', defaultOn: true, parser: 'qwen3' }],
  [/qwq/i, { kind: 'always', family: 'QwQ', parser: 'deepseek_r1' }],
  [/deepseek-r1/i, { kind: 'always', family: 'DeepSeek-R1', parser: 'deepseek_r1' }],
  [/deepseek-v3\.[12]/i, { kind: 'toggle', family: 'DeepSeek-V3.1/3.2', key: 'thinking', defaultOn: false, parser: 'deepseek_v3' }],
  [/gpt-oss/i, { kind: 'effort', family: 'gpt-oss', parser: 'openai_gptoss' }],
  [/gemma-?4/i, { kind: 'toggle', family: 'Gemma 4', key: 'enable_thinking', defaultOn: false, parser: 'gemma4' }],
  [/granite-4/i, { kind: 'toggle', family: 'Granite 4', key: 'enable_thinking', defaultOn: true, parser: 'granite' }],
  [/granite-3/i, { kind: 'toggle', family: 'Granite 3', key: 'thinking', defaultOn: false, parser: 'granite' }],
  [/glm-4\.[56]/i, { kind: 'toggle', family: 'GLM-4.5/4.6', key: 'enable_thinking', defaultOn: true, parser: 'glm45' }],
  [/glm-4\.7/i, { kind: 'toggle', family: 'GLM-4.7', key: 'enable_thinking', defaultOn: true, parser: 'glm47' }],
  [/kimi-k2[^/]*thinking/i, { kind: 'always', family: 'Kimi K2 Thinking', parser: 'kimi_k2' }],
  [/magistral/i, { kind: 'always', family: 'Magistral', parser: 'mistral' }],
  [/^(openai\/)?(o[1-4](-|$)|gpt-5)/i, { kind: 'effort', family: 'OpenAI reasoning', parser: null }],
]

export function profileFor(modelId: string): ReasoningProfile {
  for (const [re, profile] of FAMILIES) if (re.test(modelId)) return profile
  return NONE
}

const FLAG = '--reasoning-parser'

export function launchedParser(
  d: Pick<DeploymentDTO, 'extra_args' | 'custom_command'> | null,
): string | null {
  if (!d) return null
  for (const tokens of [d.extra_args, d.custom_command]) {
    if (!tokens) continue
    for (const [i, t] of tokens.entries()) {
      if (t === FLAG) return tokens[i + 1] ?? null
      if (t.startsWith(`${FLAG}=`)) return t.slice(FLAG.length + 1) || null
    }
  }
  return null
}

/** `remote`: the row is served by a provider and no deployment backs it. The
 *  family is read off the HF id for a local deployment (its served name can be
 *  anything) and off the served name for a provider row (it has nothing else). */
export function reasoningSetup(
  served: string | null,
  deployment: DeploymentDTO | null,
  remote: boolean,
): ReasoningSetup {
  const local = deployment !== null && !remote
  const id = local ? deployment.model_id : (served ?? '')
  return {
    profile: profileFor(id),
    launchedParser: local ? launchedParser(deployment) : null,
    local,
  }
}

/** What each level caps thinking at, in tokens, when the deployment can
 *  enforce it: vLLM's `thinking_token_budget`, which it refuses without a
 *  `--reasoning-parser` (it needs the parser to know where thinking ends).
 *  `max` is uncapped: thinking on, as long as the model wants. */
export const THINKING_BUDGET: Record<ThinkingLevel, number | null> = {
  minimal: 256,
  low: 1024,
  medium: 4096,
  high: 16384,
  xhigh: 32768,
  max: null,
}

/** Every text model gets every control. Which of them a model honours is not
 *  knowable up front -- a template ignores a switch it does not declare, a
 *  provider may refuse a level -- so the control is offered everywhere and
 *  the verdict under each answer says what happened. The levels are the
 *  values vLLM's `reasoning_effort` accepts, in its order. */
export const THINKING_CHOICES: readonly ThinkingChoice[] = [
  'default',
  'off',
  'on',
  'minimal',
  'low',
  'medium',
  'high',
  'xhigh',
  'max',
]

export function requestOptions(s: ReasoningSetup, c: ThinkingChoice): ChatRequestOptions | undefined {
  if (c === 'default') return undefined
  // A provider takes no template switch -- OpenAI and strict servers refuse an
  // unknown field -- but `reasoning_effort` is OpenAI's own, and OpenRouter,
  // Groq, Ollama and vLLM read it too; the ChatGPT adapter turns it into
  // `reasoning.effort` and asks for a summary. Off is `none`, on is the
  // providers' own default level, a level is sent as spelled.
  if (!s.local) return { reasoning_effort: c === 'off' ? 'none' : c === 'on' ? 'medium' : c }
  if (c === 'on' || c === 'off') {
    // An effort ladder (gpt-oss) has no off; on is its middle level.
    if (s.profile.kind === 'effort' && c === 'on') return { reasoning_effort: 'medium' }
    // Both keys with one value: vLLM ORs them, and each template reads only
    // its own, so this is right whichever one the family uses.
    return { chat_template_kwargs: { enable_thinking: c === 'on', thinking: c === 'on' } }
  }
  // An effort ladder (gpt-oss) reads the level natively.
  if (s.profile.kind === 'effort') return { reasoning_effort: c }
  // Anything else: thinking on, the level handed to the template as
  // `reasoning_effort` (vLLM passes it through; a template that declares it
  // reads it, most do not), and -- where the server can enforce one -- the
  // level's hard token cap.
  const cap = THINKING_BUDGET[c]
  return {
    chat_template_kwargs: { enable_thinking: true, thinking: true },
    reasoning_effort: c,
    ...(s.launchedParser !== null && cap !== null ? { thinking_token_budget: cap } : {}),
  }
}

/** Whether this request's answer should be read as starting inside a
 *  `<think>` block: the model is expected to think (it always does, or its
 *  switch is on, sent or by default) and no reasoning parser on the server
 *  will take the block out first. Many current templates open the block in
 *  the prompt, so the output carries only `</think>`; starting inside lets the
 *  reasoning stream into its own block from the first token. A wrong guess is
 *  repaired at the end of the stream (see `api/think.ts`). */
export function expectsInlineThinking(s: ReasoningSetup, c: ThinkingChoice): boolean {
  if (!s.local || s.launchedParser !== null) return false
  const p = s.profile
  if (p.kind === 'always') return true
  if (p.kind !== 'toggle') return false
  return c !== 'off' && (c !== 'default' || p.defaultOn)
}

export function suggestedParser(s: ReasoningSetup): string {
  return s.profile.parser ?? '<name>'
}

/** The chat head's sentence: what the control does for this model and how its
 *  deployment hands reasoning back. */
export function describeSetup(s: ReasoningSetup): string {
  const p = s.profile
  const parser = s.launchedParser
  if (!s.local) {
    // No launch flags to read and no template to switch: one OpenAI field,
    // and the provider decides what it does with it.
    return `Thinking: ${p.family ? `${p.family} ` : ''}through a provider — off · on · levels send reasoning_effort (none · medium · the level as spelled); the provider decides whether it takes the level and shows its thinking`
  }
  const caps = (Object.entries(THINKING_BUDGET) as [ThinkingLevel, number | null][])
    .map(([level, n]) => `${level} ${n === null ? 'uncapped' : n < 1024 ? n : `${n / 1024}k`}`)
    .join(' · ')
  const levels =
    parser !== null
      ? `levels cap thinking: ${caps}`
      : 'levels send reasoning_effort only — a token cap needs --reasoning-parser'
  switch (p.kind) {
    case 'toggle':
      return `Thinking: ${p.family} switch (${p.key}, model default ${p.defaultOn ? 'on' : 'off'}) · ${levels} · parser: ${parser ?? 'none — reasoning arrives inline'}`
    case 'effort':
      return `Thinking: ${p.family} effort ladder low · medium · high (reasoning_effort), on is medium, no off · parser: ${parser ?? 'none'}`
    case 'always':
      return `Thinking: ${p.family} always thinks, off has no effect · ${levels} · parser: ${parser ?? 'none — reasoning arrives inline'}`
    case 'none':
      return `Thinking: no known switch for this model — the controls are sent anyway; the line under each answer says whether they took · ${levels} · parser: ${parser ?? 'none'}`
  }
}
