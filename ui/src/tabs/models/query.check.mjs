import { load, report } from '../../check/harness.mjs'
const M = await load(import.meta.url, './query.ts')
const { check, done } = report()

const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b)
check(eq(M.parseHfLink('https://huggingface.co/Qwen/Qwen3-8B'), { repo: 'Qwen/Qwen3-8B', quant: null }), 'a plain hub link')
check(M.parseHfLink('hf.co/unsloth/Qwen3-8B-GGUF:Q4_K_M')?.quant === 'Q4_K_M', 'hf.co with a quant')
check(M.parseHfLink('hf.co/unsloth/Qwen3-8B-GGUF:q4_k_m')?.quant === 'Q4_K_M', 'the quant is upper-cased')
check(M.parseHfLink('https://huggingface.co/Qwen/Qwen3-8B/tree/main')?.repo === 'Qwen/Qwen3-8B', 'a page inside the repo names the repo')
check(M.parseHfLink('huggingface.co/models/x') === null, '/models is not an owner')
check(M.parseHfLink('huggingface.co/datasets/a/b') === null, '/datasets is not an owner')
check(M.parseHfLink('Qwen/Qwen3-8B') === null, 'a bare id is not a link')
const gg = M.parseHfLink('https://huggingface.co/unsloth/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-UD-Q4_K_XL.gguf')
check(eq(gg, { repo: 'unsloth/Qwen3-8B-GGUF', quant: 'UD-Q4_K_XL' }), 'a resolve link names the repo and its UD quant')
check(M.parseHfLink('huggingface.co/unsloth/Qwen3-8B-GGUF/blob/main/Qwen3-8B-q8_0.gguf')?.quant === 'Q8_0', 'a blob link names its quant, upper-cased')
check(M.parseHfLink('hf.co/unsloth/Qwen3-8B-GGUF/resolve/main/BF16/Qwen3-8B-BF16.gguf')?.quant === 'BF16', 'a file in a folder still names its quant')
check(M.parseHfLink('hf.co/a/b/resolve/main/model.gguf')?.quant === null, 'a filename with no quant names none')

const q = M.parseQuery('fits on:spark-26af qwen quant:q4 engine:vllm color:x')
check(eq(q.filters, [{ key: 'fits' }, { key: 'on', value: 'spark-26af' }, { key: 'quant', value: 'q4' }, { key: 'engine', value: 'vllm' }]), 'four filters parsed')
check(q.words === 'qwen color:x', 'an unknown key stays a word')
check(eq(q.filterTokens, ['fits', 'on:spark-26af', 'quant:q4', 'engine:vllm']), 'filter tokens kept as typed')
const e = M.parseQuery('on: qwen')
check(e.filters.length === 0 && e.words === 'qwen', 'an empty on: is dropped')
check(M.parseQuery('https://huggingface.co/Qwen/Qwen3-8B').link?.repo === 'Qwen/Qwen3-8B', 'a link parses as a link')

const row = (o) => ({
  key: o.model_id, label: o.model_id, where: ['hub'], servedNames: [],
  verdict: null, reason: null, basis: null, predicted_decode_tps: null, headroom: null, total_params: null,
  dtype: null, nativeDtype: null, requantized: false, warnings: [], checking: false, cachedOn: [],
  bytesOnDisk: null, deployments: [], providers: [], offers: [], downloads: null, likes: null, gated: null,
  tags: [], pipelineTag: null, quantHint: null, detail: '', remoteOnly: false, unservedOnly: false,
  staticVerdict: null, needBytes: null, fitContext: null, fitSeqs: null, ...o,
})
const hub = row({ model_id: 'Qwen/Qwen3-30B-A3B', dtype: 'bf16', verdict: 'wont_fit', pipelineTag: 'text-generation' })
const disk = row({ model_id: 'Qwen/Qwen3-8B', where: ['ondisk'], cachedOn: ['spark-26af'], dtype: 'q4_k_m', verdict: 'fits' })
const live = row({
  model_id: 'meta/Llama-3', where: ['running'], quantHint: 'Q8_0',
  deployments: [{ state: 'ready', node_ids: ['spark-4d38'], runtime: 'vllm' }],
})
const rows = [hub, disk, live]

