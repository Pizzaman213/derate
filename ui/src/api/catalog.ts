// The model picker and the fixture-scenario registry: declarative data with no
// demo-generation logic attached, so it can sit underneath both build modes.
//
// PlanView needs a curated model list whether the backend is live or fixture;
// App's fixture controls need a list of scenario ids and labels only while in
// fixture mode. Neither needs the state machine that actually manufactures
// fixture data (that stays in fixtures.ts, which imports the lists below).
// fixtures.ts imports from this module, never the reverse -- a live build
// that never reaches fixtures.ts still has everything PlanView and App's
// non-fixture surface import.

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

/** Which fixture world to serve. The failure states are designed screens, so
 *  the stub has to be able to produce them on demand. Fixture mode only --
 *  kept here because it is registry data the fixture-mode picker renders, not
 *  part of the state machine that builds each scenario's fixture data. */
export type Scenario = 'nominal' | 'single-node' | 'node-down'

export const SCENARIOS: { id: Scenario; label: string }[] = [
  { id: 'nominal', label: 'Two Sparks serving' },
  { id: 'single-node', label: 'One node, nothing running' },
  { id: 'node-down', label: 'spark-02 unreachable' },
]
