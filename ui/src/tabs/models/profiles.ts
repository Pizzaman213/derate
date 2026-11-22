/** Saved setups: what changed since one was loaded, how one reads as chips,
 *  and which one the Advanced tab preloads. Pure, so `profiles.check.mjs`
 *  can pin it. */

import type { DeploymentDTO, LaunchRequest, PlanRequest, Profile } from '../../api/types'

/** A label per launch key, in the order "Changed:" lists them. Engine options
 *  are compared per field, under `engine_options.<field>`. */
export const LABELS: Record<string, string> = {
  model_id: 'variant',
  runtime: 'engine',
  target: 'target',
  context: 'context',
  concurrency: 'sequences',
  node_ids: 'machines',
  parallelism: 'parallelism',
  kv_dtype: 'kv type',
  dtype: 'weight scheme',
  speculative: 'speculative',
  speculative_tokens: 'MTP tokens',
  served_name: 'served name',
  extra_args: 'custom flags',
  custom_command: 'custom command',
  enforce_eager: 'eager mode',
  cudagraph_capture_sizes: 'graph sizes',
  trust_remote_code: 'remote code',
  'engine_options.reasoning_parser': 'thinking output',
  'engine_options.tool_call_parser': 'tool calls',
  'engine_options.prefix_caching': 'prefix caching',
  'engine_options.max_num_batched_tokens': 'tokens per step',
  'engine_options.async_scheduling': 'async scheduling',
  'engine_options.limit_mm_per_prompt': 'images per prompt',
  'engine_options.mm_processor_cache_gb': 'image cache',
  'engine_options.language_model_only': 'text only',
  'engine_options.rope_factor': 'YaRN',
  'engine_options.generation_overrides': 'default sampling',
  'engine_options.env': 'environment',
  lifecycle: 'lifecycle',
}

/** Key order is not meaning: `{a,b}` and `{b,a}` are one request. */
function canon(v: unknown): string {
  if (v === undefined) return ''
  if (Array.isArray(v)) return `[${v.map(canon).join(',')}]`
  if (v && typeof v === 'object') {
    const o = v as Record<string, unknown>
    return `{${Object.keys(o)
      .filter((k) => o[k] !== undefined)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${canon(o[k])}`)
      .join(',')}}`
  }
  return JSON.stringify(v)
}

function flat(req: LaunchRequest): Map<string, string> {
  const out = new Map<string, string>()
  for (const [k, v] of Object.entries(req)) {
    if (v === undefined) continue
    // Overrides are permissions granted against one plan, never a setting.
    if (k.startsWith('allow_')) continue
    if (k === 'engine_options' && v && typeof v === 'object') {
      for (const [f, fv] of Object.entries(v)) {
        if (fv !== undefined && fv !== null) out.set(`engine_options.${f}`, canon(fv))
      }
    } else out.set(k, canon(v))
  }
  return out
}

/** The labels of what differs between the loaded request and the current
 *  one, in `LABELS` order. A key with no label is listed by name. No base --
 *  planner defaults -- has nothing to have changed from. */
export function changedKeys(base: LaunchRequest | null, current: LaunchRequest): string[] {
  if (!base) return []
  const a = flat(base)
  const b = flat(current)
  const keys = new Set([...a.keys(), ...b.keys()])
  const order = Object.keys(LABELS)
  const rank = (k: string) => {
    const i = order.indexOf(k)
    return i < 0 ? order.length : i
  }
  return [...keys]
    .filter((k) => a.get(k) !== b.get(k))
    .sort((x, y) => rank(x) - rank(y) || x.localeCompare(y))
    .map((k) => LABELS[k] ?? k)
}

/** The tail of a launch id: a GGUF file, or the repository's own name. */
export function variantLabel(modelId: string): string {
  const parts = modelId.replace(/^hf:\/\//, '').split('/')
  return parts[parts.length - 1] || modelId
}

/** One chip per fact worth seeing at a glance. */
export function profileSummary(req: LaunchRequest): string[] {
  // A stored profile may carry only some keys (the API accepts any subset), so
  // a missing one is left out rather than rendered as "undefined".
  const chips: string[] = [req.runtime, variantLabel(req.model_id)].filter(Boolean)
  const n = req.node_ids?.length ?? 0
  if (n) chips.push(`${n} node${n === 1 ? '' : 's'}`)
  if (req.context) {
    chips.push(`${Math.round(req.context / 1024)}k${req.concurrency ? `·${req.concurrency}` : ''}`)
  } else if (req.concurrency) chips.push(`${req.concurrency} seqs`)
  if (req.kv_dtype) chips.push(`kv ${req.kv_dtype}`)
  if (req.speculative) chips.push(req.speculative.method)
  const flags = req.extra_args ? req.extra_args.trim().split(/\s+/).filter((t) => t.startsWith('-')).length : 0
  if (flags) chips.push(`${flags} flag${flags === 1 ? '' : 's'}`)
  const env = Object.keys(req.engine_options?.env ?? {}).length
  if (env) chips.push(`${env} env`)
  return chips
}

export type PreloadSource = 'default' | 'last-run'

/** What the Advanced tab opens with: the starred profile, else this model's
 *  newest auto-saved run profile, else planner defaults (null). */
export function preloadChoice(
  profiles: Profile[],
): { source: PreloadSource | null; request: LaunchRequest | null } {
  const star = profiles.find((p) => p.is_default)
  if (star) return { source: 'default', request: star.request }
  const newest = profiles
    .filter((p) => p.kind === 'run')
    .sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0))[0]
  if (newest) return { source: 'last-run', request: newest.request }
  return { source: null, request: null }
}

/** The plan request a stored launch request asks the gate, carrying only the
 *  keys `/api/plan` reads. Absent keys stay absent: absence is "planner picks". */
export function planBodyFrom(req: LaunchRequest): PlanRequest {
  const out: PlanRequest = { model_id: req.model_id, target: req.target }
  if (req.context) out.context = req.context
  if (req.concurrency) out.concurrency = req.concurrency
  if (req.runtime) out.runtime = req.runtime
  if (req.dtype) out.dtype = req.dtype
  if (req.node_ids) out.node_ids = req.node_ids
  if (req.parallelism) out.parallelism = req.parallelism
  if (req.speculative) out.speculative = req.speculative
  if (req.speculative_tokens) out.speculative_tokens = req.speculative_tokens
  if (req.kv_dtype) out.kv_dtype = req.kv_dtype
  if (req.engine_options) out.engine_options = req.engine_options
  return out
}

export type ProfilePill = 'saved' | 'previous run' | 'failed run'

/** A run is "failed" when the deployment it recorded is in state failed. */
export function profilePill(p: Profile, deployments: Pick<DeploymentDTO, 'deployment_id' | 'state'>[]): ProfilePill {
  if (p.kind !== 'run') return 'saved'
  const d = p.deployment_id ? deployments.find((x) => x.deployment_id === p.deployment_id) : undefined
  return d?.state === 'failed' ? 'failed run' : 'previous run'
}

/** The benchmark figure the server attached to this setup, or null. */
export function lastResultLine(p: Profile): string | null {
  const r = p.last_result
  if (!r) return null
  const parts: string[] = []
  if (r.tps_each_1 != null) parts.push(`${r.tps_each_1.toFixed(1)} tok/s`)
  if (r.ttft_ms_1 != null) parts.push(`first token ${Math.round(r.ttft_ms_1)} ms`)
  return parts.length ? parts.join(' · ') : null
}
