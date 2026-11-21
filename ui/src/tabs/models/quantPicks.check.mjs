import { load, report } from '../../check/harness.mjs'
const M = await load(import.meta.url, './quantPicks.ts')
const { variantKey } = await load(import.meta.url, './ollamaTarget.ts')
const { check, done } = report()

// Trimmed by hand from a live `GET /api/models/variants?model_id=Qwen/Qwen3-8B`
// on 2026-11-20 (45 rows -> 8), every field kept.
const LADDER = {
  "model_id": "Qwen/Qwen3-8B",
  "variants": [
    {
      "dtype": "q8_0",
      "label": "UD-Q8_K_XL",
      "repo_id": "unsloth/Qwen3-8B-GGUF",
      "source": "gguf_file",
      "gguf_file": "Qwen3-8B-UD-Q8_K_XL.gguf",
      "shard_count": 1,
      "shard_files": [
        "Qwen3-8B-UD-Q8_K_XL.gguf"
      ],
      "file_bytes": 10824038208,
      "downloads": null,
      "launchable": true,
      "note": "one file inside a GGUF repository; size is measured, not estimated",
      "bits_per_weight": 8.5,
      "family": "gguf",
      "verdict": "fits",
      "fits": true,
      "headroom": 23661458602,
      "reason": "Fits: 16.9 GiB of 38.9 GiB allocatable right now per rank, 22.0 GiB headroom. Predicted decode 24 tok/s at short context, falling to 10 tok/s at 40960. Per sequence. Context could go to 201216 tokens at 1 sequences.",
      "predicted_decode_tps": 10.184871472904733,
      "context": 40960,
      "max_seqs": 1,
      "static_verdict": "fits",
      "static_fits": true,
      "static_headroom": 97534694570,
      "static_reason": "Fits: 16.9 GiB of 107.7 GiB usable per rank, 90.8 GiB headroom. Predicted decode 24 tok/s at short context, falling to 10 tok/s at 40960. Per sequence. Context could go to 701952 tokens at 1 sequences.",
      "static_predicted_decode_tps": 10.184871472904733,
      "rank": 0
    },
    {
      "dtype": "fp8",
      "label": "Qwen3-8B-FP8",
      "repo_id": "Qwen/Qwen3-8B-FP8",
      "source": "repo_name",
      "gguf_file": null,
      "shard_count": 2,
      "shard_files": [
        "model-00001-of-00002.safetensors",
        "model-00002-of-00002.safetensors"
      ],
      "file_bytes": 9436628784,
      "downloads": 293658,
      "launchable": true,
      "note": "scheme read from the repository name",
      "bits_per_weight": 8.0,
      "family": "float",
      "verdict": "fits",
      "fits": true,
      "headroom": 25048868026,
      "reason": "Fits: 15.6 GiB of 38.9 GiB allocatable right now per rank, 23.3 GiB headroom. Predicted decode 25 tok/s at short context, falling to 11 tok/s at 40960. Per sequence. Context could go to 210432 tokens at 1 sequences.",
      "predicted_decode_tps": 10.5512561429603,
      "context": 40960,
      "max_seqs": 1,
      "static_verdict": "fits",
      "static_fits": true,
      "static_headroom": 98922103994,
      "static_reason": "Fits: 15.6 GiB of 107.7 GiB usable per rank, 92.1 GiB headroom. Predicted decode 25 tok/s at short context, falling to 11 tok/s at 40960. Per sequence. Context could go to 711680 tokens at 1 sequences.",
      "static_predicted_decode_tps": 10.5512561429603,
      "rank": 1
    },
    {
      "dtype": "q8_0",
      "label": "Q8_0",
      "repo_id": "unsloth/Qwen3-8B-GGUF",
      "source": "gguf_file",
      "gguf_file": "Qwen3-8B-Q8_0.gguf",
      "shard_count": 1,
      "shard_files": [
        "Qwen3-8B-Q8_0.gguf"
      ],
      "file_bytes": 8709519168,
      "downloads": null,
      "launchable": true,
      "note": "one file inside a GGUF repository; size is measured, not estimated",
      "bits_per_weight": 8.5,
      "family": "gguf",
      "verdict": "fits",
      "fits": true,
      "headroom": 25775977642,
      "reason": "Fits: 14.9 GiB of 38.9 GiB allocatable right now per rank, 24.0 GiB headroom. Predicted decode 24 tok/s at short context, falling to 10 tok/s at 40960. Per sequence. Context could go to 215552 tokens at 1 sequences.",
      "predicted_decode_tps": 10.184871472904733,
      "context": 40960,
      "max_seqs": 1,
      "static_verdict": "fits",
      "static_fits": true,
      "static_headroom": 99649213610,
      "static_reason": "Fits: 14.9 GiB of 107.7 GiB usable per rank, 92.8 GiB headroom. Predicted decode 24 tok/s at short context, falling to 10 tok/s at 40960. Per sequence. Context could go to 716288 tokens at 1 sequences.",
      "static_predicted_decode_tps": 10.184871472904733,
      "rank": 2
    },
    {
      "dtype": "awq_int4",
      "label": "Qwen3-8B-AWQ",
      "repo_id": "Qwen/Qwen3-8B-AWQ",
      "source": "repo_name",
      "gguf_file": null,
      "shard_count": 2,
      "shard_files": [
        "model-00001-of-00002.safetensors",
        "model-00002-of-00002.safetensors"
      ],
      "file_bytes": 6098581864,
      "downloads": 873640,
      "launchable": true,
      "note": "scheme read from the repository name",
      "bits_per_weight": 4.5,
      "family": "int",
      "verdict": "fits",
      "fits": true,
      "headroom": 28386914946,
      "reason": "Fits: 12.5 GiB of 38.9 GiB allocatable right now per rank, 26.4 GiB headroom. Predicted decode 44 tok/s at short context, falling to 14 tok/s at 40960. Per sequence. Context could go to 232960 tokens at 1 sequences.",
      "predicted_decode_tps": 14.10244966172154,
      "context": 40960,
      "max_seqs": 1,
      "static_verdict": "fits",
      "static_fits": true,
      "static_headroom": 102260150914,
      "static_reason": "Fits: 12.5 GiB of 107.7 GiB usable per rank, 95.2 GiB headroom. Predicted decode 44 tok/s at short context, falling to 14 tok/s at 40960. Per sequence. Context could go to 734208 tokens at 1 sequences.",
      "static_predicted_decode_tps": 14.10244966172154,
      "rank": 11
    },
    {
      "dtype": "q4_k_m",
      "label": "UD-Q4_K_XL",
      "repo_id": "unsloth/Qwen3-8B-GGUF",
      "source": "gguf_file",
      "gguf_file": "Qwen3-8B-UD-Q4_K_XL.gguf",
      "shard_count": 1,
      "shard_files": [
        "Qwen3-8B-UD-Q4_K_XL.gguf"
      ],
      "file_bytes": 5135722304,
      "downloads": null,
      "launchable": true,
      "note": "one file inside a GGUF repository; size is measured, not estimated",
      "bits_per_weight": 4.9,
      "family": "gguf",
      "verdict": "fits",
      "fits": true,
      "headroom": 29349774506,
      "reason": "Fits: 11.6 GiB of 38.9 GiB allocatable right now per rank, 27.3 GiB headroom. Predicted decode 41 tok/s at short context, falling to 14 tok/s at 40960. Per sequence. Context could go to 239616 tokens at 1 sequences.",
      "predicted_decode_tps": 13.580095633046723,
      "context": 40960,
      "max_seqs": 1,
      "static_verdict": "fits",
      "static_fits": true,
      "static_headroom": 103223010474,
      "static_reason": "Fits: 11.6 GiB of 107.7 GiB usable per rank, 96.1 GiB headroom. Predicted decode 41 tok/s at short context, falling to 14 tok/s at 40960. Per sequence. Context could go to 740864 tokens at 1 sequences.",
      "static_predicted_decode_tps": 13.580095633046723,
      "rank": 19
    },
    {
      "dtype": "q4_k_m",
      "label": "Q4_K_M",
      "repo_id": "unsloth/Qwen3-8B-GGUF",
      "source": "gguf_file",
      "gguf_file": "Qwen3-8B-Q4_K_M.gguf",
      "shard_count": 1,
      "shard_files": [
        "Qwen3-8B-Q4_K_M.gguf"
      ],
      "file_bytes": 5027784512,
      "downloads": null,
      "launchable": true,
      "note": "one file inside a GGUF repository; size is measured, not estimated",
      "bits_per_weight": 4.9,
      "family": "gguf",
      "verdict": "fits",
      "fits": true,
      "headroom": 29457712298,
      "reason": "Fits: 11.5 GiB of 38.9 GiB allocatable right now per rank, 27.4 GiB headroom. Predicted decode 41 tok/s at short context, falling to 14 tok/s at 40960. Per sequence. Context could go to 240640 tokens at 1 sequences.",
      "predicted_decode_tps": 13.580095633046723,
      "context": 40960,
      "max_seqs": 1,
      "static_verdict": "fits",
      "static_fits": true,
      "static_headroom": 103330948266,
      "static_reason": "Fits: 11.5 GiB of 107.7 GiB usable per rank, 96.2 GiB headroom. Predicted decode 41 tok/s at short context, falling to 14 tok/s at 40960. Per sequence. Context could go to 741376 tokens at 1 sequences.",
      "static_predicted_decode_tps": 13.580095633046723,
      "rank": 20
    },
    {
      "dtype": "q2_k",
      "label": "UD-Q2_K_XL",
      "repo_id": "unsloth/Qwen3-8B-GGUF",
      "source": "gguf_file",
      "gguf_file": "Qwen3-8B-UD-Q2_K_XL.gguf",
      "shard_count": 1,
      "shard_files": [
        "Qwen3-8B-UD-Q2_K_XL.gguf"
      ],
      "file_bytes": 3501975360,
      "downloads": null,
      "launchable": true,
      "note": "one file inside a GGUF repository; size is measured, not estimated",
      "bits_per_weight": 2.63,
      "family": "gguf",
      "verdict": "fits",
      "fits": true,
      "headroom": 30983521450,
      "reason": "Fits: 10.1 GiB of 38.9 GiB allocatable right now per rank, 28.9 GiB headroom. Predicted decode 76 tok/s at short context, falling to 17 tok/s at 40960. Per sequence. Context could go to 250880 tokens at 1 sequences.",
      "predicted_decode_tps": 17.19035385565082,
      "context": 40960,
      "max_seqs": 1,
      "static_verdict": "fits",
      "static_fits": true,
      "static_headroom": 104856757418,
      "static_reason": "Fits: 10.1 GiB of 107.7 GiB usable per rank, 97.7 GiB headroom. Predicted decode 76 tok/s at short context, falling to 17 tok/s at 40960. Per sequence. Context could go to 751616 tokens at 1 sequences.",
      "static_predicted_decode_tps": 17.19035385565082,
      "rank": 33
    },
    {
      "dtype": "bf16",
      "label": "bf16",
      "repo_id": "Qwen/Qwen3-8B",
      "source": "self",
      "gguf_file": null,
      "shard_count": 1,
      "shard_files": [],
      "file_bytes": 16381516776,
      "downloads": null,
      "launchable": true,
      "note": "the repository as it stands",
      "bits_per_weight": 16.0,
      "family": "float",
      "verdict": "fits_degraded",
      "fits": true,
      "headroom": 18103980034,
      "reason": "Loads with 16.9 GiB to spare, but predicted decode is 6.7 tok/s, under the 10 tok/s usability threshold. Bandwidth bound: 22.4 GB moves per decoded token at 273 GB/s. Adding nodes will not fix this \u2014 a smaller quantization, a smaller model, or an MoE with fewer active parameters will.",
      "predicted_decode_tps": 6.696766515861283,
      "context": 40960,
      "max_seqs": 1,
      "static_verdict": "fits_degraded",
      "static_fits": true,
      "static_headroom": 91977216002,
      "static_reason": "Loads with 85.7 GiB to spare, but predicted decode is 6.7 tok/s, under the 10 tok/s usability threshold. Bandwidth bound: 22.4 GB moves per decoded token at 273 GB/s. Adding nodes will not fix this \u2014 a smaller quantization, a smaller model, or an MoE with fewer active parameters will.",
      "static_predicted_decode_tps": 6.696766515861283,
      "rank": 44
    }
  ],
  "recommended": {
    "repo_id": "unsloth/Qwen3-8B-GGUF",
    "label": "UD-Q8_K_XL",
    "dtype": "q8_0",
    "reason": "Fits: 16.9 GiB of 38.9 GiB allocatable right now per rank, 22.0 GiB headroom. Predicted decode 24 tok/s at short context, falling to 10 tok/s at 40960. Per sequence. Context could go to 201216 tokens at 1 sequences."
  },
  "sized_on": {
    "nodes": [
      "spark-26af"
    ],
    "probed_node": "spark-26af",
    "budget_basis": "gpu",
    "local_serving": true,
    "tensor_parallel": 1,
    "allocatable_per_node": 41800970730,
    "usable_per_node": 115674206698,
    "budget_is_live": true
  },
  "heuristic": true,
  "note": "Names are the only signal most quantizers leave, so this is a shortlist to offer a user, not a promise that each one loads.",
  "from_cache": false
}

