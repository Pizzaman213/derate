// The KV width picker's rows, checked against `kvWidths.ts` itself.
//
//   node src/tabs/models/kvWidths.check.mjs
//
// Every fact on a row is the server's. The bug this exists for: the client
// kept its own list of widths that pass no flag, so on a checkpoint whose
// quantization config declares fp8_e4m3 the model's own row said nothing about
// fp8 and the 16-bit rows claimed a launch they did not get.

import { load, report } from '../../check/harness.mjs'

const M = await load(import.meta.url, './kvWidths.ts')
const { check, done } = report()

const declared = {
  kv_dtype_ratios: { bfloat16: 1, fp8: 0.5, fp8_e4m3: 0.5 },
  kv_dtype_own: { name: 'fp8_e4m3', declared: true, ratio: 0.5 },
  kv_dtype_flags: { bfloat16: '--kv-cache-dtype bfloat16', fp8: '--kv-cache-dtype fp8' },
  kv_dtype_refused: { float16: 'computes in bfloat16; choose bfloat16', fp8_ds_mla: 'no MLA' },
}
const names = ['auto', 'bfloat16', 'float16', 'fp8', 'fp8_e4m3', 'fp8_ds_mla']
const byValue = (rows) => Object.fromEntries(rows.map((r) => [r.value, r]))

{
  const rows = byValue(M.kvRows(names, declared, { fp8: 1 }, ''))
  check(!('auto' in rows), '`auto` is the own row, never a second one')
  check(rows[''].label === 'The model’s own — fp8_e4m3, set by its quantization config', `own row names the declared width (${rows[''].label})`)
  check(rows[''].ratio === 0.5 && rows[''].flag === '', 'own row: its ratio, and no flag (vLLM reads it off the checkpoint)')
  check(rows.bfloat16.flag === '--kv-cache-dtype bfloat16', 'the override is passed by name, as the server says')
  check(!('fp8_e4m3' in rows), 'the declared width is the own row, not listed twice')
  check(rows.float16.dashed && rows.float16.why === declared.kv_dtype_refused.float16, 'a refused width is dashed with the server’s sentence, verbatim')
  check(rows.float16.disabled && rows.float16.ratio === null, 'a dashed width cannot be picked and has no multiplier')
}

{
  const rows = byValue(M.kvRows(names, declared, undefined, 'float16'))
  check(!rows.float16.disabled, 'the dashed width that is picked stays reachable, so it can be left')
}

{
  const rows = byValue(M.kvRows(names, null, { fp8: 1, bfloat16: 2 }, ''))
  check(rows.fp8.ratio === 0.5 && rows.bfloat16.ratio === 1, 'before a plan answers, the model-free bytes stand in')
  check(!rows.fp8_ds_mla.dashed && rows.fp8_ds_mla.ratio === null, 'before a plan answers, an unpriced width is not dashed')
  check(rows[''].label === 'The model’s own (default)' && rows[''].ratio === null, 'before a plan answers, the own row claims no width')
}

{
  const plain = { ...declared, kv_dtype_own: { name: 'bfloat16', declared: false, ratio: 1 } }
  check(M.ownLabel(plain) === 'The model’s own — bfloat16', 'a 16-bit own width is named without a source')
  check(M.ownLabel({ ...declared, kv_dtype_own: { name: null, declared: false, ratio: 1 } }) === 'The model’s own (default)', 'an unstated compute dtype claims no width')
  check(M.kvSummary('', declared) === 'fp8_e4m3 (model’s own)' && M.kvSummary('fp8', declared) === 'fp8', 'the summary says what the cache is')
  check(M.kvSummary('', null) === 'default', 'the summary claims nothing before a plan answers')
}

{
  const plain = { ...declared, kv_dtype_own: { name: 'bfloat16', declared: false, ratio: 1 }, kv_dtype_flags: { fp8: '--kv-cache-dtype fp8' } }
  const values = M.kvRows(names, plain, undefined, '').map((r) => r.value)
  check(!values.includes('bfloat16'), 'the 16-bit width that is the own row is not listed twice')
  check(M.kvRows(names, plain, undefined, 'bfloat16').some((r) => r.value === 'bfloat16'), '...unless it is the one picked')
  const offered = M.pickableKvRows(M.kvRows(names, declared, undefined, 'float16'))
  check(offered.some((r) => r.value === 'float16'), 'a refused width that is picked stays a radio, so it can be left')
  check(!offered.some((r) => r.value === 'fp8_ds_mla'), 'every other refused width is hidden')
}

done()
