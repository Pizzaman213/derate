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

/** Tasks a runtime here serves. Generation is what vLLM and SGLang do; a
 *  diffusion or classification checkpoint is a different program entirely.
 *
 *  `text-to-speech` is here because it stopped being true that nothing serves
 *  it: the `tts` runtime does, on `/v1/audio/speech`. Whether this particular
 *  checkpoint loads is a narrower question than its pipeline tag, and the one
 *  the support table answers -- so a TTS model whose architecture is not in
 *  `TTS_ARCHITECTURES` still gets its red dot, from the runtime rows, with the
 *  architecture named. What it must not get is a dot for being TTS at all. */
const SERVABLE_PIPELINES = new Set([
  'text-generation',
  'text2text-generation',
  'image-text-to-text',
  'any-to-any',
  'conversational',
  'text-to-speech',
])

export function classifySupport(
  row: ModelRow,
  table: QuantTable | null,
  /** Whether any provider is configured that can be told to fetch weights.
   *  Defaults false, so a caller that has not been taught about providers gets
   *  exactly the sentences it got before. */
  canPull = false,
): SupportVerdict {
  if (row.remoteOnly) return OK
  // Same argument. Nothing here is going to load a model that exists in this
  // list only because a provider publishes it, so "no runtime here takes this
  // format" is an answer to a question nobody asked -- and drawing a red dot
  // beside a model one click from being served reads as a refusal.
  if (row.unservedOnly) return OK

  if (row.pipelineTag && !SERVABLE_PIPELINES.has(row.pipelineTag)) {
    // Left as it stands, deliberately. A provider does not rescue this one:
    // Ollama does not serve text-to-speech or reranking either, so naming it
    // here would offer a route that ends the same way.
    return {
      status: 'unsupported',
      reason: `This is a ${row.pipelineTag} model. No runtime here serves that task.`,
    }
  }

  const scheme = detectScheme(row, table)
  if (!scheme || !table) return UNKNOWN

  const entry = table.schemes.find((s) => s.key === scheme)
  if (!entry) return UNKNOWN

  // GGUF used to be refused here before the table was consulted at all, on
  // the grounds that both serve templates took a repository path and there
  // was no llama.cpp runtime on this cluster. There is one now, so the
  // special case is gone and the question goes to the table -- which is what
  // this module's header says it does, and what made a scheme gaining support
  // server-side light up here without an edit. The special case was the one
  // exception to that, and it was the one that went stale.
  const levels = Object.values(entry.runtimes)
  if (levels.length && levels.every((l) => l === 'unsupported')) {
    return {
      status: 'unsupported',
      reason:
        (entry.note || `No runtime here loads ${entry.key}.`) +
        // Only for GGUF, and only when a provider could actually take it. A
        // route out is worth appending; one that ends the same way is not.
        (entry.family === 'gguf' && canPull
          ? ' Open the model and pick the ollama runtime to pull a GGUF onto a provider instead.'
          : ''),
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
  // `nativeDtype`, never `dtype`. `dtype` is what the fit gate would step DOWN
  // to in order to make the model fit -- a recommendation about a different set
  // of weights. Reading it here put a red "q4_k_m is a llama.cpp format"
  // unsupported dot on `Qwen/Qwen3-30B-A3B`, which is a bf16 safetensors repo
  // and the one model on this cluster that both fits and can be served.
  if (row.nativeDtype) return row.nativeDtype.toLowerCase()
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