const v = (label, bits) => ({ ...LADDER.variants[0], label, bits_per_weight: bits })
check(['16', '8', '5', '2'].join() === [16, 8.5, 4.85, 2.4].map((b) => M.tierOf(v('x', b))).join(), 'tierOf reads measured bits first')
check(M.tierOf(v('UD-Q4_K_XL', null)) === '4', 'UD-Q4_K_XL with no bits is 4-bit')
check(M.tierOf(v('FP8', null)) === '8', 'FP8 with no bits is 8-bit')
// A K-quant measures above its name (Q4_K_M ~4.9 bpw); the name decides.
check(M.tierOf(v('Q4_K_M', 4.9)) === '4' && M.tierOf(v('UD-Q4_K_XL', 4.9)) === '4' && M.tierOf(v('IQ4_XS', 4.46)) === '4', 'a Q4 name is 4-bit whatever it measures')
check(M.tierOf(v('Q8_0', 8.5)) === '8' && M.tierOf(v('UD-IQ1_M', 2.15)) === '1', 'Q8_0 is 8-bit and IQ1_M is 1-bit')

// The live recommendation is a GGUF, which llamacpp serves and vllm does not.
const gguf = M.recommendedPicks(LADDER, 'llamacpp')
check(gguf[0]?.title === 'BEST QUALITY THAT FITS' && gguf[0].variant.label === LADDER.recommended.label && gguf[0].variant.repo_id === LADDER.recommended.repo_id, 'under llamacpp the recommended row is the first card')
check(new Set(gguf.map((p) => variantKey(p.variant))).size === gguf.length, 'no two cards share a row')
const ud = gguf.find((p) => p.title === 'UNSLOTH’S PICK')
check(ud?.variant.label === 'UD-Q4_K_XL', 'Unsloth’s pick is UD-Q4_K_XL')

