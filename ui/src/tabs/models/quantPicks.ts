// Selection over the quant ladder the gateway already judged. No fit
// arithmetic here: every verdict, size and rank is the server's, and these
// functions only choose among rows and group them for the picker.

import type { QuantVariant, VariantLadder } from '../../api/types'
import type { Runtime } from '../../state/runtime'
import { partitionForRuntime, variantKey } from './ollamaTarget'

export type Tier = '16' | '8' | '6' | '5' | '4' | '3' | '2' | '1' | 'other'

export const TIER_ORDER: Tier[] = ['16', '8', '6', '5', '4', '3', '2', '1', 'other']

const BIT_STEPS: [number, Tier][] = [
  [12, '16'],
  [7.5, '8'],
  [5.5, '6'],
  [4.75, '5'],
  [3.5, '4'],
  [2.75, '3'],
  [1.9, '2'],
]

/** A llama.cpp quant name's own level: `Q4_K_M`, `UD-Q4_K_XL`, `IQ4_XS`, `Q8_0`. */
const QUANT_NAME = /(?:^|[-_.])I?Q(\d)(?:_|$)/

/** Precision tab for a row.
 *
 *  A llama.cpp quant name wins when there is one: it is the precision class
 *  the publisher chose and the one people search for, and K-quants carry block
 *  overhead (Q4_K_M measures ~4.9 bpw) that would otherwise file every "Q4"
 *  under 5-bit. Measured bits decide everything else -- AWQ, NVFP4, FP8 and
 *  bf16 repositories, whose names say nothing reliable -- and the label rules
 *  are the last resort for a row with neither. */
export function tierOf(v: QuantVariant): Tier {
  const l = v.label.toUpperCase()
  const named = QUANT_NAME.exec(l)?.[1]
  if (named && (TIER_ORDER as string[]).includes(named)) return named as Tier
  if (v.bits_per_weight != null) {
    for (const [min, tier] of BIT_STEPS) if (v.bits_per_weight >= min) return tier
    return '1'
  }
  if (/BF16|F16|FP16/.test(l)) return '16'
  if (/FP8|Q8/.test(l)) return '8'
  if (/NVFP4|MXFP4|AWQ|GPTQ|BNB|INT4/.test(l)) return '4'
  const d = /Q(\d)/.exec(l)?.[1]
  return d && (TIER_ORDER as string[]).includes(d) ? (d as Tier) : 'other'
}

export function formatOf(v: QuantVariant): 'gguf' | 'safetensors' {
  return v.gguf_file != null ? 'gguf' : 'safetensors'
}

export function isUnslothDynamic(v: QuantVariant): boolean {
  return v.label.toUpperCase().startsWith('UD-')
}

export interface Pick {
  title: string
  why: string
  variant: QuantVariant
}

/** Whether *v* is the row the server recommended. Matched on the file as well
 *  as `(repo_id, label)`: one repository can publish two builds of one label,
 *  and the pill must land on exactly one row. */
export function isRecommendedVariant(ladder: VariantLadder, v: QuantVariant): boolean {
  const r = ladder.recommended
  return (
    r != null &&
    r.repo_id === v.repo_id &&
    r.label === v.label &&
    (r.gguf_file ?? null) === (v.gguf_file ?? null)
  )
}

const ROOMY: ReadonlySet<Tier> = new Set<Tier>(['16', '8', '6', '5', '4'])

/** Servable rows of the runtime's native format (`formats[0]`), or every
 *  servable row when it has none: a runtime that reads both formats opens on
 *  its own and never switches an existing vllm user's default to a GGUF. */
function nativeFirst(servable: QuantVariant[], formats: readonly string[]): QuantVariant[] {
  const primary = servable.filter((v) => (v.gguf_file ? 'gguf' : 'safetensors') === formats[0])
  return primary.length ? primary : servable
}

