import type { QuantVariant } from '../../api/types'
import type { Runtime } from '../../state/runtime'
import { servesOnCluster } from '../../state/runtime'

/** Addressing a ladder row as something Ollama can fetch.
 *
 *  Ollama has its own model namespace -- `qwen2.5:0.5b` -- which the rest of
 *  this tab knows nothing about. It also accepts a HuggingFace repository
 *  directly, as `hf.co/<repo>:<quantization>`, and that is the form used here:
 *  it keeps one model namespace across the screen, so the row you picked and
 *  the thing that gets served are the same object rather than two names a
 *  human has to keep in agreement.
 *
 *  Verified against Ollama 0.33.3 -- `hf.co/bartowski/Qwen2.5-0.5B-Instruct-
 *  GGUF:Q4_K_M` resolves the manifest and downloads.
 */

/** Whether this variant is something Ollama could fetch at all.
 *
 *  `gguf_file` is most of the test. Ollama loads GGUF and nothing else, so a
 *  bf16 or an AWQ row is not a target however well it fits -- and offering it
 *  would be offering a pull that fails after the download, which is the
 *  expensive way to find out.
 *
 *  `shard_count` is the rest, and it is not a guess. Ollama 0.33.3 refuses a
 *  multi-part repository at manifest resolution, before any bytes move:
 *
 *      pull model manifest: 400: This repository only contains sharded GGUF
 *      files. Ollama does not yet support pulling sharded GGUF via the
 *      registry
 *
 *  That refusal is cheap, but a Serve button that cannot work is still a dead
 *  offer, and the row says nothing about why. Excluded here so it lands in the
 *  reference group with the rest of what this route cannot take.
 */
export function runnableOnOllama(variant: QuantVariant): boolean {
  return (
    Boolean(variant.gguf_file) &&
    Boolean(variant.repo_id) &&
    variant.shard_count <= 1
  )
}

/** `hf.co/<repo>:<quantization>` for a GGUF row, or null when it is not one.
 *
 *  The publisher's `label`, not the canonical `dtype`. Ollama matches the tag
 *  against the filenames in the repository, and a repository publishing
 *  `UD-Q4_K_XL` has no file called `q4_k_m` -- the canonical key is what this
 *  codebase prices with, not what the publisher called the file. Rendering the
 *  publisher's name verbatim is the same rule the ladder already follows.
 */
export function ollamaRef(variant: QuantVariant): string | null {
  if (!runnableOnOllama(variant)) return null
  const repo = variant.repo_id.trim().replace(/^\/+|\/+$/g, '')
  const tag = variant.label.trim()
  if (!repo || !tag) return null
  return `hf.co/${repo}:${tag}`
}

/** A stable identity for one ladder row.
 *
 *  `repo_id` is not it. One GGUF repository publishes many quantizations --
 *  `Q8_0`, `Q6_K`, `Q5_K_M` all live in `bartowski/Qwen2.5-0.5B-Instruct-GGUF`
 *  -- so keying a per-row state on the repository makes Serve on one row light
 *  up every sibling. `gguf_file` separates them where it exists and `label`
 *  does where it does not, which is exactly the pair the variants table
 *  already composes for its React key.
 */
export function variantKey(variant: QuantVariant): string {
  return `${variant.repo_id}::${variant.gguf_file ?? variant.label}`
}

/** The id to LAUNCH this row as, which is not always the row's repository.
 *
 *  For a safetensors row the repository is the model: `variant.repo_id` names
 *  one set of weights and the resolver reads its `config.json`.
 *
 *  For a GGUF row it is not. One repository publishes many quantizations, so
 *  the repository id names a directory rather than a checkpoint -- and derate
 *  would resolve it through the original `config.json` those repos keep,
 *  which reports `torch_dtype: bfloat16` and describes weights that are not
 *  there. `hf://owner/repo/file.gguf` is the form that names the blob, and it
 *  is the form `resolver/gguf.py` measures by summing the file's own tensor
 *  directory rather than multiplying a parameter count by a nominal width.
 *
 *  `deploy/flags.py::llamacpp_model_spec` translates it once more at the
 *  recipe boundary, into the `owner/repo:QUANT` spelling sparkrun's llama-cpp
 *  plugin parses. Three spellings of one file, each owned by whoever needs it,
 *  and this is the only one the browser has to know.
 */
export function launchId(variant: QuantVariant): string {
  return variant.gguf_file
    ? `hf://${variant.repo_id}/${variant.gguf_file}`
    : variant.repo_id
}

/** Split a ladder into what this runtime can serve and what it cannot.
 *
 *  Under vllm and sglang this is the gateway's own `launchable`, unchanged.
 *  Under ollama it is `runnableOnOllama`, and the two are very nearly
 *  inverses: a GGUF row is the only thing Ollama loads and the only thing the
 *  cluster's runtimes will not.
 *
 *  This is not a second answer to the gateway's question. `launchable` mirrors
 *  what `POST /api/deployments` will decide -- that is what its docstring says
 *  it is for, so that a Serve button cannot disagree with the launch it
 *  triggers -- and under ollama that endpoint is never called. `launchable:
 *  false` stays exactly true, for exactly its stated reason: there is no
 *  llama.cpp runtime on this cluster. Ollama is a provider, not a runtime
 *  here, and "can that box fetch this" is a different question answered from
 *  two fields the same payload supplied.
 */
export function partitionForRuntime(
  variants: QuantVariant[],
  runtime: Runtime,
): { servable: QuantVariant[]; reference: QuantVariant[] } {
  const canServe = servesOnCluster(runtime)
    ? (v: QuantVariant) => v.launchable
    : runnableOnOllama
  const servable: QuantVariant[] = []
  const reference: QuantVariant[] = []
  // Order is the gateway's `rank`, preserved. Re-sorting here would be the
  // second answer this module is careful not to be.
  for (const v of variants) (canServe(v) ? servable : reference).push(v)
  return { servable, reference }
}