// Under vllm a runtime-blind GGUF recommendation must not lead or be the
// default: the plan refuses it, so it would open every model on a refusal.
const st = M.recommendedPicks(LADDER, 'vllm')
check(st[0]?.title === 'BEST QUALITY THAT FITS' && st[0].variant.gguf_file == null && st[0].variant.fits === true, 'under vllm the first card is the best fitting safetensors row')
check(st.every((p) => p.variant.gguf_file == null), 'under vllm no card is a GGUF')
check(M.defaultVariant(LADDER, 'vllm', null)?.gguf_file == null, 'under vllm the default is not the GGUF recommendation')

check(M.defaultVariant(LADDER, 'vllm', 'Q4_K_M')?.label === 'Q4_K_M', 'a preferred label is selected')
check(M.defaultVariant(LADDER, 'vllm', 'Q4_K_XL')?.label === 'UD-Q4_K_XL', 'a link label without UD- finds the Dynamic row')
check(M.defaultVariant(LADDER, 'llamacpp', null)?.label === LADDER.recommended.label, 'no preference selects the recommendation where it is servable')
check(M.defaultVariant(null, 'vllm', null) === null, 'no ladder, no selection')

const groups = M.repoGroups(LADDER, 'vllm')
check(groups[0]?.repo === 'Qwen/Qwen3-8B', 'the base repo is the first group')
check(groups.find((g) => g.repo === 'unsloth/Qwen3-8B-GGUF')?.variants.length === 5, 'rows group by repository')

const idle = structuredClone(LADDER)
idle.recommended = null
for (const r of idle.variants) r.fits = false
check(M.recommendedPicks(idle, 'vllm')[0]?.title === 'BEST ONCE A NODE IS FREE', 'with nothing fitting now, the idle-hardware fit leads')
done()
