// What changed, how a setup reads as chips, and what preloads.
//
//   node src/tabs/models/profiles.check.mjs

import { load, report } from '../../check/harness.mjs'

const M = await load(import.meta.url, './profiles.ts')
const { check, done } = report()

const base = {
  model_id: 'Qwen/Qwen3-8B', context: 32768, concurrency: 8, target: 'cluster', runtime: 'vllm',
  node_ids: ['spark-01', 'spark-02'], kv_dtype: 'fp8',
  speculative: { method: 'eagle3', num_speculative_tokens: 4 },
  extra_args: '--max-num-seqs 8 --disable-log-requests',
  engine_options: { max_num_batched_tokens: 4096, env: { NCCL_MAX_NCHANNELS: '2', NCCL_ALGO: 'Ring' } },
}

check(M.changedKeys(null, base).length === 0, 'planner defaults have nothing to have changed from')
check(M.changedKeys(base, { ...base }).length === 0, 'an identical request changes nothing')
check(
  M.changedKeys(base, { ...base, engine_options: { env: { NCCL_ALGO: 'Ring', NCCL_MAX_NCHANNELS: '2' }, max_num_batched_tokens: 4096 } }).length === 0,
  'key order is not a change',
)
const changed = M.changedKeys(base, {
  ...base,
  kv_dtype: undefined,
  context: 8192,
  engine_options: { ...base.engine_options, max_num_batched_tokens: 8192 },
})
check(changed.join(',') === 'context,kv type,tokens per step', `labels in LABELS order: ${changed.join(',')}`)
check(M.changedKeys(base, { ...base, allow_over_live_memory: true }).length === 0, 'a granted override is not a setting')

const chips = M.profileSummary(base)
check(chips.join('|') === 'vllm|Qwen3-8B|2 nodes|32k·8|kv fp8|eagle3|2 flags|2 env', `chips: ${chips.join('|')}`)
check(M.variantLabel('hf://unsloth/Qwen3-8B-GGUF/Qwen3-8B-Q4_K_M.gguf') === 'Qwen3-8B-Q4_K_M.gguf', 'a GGUF id labels by its file')
check(M.profileSummary({ ...base, node_ids: ['a'], extra_args: '--x' }).includes('1 node'), 'one node is singular')

const prof = (over) => ({ profile_id: 'p', model_id: 'Qwen/Qwen3-8B', name: 'n', request: base, is_default: false, created_at: 0, updated_at: 0, created_by: null, ...over })
const star = M.preloadChoice([prof({ kind: 'run', created_at: 5 }), prof({ is_default: true, name: 'star' })])
check(star.source === 'default' && star.request === base, 'the starred profile wins over a run')
const run = (created_at, ctx) => prof({ kind: 'run', created_at, request: { ...base, context: ctx } })
const last = M.preloadChoice([prof({ kind: 'saved', created_at: 50 }), run(5, 1024), run(9, 2048), run(1, 4096)])
check(last.source === 'last-run' && last.request.context === 2048, 'else the newest run profile')
const saved = M.preloadChoice([prof({ kind: 'saved' })])
check(saved.source === null, 'an unstarred saved profile does not preload')
const none = M.preloadChoice([])
check(none.source === null && none.request === null, 'else planner defaults')

done()