/** The Recommended tab's cards, in the gateway's rank order. */
export function recommendedPicks(ladder: VariantLadder, runtime: Runtime, formats: readonly string[]): Pick[] {
  const servable = nativeFirst(partitionForRuntime(ladder.variants, runtime, formats).servable, formats)
  const picks: Pick[] = []

  // The gateway's recommendation is runtime-blind (it can name a GGUF while
  // vllm is picked), so it only leads when this runtime can serve it; failing
  // that, rank order is fit first then quality, so the first servable row that
  // fits is the same claim made for this runtime.
  const best = servable.find((v) => isRecommendedVariant(ladder, v)) ?? servable.find((v) => v.fits === true)
  const idle = servable.find((v) => v.static_fits === true)
  if (best)
    picks.push({ title: 'BEST QUALITY THAT FITS', why: 'Highest precision your nodes can hold right now.', variant: best })
  else if (idle)
    picks.push({ title: 'BEST ONCE A NODE IS FREE', why: 'Highest precision that fits on idle hardware.', variant: idle })
  else if (servable[0])
    picks.push({ title: 'CLOSEST AVAILABLE', why: 'Nothing here fits; this is the nearest option.', variant: servable[0] })

  const ud = servable.find(
    (v) => isUnslothDynamic(v) && v.label.toUpperCase() === 'UD-Q4_K_XL' && v.static_fits !== false,
  )
  if (ud)
    picks.push({
      title: 'UNSLOTH’S PICK',
      why: 'Unsloth Dynamic 4-bit: the layers that matter more keep more bits.',
      variant: ud,
    })

  let roomy: QuantVariant | null = null
  for (const v of servable) {
    if (v.fits !== true || v.file_bytes == null || !ROOMY.has(tierOf(v))) continue
    if (roomy == null || v.file_bytes < (roomy.file_bytes as number)) roomy = v
  }
  if (roomy)
    picks.push({
      title: 'MOST ROOM FOR CONTEXT',
      why: 'Smallest file at 4-bit or above, leaving the most memory for the KV cache.',
      variant: roomy,
    })

  const seen = new Set<string>()
  return picks.filter((p) => {
    const k = variantKey(p.variant)
    if (seen.has(k)) return false
    seen.add(k)
    return true
  })
}

/** The row selected before the operator picks one. `preferLabel` comes from a
 *  pasted `repo:QUANT` link, which usually omits Unsloth's `UD-` prefix. */
export function defaultVariant(
  ladder: VariantLadder | null,
  runtime: Runtime,
  preferLabel: string | null,
  formats: readonly string[],
): QuantVariant | null {
  if (!ladder) return null
  const { servable, reference } = partitionForRuntime(ladder.variants, runtime, formats)
  if (preferLabel) {
    const want = preferLabel.toUpperCase()
    const hit = [...servable, ...reference].find((v) => {
      const l = v.label.toUpperCase()
      return l === want || l.replace(/^UD-/, '') === want
    })
    if (hit) return hit
  }
  return (
    // The recommendation only when this runtime can serve it: it is
    // runtime-blind, and a default that opens on a refusal is a dead end.
    nativeFirst(servable, formats).find((v) => isRecommendedVariant(ladder, v)) ??
    nativeFirst(servable, formats)[0] ??
    ladder.variants[0] ??
    null
  )
}

export interface RepoGroup {
  repo: string
  variants: QuantVariant[]
  minBytes: number | null
  maxBytes: number | null
  pick: QuantVariant
}

/** The ladder by repository, base model first, each with the row a `Use`
 *  button should select. */
export function repoGroups(ladder: VariantLadder, runtime: Runtime, formats: readonly string[]): RepoGroup[] {
  const servable = new Set(partitionForRuntime(ladder.variants, runtime, formats).servable)
  const byRepo = new Map<string, QuantVariant[]>()
  for (const v of ladder.variants) {
    const list = byRepo.get(v.repo_id)
    if (list) list.push(v)
    else byRepo.set(v.repo_id, [v])
  }
  const groups = [...byRepo.entries()].map(([repo, variants]): RepoGroup => {
    const sizes = variants.map((v) => v.file_bytes).filter((b): b is number => b != null)
    return {
      repo,
      variants,
      minBytes: sizes.length ? Math.min(...sizes) : null,
      maxBytes: sizes.length ? Math.max(...sizes) : null,
      pick:
        variants.find((v) => servable.has(v) && v.fits === true) ??
        variants.find((v) => v.static_fits === true) ??
        (variants[0] as QuantVariant),
    }
  })
  const base = groups.findIndex((g) => g.repo === ladder.model_id)
  if (base > 0) groups.unshift(...groups.splice(base, 1))
  return groups
}
