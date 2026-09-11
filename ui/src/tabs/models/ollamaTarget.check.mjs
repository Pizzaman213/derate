// Verifier for ollamaTarget.ts. There is no UI test runner here, so the rule
// is the same as rows.check.mjs and layout.check.mjs: esbuild-bundle the module
// and exercise it, because a green tsc says nothing about what the string is.
//
//   node ui/src/tabs/models/ollamaTarget.check.mjs
import { build as bundleWithEsbuild } from 'esbuild'
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const out = mkdtempSync(join(tmpdir(), 'ollama-target-'))
const bundle = join(out, 'bundle.mjs')
// esbuild's JS API rather than spawning `npx`: on Windows that resolves to
// npx.cmd, which cannot be launched by path without a shell. Same form as the
// other verifiers.
await bundleWithEsbuild({
  entryPoints: [join(here, 'ollamaTarget.ts')],
  bundle: true,
  format: 'esm',
  outfile: bundle,
  logLevel: 'warning',
})
const { ollamaRef, runnableOnOllama, variantKey, partitionForRuntime, launchId } =
  await import(pathToFileURL(bundle).href)

let failures = 0
const check = (name, actual, expected) => {
  const ok = actual === expected
  if (!ok) failures++
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${name}${ok ? '' : `\n       got ${JSON.stringify(actual)}\n       want ${JSON.stringify(expected)}`}`)
}

const gguf = (over = {}) => ({
  dtype: 'q4_k_m', label: 'Q4_K_M', repo_id: 'bartowski/Qwen2.5-0.5B-Instruct-GGUF',
  source: 'gguf_file', gguf_file: 'Qwen2.5-0.5B-Instruct-Q4_K_M.gguf',
  file_bytes: 397808192, downloads: 1, launchable: true, note: '',
  shard_count: 1, shard_files: [], ...over,
})

// The exact string verified against a live Ollama 0.33.3.
check('gguf row maps to an hf.co ref', ollamaRef(gguf()),
  'hf.co/bartowski/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M')

// The publisher's name, not the canonical key: this repo has no file called
// q4_k_m, so pricing's spelling would resolve to nothing.
check('publisher label wins over canonical dtype',
  ollamaRef(gguf({ label: 'UD-Q4_K_XL', dtype: 'q4_k_m' })),
  'hf.co/bartowski/Qwen2.5-0.5B-Instruct-GGUF:UD-Q4_K_XL')

// Ollama loads GGUF and nothing else. Offering a bf16 row would be offering a
// pull that fails after the download.
check('non-gguf row is not a target', ollamaRef(gguf({ gguf_file: null })), null)
check('non-gguf row is not runnable', runnableOnOllama(gguf({ gguf_file: null })), false)
check('gguf row is runnable', runnableOnOllama(gguf()), true)

// Verified against Ollama 0.33.3, which refuses a multi-part repository at
// manifest resolution: "Ollama does not yet support pulling sharded GGUF via
// the registry". Cheap to fail, but a button that cannot work is a dead offer.
check('a sharded gguf repo is not runnable',
  runnableOnOllama(gguf({ shard_count: 3, shard_files: ['a', 'b', 'c'] })), false)
check('and is not addressable either',
  ollamaRef(gguf({ shard_count: 3 })), null)

// Nothing to address.
check('empty repo is not a target', ollamaRef(gguf({ repo_id: '' })), null)
check('empty label is not a target', ollamaRef(gguf({ label: '   ' })), null)
check('stray slashes are trimmed',
  ollamaRef(gguf({ repo_id: '/bartowski/Qwen2.5-0.5B-Instruct-GGUF/' })),
  'hf.co/bartowski/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M')

// --- variantKey -----------------------------------------------------------
// One repository publishes many quantizations. Keying a per-row state on
// repo_id makes Serve on one row light up every sibling in the same repo.
const q8 = gguf({ label: 'Q8_0', gguf_file: 'Qwen2.5-0.5B-Instruct-Q8_0.gguf' })
check('two tags of one repo are two keys',
  variantKey(gguf()) !== variantKey(q8), true)
check('a row without a gguf file falls back to its label',
  variantKey(gguf({ gguf_file: null, label: 'bf16' })),
  'bartowski/Qwen2.5-0.5B-Instruct-GGUF::bf16')

// --- partitionForRuntime --------------------------------------------------
// launchable and runnableOnOllama are near enough inverses that the ladder
// flips: what the cluster cannot launch is the only thing Ollama can fetch.
// The flags below are deliberately set the wrong way round for each runtime,
// so a partition that quietly read the other field would show up here.
const ladder = [
  gguf({ label: 'Q4_K_M', launchable: false }),
  gguf({ label: 'Q8_0', gguf_file: 'q8.gguf', launchable: false }),
  gguf({ label: 'bf16', dtype: 'bf16', gguf_file: null, launchable: true }),
]
const onCluster = partitionForRuntime(ladder, 'vllm')
check('vllm serves what the gateway marked launchable',
  onCluster.servable.map((v) => v.label).join(','), 'bf16')
check('vllm leaves the gguf rows as reference',
  onCluster.reference.map((v) => v.label).join(','), 'Q4_K_M,Q8_0')

const onOllama = partitionForRuntime(ladder, 'ollama')
check('ollama serves the gguf rows regardless of launchable',
  onOllama.servable.map((v) => v.label).join(','), 'Q4_K_M,Q8_0')
check('ollama leaves the safetensors row as reference',
  onOllama.reference.map((v) => v.label).join(','), 'bf16')

// The gateway's rank order is the ladder's order, and neither branch re-sorts.
check('order is preserved, not recomputed',
  partitionForRuntime([...ladder].reverse(), 'ollama').servable
    .map((v) => v.label).join(','), 'Q8_0,Q4_K_M')

// sglang reads the same field vllm does -- the split is on the verb, not the
// engine name, so only ollama diverges.
check('sglang partitions exactly as vllm does',
  partitionForRuntime(ladder, 'sglang').servable.map((v) => v.label).join(','),
  onCluster.servable.map((v) => v.label).join(','))

// --- what a row is LAUNCHED as, which is not what it is keyed by ---------
//
// The repository id names a checkpoint for a safetensors row and a directory
// for a GGUF one. Sending the directory is the bug this exists to stop: those
// repos keep the base model's `config.json`, so derate would resolve it to a
// confident bf16 describing weights that are not in the repository, and the
// launch would carry a quantization nobody chose.
check('a gguf row is launched as the file, not the repository',
  launchId(gguf()), 'hf://bartowski/Qwen2.5-0.5B-Instruct-GGUF/Qwen2.5-0.5B-Instruct-Q4_K_M.gguf')
check('a safetensors row is launched as its repository, untouched',
  launchId(gguf({ gguf_file: null, dtype: 'bf16' })), 'bartowski/Qwen2.5-0.5B-Instruct-GGUF')
// Two quantizations in one repository must launch as two different ids. This
// is the same failure `variantKey` exists for, one layer further out: keyed
// apart on screen and collapsed on the wire would be worse than either.
check('two quantizations in one repository do not collapse to one id',
  launchId(gguf({ gguf_file: 'a.gguf' })) === launchId(gguf({ gguf_file: 'b.gguf' })), false)
// A subdirectory layout, which is how the large quants are published.
check('a quant in a subdirectory keeps its path',
  launchId(gguf({ gguf_file: 'UD-Q4_K_XL/model-00001-of-00002.gguf' })),
  'hf://bartowski/Qwen2.5-0.5B-Instruct-GGUF/UD-Q4_K_XL/model-00001-of-00002.gguf')

rmSync(out, { recursive: true, force: true })
console.log(failures ? `\n${failures} failed` : '\nall passed')
process.exit(failures ? 1 : 0)
