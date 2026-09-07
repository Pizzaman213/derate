import type { QuantTable } from '../../api/types'
import type { ModelRow } from './rows'

/** The red dot: whether anything on this cluster could load this at all.
 *
 *  Separate from fit, and it has to be. Fit asks whether the bytes go in;
 *  this asks whether a runtime here understands the format. A model can pass
 *  one and fail the other, and merging them into a single "no" would tell
 *  somebody to free memory for a file that would never have loaded.
 *
 *  Every judgement below is read off `GET /api/models/quant-table`, which is
 *  the contract's own table with a per-runtime verdict per scheme. Nothing is
 *  hardcoded here, so a scheme gaining vLLM support server-side lights up on
 *  this screen without an edit.
 *
 *  Unsupported is never hidden, only marked -- Unsloth's rule and the right
 *  one. The repository exists, somebody may be looking for it, and a list that
 *  quietly drops what it cannot run is lying about the hub. */
export interface SupportVerdict {
  status: 'ok' | 'unsupported' | 'unknown'
  reason: string | null
}

const OK: SupportVerdict = { status: 'ok', reason: null }
const UNKNOWN: SupportVerdict = { status: 'unknown', reason: null }

/** Tasks no runtime here serves. Generation is what vLLM and SGLang do; a
 *  diffusion or classification checkpoint is a different program entirely. */
const SERVABLE_PIPELINES = new Set([
  'text-generation',
  'text2text-generation',
  'image-text-to-text',
  'any-to-any',
  'conversational',
])

export function classifySupport(row: ModelRow, table: QuantTable | null): SupportVerdict {
  if (row.remote) return OK

  if (row.pipelineTag && !SERVABLE_PIPELINES.has(row.pipelineTag)) {
    return {
      status: 'unsupported',
      reason: `This is a ${row.pipelineTag} model. Neither vllm nor sglang serves that task here.`,
    }
  }

  const scheme = detectScheme(row, table)
  if (!scheme || !table) return UNKNOWN

  const entry = table.schemes.find((s) => s.key === scheme)
  if (!entry) return UNKNOWN

  // Mirrors `gateway/serialize.py:_launchable`: a llama.cpp format is refused
  // outright, because both serve templates take a repository path and there is
  // no llama.cpp runtime on this cluster.
  if (entry.family === 'gguf') {
    return {
      status: 'unsupported',
      reason: `${entry.key} is a llama.cpp format. Neither vllm nor sglang is verified to load it, and there is no llama.cpp runtime here.`,
    }
  }

  const levels = Object.values(entry.runtimes)
  if (levels.length && levels.every((l) => l === 'unsupported')) {
    return {
      status: 'unsupported',
      reason: entry.note || `No runtime here loads ${entry.key}.`,
    }
  }
  return OK
}

/** Which quantization this repository probably holds.
 *
 *  `quant_hint` is the gateway's own guess from the repository name and is
 *  preferred; tags are the fallback. Both are guesses -- the search path never
 *  resolves anything -- which is why the dot they drive is a warning with a
 *  sentence attached rather than a refusal. */
function detectScheme(row: ModelRow, table: QuantTable | null): string | null {
  if (row.quantHint) return row.quantHint.toLowerCase()
  if (row.dtype) return row.dtype.toLowerCase()
  if (!table) return null
  const haystack = `${row.model_id} ${row.tags.join(' ')}`.toLowerCase()
  // Longest key first: `q4_k_m` and `q4_k_s` both contain no shorter key, but
  // `iq4_nl` contains `q4_n`-shaped fragments and `fp16` sits inside names that
  // also carry `fp8`. Matching the longest candidate first stops a specific
  // scheme being read as a broader one.
  const keys = [...table.schemes.map((s) => s.key)].sort((a, b) => b.length - a.length)
  for (const key of keys) {
    if (haystack.includes(key)) return key
  }
  if (/\bgguf\b/.test(haystack)) return 'q4_k_m'
  return null
}
