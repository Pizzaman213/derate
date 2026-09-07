// The model picker: declarative data with no demo-generation logic attached.
//
// PlanView needs a curated model list whether or not anything is deployed yet.
// The fixture-scenario registry that used to live alongside this (for the
// day-0 fixture backend's demo-world switcher) is gone as of the derate port
// -- the UI is live-only now, so there is no "which fixture world" to pick.

export interface CuratedModel {
  model_id: string
  label: string
  detail: string
  default_context: number
  default_concurrency: number
}

/** The picker: the four frozen shapes, plus a free-text HuggingFace ID field. */
export const CURATED_MODELS: CuratedModel[] = [
  {
    model_id: 'openai/gpt-oss-120b',
    label: 'gpt-oss-120b',
    detail: 'MoE · mxfp4 · sliding window · 116.8B',
    default_context: 32768,
    default_concurrency: 16,
  },
  {
    model_id: 'Qwen/Qwen3-30B-A3B',
    label: 'qwen3-30b-a3b',
    detail: 'MoE · bf16 · 30.5B, 3.3B active',
    default_context: 32768,
    default_concurrency: 8,
  },
  {
    model_id: 'meta-llama/Llama-3.3-70B-Instruct',
    label: 'llama-3.3-70b',
    detail: 'dense · GQA 8:1 · bf16 · 70.6B',
    default_context: 131072,
    default_concurrency: 32,
  },
  {
    model_id: 'deepseek-ai/DeepSeek-V3',
    label: 'deepseek-v3',
    detail: 'MoE · MLA · fp8 · 671.0B',
    default_context: 32768,
    default_concurrency: 16,
  },
]