check(M.rowPasses(disk, { key: 'fits' }) && M.rowPasses(live, { key: 'fits' }) && !M.rowPasses(hub, { key: 'fits' }), 'fits: running and fitting rows only')
check(M.rowPasses(disk, { key: 'on', value: '26af' }) && M.rowPasses(live, { key: 'on', value: '4d38' }) && !M.rowPasses(hub, { key: 'on', value: 'spark' }), 'on: cached or deployed nodes')
check(M.rowPasses(disk, { key: 'quant', value: 'q4' }) && M.rowPasses(live, { key: 'quant', value: 'q8' }) && !M.rowPasses(hub, { key: 'quant', value: 'q4' }), 'quant: dtype or hint')
check(M.rowPasses(hub, { key: 'task', value: 'text' }) && !M.rowPasses(disk, { key: 'task', value: 'text' }), 'task: pipeline tag prefix')
check(M.rowPasses(live, { key: 'engine', value: 'vl' }) && !M.rowPasses(live, { key: 'engine', value: 'sglang' }) && !M.rowPasses(disk, { key: 'engine', value: 'vllm' }), 'engine: a live deployment runtime prefix')
check(!M.rowPasses({ ...live, deployments: [{ state: 'failed', node_ids: [], runtime: 'vllm' }] }, { key: 'engine', value: 'vllm' }), 'engine: a failed deployment is not running on it')
check(eq(M.parseQuery('qwen pinned').filters, [{ key: 'pinned' }]), 'pinned parses as a filter')
const pins = new Set([disk.model_id])
check(M.rowPasses(disk, { key: 'pinned' }, { pins }) && !M.rowPasses(hub, { key: 'pinned' }, { pins }) && !M.rowPasses(disk, { key: 'pinned' }), 'pinned: only rows in the pin set')
check(M.suggest('pin', { nodes: [], rows }).some((s) => s.title === 'Filters' && s.items.some((i) => i.text === 'pinned' && i.action.query === 'pinned ')), 'pin offers the pinned keyword')

const ctx = {
  nodes: [
    { id: 'spark-26af', freeBytes: 40 * 2 ** 30, running: 1 },
    { id: 'spark-4d38', freeBytes: null, running: 0 },
    { id: 'pi', freeBytes: 2 ** 30, running: 0 },
  ],
  rows,
}
const fin = M.suggest('quant:q', ctx).find((s) => s.title === 'Finish the filter')
check(fin != null && fin.items.length > 0 && fin.items.every((i) => i.text.startsWith('quant:q')), 'quant: completes from the rows')
check(fin?.items.every((i) => /^(1 model|\d+ models)$/.test(i.note)), 'quant: notes count models')
const on = M.suggest('fits on:sp', ctx).find((s) => s.title === 'Finish the filter')
check(eq(on?.items.map((i) => i.text), ['on:spark-26af', 'on:spark-4d38']), 'on: completes node ids by prefix')
check(on?.items[0].note === '40.0 GiB free · 1 running' && on?.items[1].note === '— free · 0 running', 'on: notes say free memory and running count')
check(on?.items[0].action.query === 'fits on:spark-26af ', 'completion replaces the last token')
const eng = M.suggest('engine:vl', ctx).find((s) => s.title === 'Finish the filter')
check(eq(eng?.items.map((i) => i.text), ['engine:vllm']), 'engine:vl completes to engine:vllm')
const models = M.suggest('qw', ctx).find((s) => s.title.startsWith('Models matching'))
check(eq(models?.items.map((i) => i.text), ['Qwen/Qwen3-8B', 'Qwen/Qwen3-30B-A3B']), 'local rows come before hub-only rows')
check(eq(models?.items[0].hit, [0, 2]), 'the hit range marks the words')
check(models?.items[0].band === 'fits' && models?.items[1].band === 'wont', 'model suggestions carry their band for the lamp')
check(M.suggest('fi', ctx).some((s) => s.title === 'Filters' && s.items.some((i) => i.text === 'fits')), 'fi offers the fits keyword')
const link = M.suggest('hf.co/Qwen/Qwen3-8B', ctx)
check(link.length === 1 && link[0].items[0].note === 'already known to this cluster', 'a link is the only section and knows the row')
const fresh = M.suggest('hf.co/org/New-Model', ctx)
check(fresh[0].items[0].note === 'Added org/New-Model from the link. derate reads its config next, then checks the fit.', 'an unknown repo says what happens next')

// The filter dropdown toggles its tokens in the box, so each one must parse
// back as exactly that filter -- a token the parser reads as a word would tick
// in the menu and search for a model called "quant:q4" instead.
const menu = M.filterMenu(ctx)
const tokens = menu.flatMap((s) => s.choices.map((c) => c.token))
check(tokens.every((t) => eq(M.parseQuery(t).filterTokens, [t])), 'every dropdown choice parses back as its own filter')
check(eq(menu.map((s) => s.title), ['Status', 'On a machine', 'Quant', 'Kind', 'Engine']), 'the dropdown lists every category')
check(menu.find((s) => s.title === 'On a machine').choices.map((c) => c.token).join() === 'on:spark-26af,on:spark-4d38,on:pi', 'one machine choice per node')
check(!M.filterMenu({ nodes: [], rows: [] }).some((s) => s.title === 'On a machine' || s.title === 'Quant'), 'an empty category is left out')
done()
