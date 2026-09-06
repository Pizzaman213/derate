// Agent G's day-0 fixture stub, served in the browser.
//
// It exists so the UI renders completely before any coordinator is running, and
// so every state the brief names has something to render: a measured link and an
// unmeasured one, a deployment spanning two nodes, unequal replicas including one
// held at zero weight, a remote provider as a route target, and a refusal.
//
// Node profiles, link measurements and model shapes are copied from the frozen
// day-0 fixtures in tests/fixtures/, so the demo data here is the same demo data
// every other workstream tests against. The arithmetic in the fit breakdowns is
// derived from those shapes rather than invented.
//
// Deleted at integration.

import type {
  Candidate,
  Cluster,
  DeploymentDTO,
  LaunchRequest,
  MetricsFrame,
  PlanRequest,
  PlanResponse,
  Provider,
  RoutingConfig,
  Topology,
} from './types'

const GiB = 1024 ** 3
const gb = (n: number) => Math.round(n * GiB)

// contracts/constants.py
const GB10_TOTAL_MEMORY = 128 * GiB
const GB10_ADDRESSABLE = Math.trunc(119.7 * GiB)
const GB10_MEM_BANDWIDTH = 273.0
const DEFAULT_GUARDRAIL = 0.9
/** 107.7 GB. The line every fit is measured against. */
const USABLE_GB10 = Math.trunc(GB10_ADDRESSABLE * DEFAULT_GUARDRAIL)
const USABLE_3090 = Math.trunc(Math.trunc(23.6 * GiB) * DEFAULT_GUARDRAIL)

const CLUSTER_ID = 'c-8f21'
const now = () => Date.now() / 1000

/** Which fixture world to serve. The failure states are designed screens, so the
 *  stub has to be able to produce them on demand. Fixture mode only. */
export type Scenario = 'nominal' | 'single-node' | 'node-down'

export const SCENARIOS: { id: Scenario; label: string }[] = [
  { id: 'nominal', label: 'Two Sparks serving' },
  { id: 'single-node', label: 'One node, nothing running' },
  { id: 'node-down', label: 'spark-02 unreachable' },
]

// ── Node profiles: tests/fixtures/nodes.py ───────────────────────────────────

const PROFILES = {
  'spark-01': {
    node_id: 'spark-01',
    hostname: 'spark-01',
    address: '10.0.0.11',
    device_class: 'gb10' as const,
    gpu_name: 'NVIDIA GB10',
    gpu_count: 1,
    total_memory: GB10_TOTAL_MEMORY,
    addressable_memory: GB10_ADDRESSABLE,
    memory_bandwidth_gbps: GB10_MEM_BANDWIDTH,
    compute_capability: '12.1',
    driver_version: '580.65.06',
  },
  'spark-02': {
    node_id: 'spark-02',
    hostname: 'spark-02',
    address: '10.0.0.12',
    device_class: 'gb10' as const,
    gpu_name: 'NVIDIA GB10',
    gpu_count: 1,
    total_memory: GB10_TOTAL_MEMORY,
    addressable_memory: GB10_ADDRESSABLE,
    memory_bandwidth_gbps: GB10_MEM_BANDWIDTH,
    compute_capability: '12.1',
    driver_version: '580.65.06',
  },
  'ws-3090': {
    node_id: 'ws-3090',
    hostname: 'workstation',
    address: '10.0.0.50',
    device_class: 'discrete' as const,
    gpu_name: 'NVIDIA GeForce RTX 3090',
    gpu_count: 1,
    total_memory: 24 * GiB,
    addressable_memory: Math.trunc(23.6 * GiB),
    memory_bandwidth_gbps: 936.2,
    compute_capability: '8.6',
    driver_version: '580.65.06',
  },
}

const TELEMETRY_BASE: Record<
  string,
  { power: number; temp: number; mem_pct: number; util: number }
> = {
  'spark-01': { power: 71, temp: 62, mem_pct: 78, util: 94 },
  'spark-02': { power: 68, temp: 60, mem_pct: 74, util: 91 },
  'ws-3090': { power: 210, temp: 68, mem_pct: 31, util: 22 },
}

const STRENGTH: Record<string, number> = {
  'spark-01': 1.0,
  'spark-02': 1.0,
  'ws-3090': 0.34,
}

const ROLE: Record<string, 'coordinator' | 'worker'> = {
  'spark-01': 'coordinator',
  'spark-02': 'worker',
  'ws-3090': 'worker',
}

// The 3090 serves its own model perfectly well. It cannot join the gpt-oss-120b
// pipeline, and the roster says why rather than hiding the machine.
const INELIGIBLE: Record<string, string | null> = {
  'spark-01': null,
  'spark-02': null,
  'ws-3090':
    'Not in the gpt-oss-120b pool: compute capability 8.6 has no FP4 path, so MXFP4 weights would dequantise to 233 GB against 21.2 GB usable.',
}

// ── Links: tests/fixtures/nodes.py LINK_SPARK_PAIR ───────────────────────────

interface StubLink {
  src: string
  dst: string
  medium: string
  stale: boolean
  measured: boolean
  all_reduce_gbps?: number
  sendrecv_gbps?: number
  latency_us?: number
  gpudirect_rdma?: boolean
  method?: string
  measured_at?: number
}

const BASE_LINKS: StubLink[] = [
  {
    src: 'spark-01',
    dst: 'spark-02',
    all_reduce_gbps: 10.2,
    sendrecv_gbps: 9.0,
    latency_us: 40.0,
    gpudirect_rdma: false,
    measured_at: 1757193600.0,
    method: 'nccl-tests',
    medium: 'connectx-7',
    stale: false,
    measured: true,
  },
  {
    src: 'spark-01',
    dst: 'ws-3090',
    all_reduce_gbps: 1.1,
    sendrecv_gbps: 1.0,
    latency_us: 210.0,
    gpudirect_rdma: false,
    measured_at: 1757193600.0,
    method: 'nccl-tests',
    medium: 'ethernet',
    stale: false,
    measured: true,
  },
  // Never probed. Drawn dashed, carries no number, offers a measurement.
  { src: 'spark-02', dst: 'ws-3090', medium: 'ethernet', stale: false, measured: false },
]

// ── Plans and fits ───────────────────────────────────────────────────────────
// Every reason string here is written the way the planner and the fit gate emit
// them. The UI renders them verbatim and never rewrites them.

const PLAN_GPT_OSS = {
  kind: 'pipeline' as const,
  tensor_parallel: 1,
  pipeline_parallel: 2,
  expert_parallel: 1,
  data_parallel: 1,
  node_ids: ['spark-01', 'spark-02'],
  reason:
    'Pipeline parallel across 2 nodes: measured all-reduce is 10.2 GB/s, below the 40.0 GB/s tensor-parallel threshold, so PP costs one 21 MB activation handoff per microbatch instead of 36 all-reduces per token.',
  measured_link_gbps: 10.2,
  rejected: [
    'TP=2: link 10.2 GB/s below 40.0 GB/s all-reduce threshold; 36 all-reduces per token would dominate decode',
    'EP=2: cross-node expert parallel refused below 40.0 GB/s (measured 10.2 GB/s); 128 experts would put an all-to-all on the critical path',
    'SINGLE_NODE: 57.8 GB of weights plus 18.1 GB of KV exceeds 107.7 GB usable on one node at 32768 context',
  ],
}

const FIT_GPT_OSS = {
  verdict: 'fits' as const,
  breakdown: {
    weights: gb(28.9),
    kv_cache: gb(9.0),
    activations: gb(1.4),
    comm_buffers: gb(1.5),
    replicated: gb(0.6),
    framework_overhead: gb(1.0),
    total: gb(42.4),
  },
  usable_per_node: USABLE_GB10,
  headroom: USABLE_GB10 - gb(42.4),
  reason:
    'Fits on 2 pipeline stages with 65.3 GB of headroom per node. Weights are 57.8 GB at 0.53125 bytes/param, halved across stages. 18 of 36 layers use a 128-token sliding window, which holds KV to 18.1 GB at 32768 context and 16 concurrent sequences instead of 36.1 GB.',
  limiting_term: 'weights',
  max_context_that_fits: null,
  predicted_decode_tps: 100.2,
  warnings: [],
}

const PLAN_QWEN = {
  kind: 'single_node' as const,
  tensor_parallel: 1,
  pipeline_parallel: 1,
  expert_parallel: 1,
  data_parallel: 1,
  node_ids: ['spark-01'],
  reason:
    'Single node: 56.9 GB of bf16 weights and 24.0 GB of KV fit inside 107.7 GB usable on spark-01, so no cross-node communication is needed.',
  measured_link_gbps: 10.2,
  rejected: [
    'TP=2: model fits on one node; sharding would add an all-reduce per layer over a 10.2 GB/s link for no memory benefit',
    'EP=2: model fits on one node; cross-node expert parallel would put an all-to-all on the critical path for no memory benefit',
  ],
}

const FIT_QWEN = {
  verdict: 'fits' as const,
  breakdown: {
    weights: gb(56.9),
    kv_cache: gb(24.0),
    activations: gb(0.9),
    comm_buffers: gb(0.0),
    replicated: gb(0.0),
    framework_overhead: gb(1.0),
    total: gb(82.8),
  },
  usable_per_node: USABLE_GB10,
  headroom: USABLE_GB10 - gb(82.8),
  reason:
    'Fits on one node with 24.9 GB of headroom. 3.3B active parameters at bf16 is 6.7 GB read per token against 273.0 GB/s, predicting 40.9 tok/s single-stream decode.',
  limiting_term: 'weights',
  max_context_that_fits: null,
  predicted_decode_tps: 40.9,
  warnings: [],
}

const PLAN_QWEN_3090 = {
  ...PLAN_QWEN,
  node_ids: ['ws-3090'],
  reason:
    'Single node: 17.4 GB of q4_k_m weights fit 21.2 GB usable on ws-3090, leaving 2.3 GB for KV, which caps context at 4096 across 4 sequences.',
  measured_link_gbps: 1.1,
  rejected: [
    'bf16: 56.9 GB of weights exceeds 21.2 GB usable on ws-3090; requantised to q4_k_m at 0.6125 bytes/param',
    'PP=2 with spark-01: 1.1 GB/s ethernet handoff would cost more per microbatch than the stage saves',
  ],
}

const FIT_QWEN_3090 = {
  verdict: 'fits' as const,
  breakdown: {
    weights: gb(17.4),
    kv_cache: gb(1.5),
    activations: gb(0.5),
    comm_buffers: gb(0.0),
    replicated: gb(0.0),
    framework_overhead: gb(1.0),
    total: gb(20.4),
  },
  usable_per_node: USABLE_3090,
  headroom: USABLE_3090 - gb(20.4),
  reason:
    'Fits on ws-3090 with 0.8 GB of headroom at 4096 context. 936.2 GB/s of memory bandwidth decodes fast, but 2.3 GB of KV holds the batch to 4 sequences.',
  limiting_term: 'kv_cache',
  max_context_that_fits: 4096,
  predicted_decode_tps: 63.4,
  warnings: [
    'Context is 4096 against the pool default of 32768. Requests longer than 4096 tokens will be refused by this replica.',
  ],
}

// The refusal. Dense 70B at bf16, 131072 context, 32 concurrent sequences.
const PLAN_LLAMA = {
  kind: 'pipeline' as const,
  tensor_parallel: 1,
  pipeline_parallel: 2,
  expert_parallel: 1,
  data_parallel: 1,
  node_ids: ['spark-01', 'spark-02'],
  reason:
    'Pipeline parallel across 2 nodes: measured all-reduce is 10.2 GB/s, below the 40.0 GB/s tensor-parallel threshold, and 131.4 GB of bf16 weights does not fit on one node.',
  measured_link_gbps: 10.2,
  rejected: [
    'TP=2: link 10.2 GB/s below 40.0 GB/s all-reduce threshold; 80 all-reduces per token would dominate decode',
    'SINGLE_NODE: 131.4 GB of weights exceeds 107.7 GB usable on one node',
  ],
}

const FIT_LLAMA_WONT_FIT = {
  verdict: 'wont_fit' as const,
  breakdown: {
    weights: gb(65.7),
    kv_cache: gb(640.0),
    activations: gb(2.0),
    comm_buffers: gb(1.5),
    replicated: gb(0.0),
    framework_overhead: gb(1.0),
    total: gb(710.2),
  },
  usable_per_node: USABLE_GB10,
  headroom: USABLE_GB10 - gb(710.2),
  reason:
    'KV cache needs 640.0 GB at 131072 context across 32 concurrent sequences, but only 37.5 GB remains on each pipeline stage after 65.7 GB of weights. 8 KV heads at 128 head dim over 40 layers per stage is 160 KB per token. Reduce context to 7168, or hold 131072 context by dropping concurrency to 1.',
  limiting_term: 'kv_cache',
  max_context_that_fits: 7168,
  predicted_decode_tps: null,
  warnings: [
    'kv_dtype is fp16. Switching to fp8 halves the KV term and raises the fitting context to 14336.',
  ],
}

// Same model at the context that fits. It loads, and it is slow, and the UI says
// so next to the launch button rather than blocking.
const FIT_LLAMA_DEGRADED = {
  verdict: 'fits_degraded' as const,
  breakdown: {
    weights: gb(65.7),
    kv_cache: gb(35.0),
    activations: gb(2.0),
    comm_buffers: gb(1.5),
    replicated: gb(0.0),
    framework_overhead: gb(1.0),
    total: gb(105.2),
  },
  usable_per_node: USABLE_GB10,
  headroom: USABLE_GB10 - gb(105.2),
  reason:
    'Loads across 2 pipeline stages with 2.5 GB of headroom, but predicted decode is 3.9 tok/s: each stage reads 70.6 GB of weights per token at 273.0 GB/s, and no batch size hides that. This is below the 10.0 tok/s degraded threshold.',
  limiting_term: 'bandwidth',
  max_context_that_fits: 7168,
  predicted_decode_tps: 3.9,
  warnings: [
    'Headroom is 2.5 GB. A second deployment on either node will push this over the 0.90 guardrail.',
  ],
}

const PLAN_DEEPSEEK = {
  kind: 'pipeline' as const,
  tensor_parallel: 1,
  pipeline_parallel: 2,
  expert_parallel: 1,
  data_parallel: 1,
  node_ids: ['spark-01', 'spark-02'],
  reason:
    'Pipeline parallel across 2 nodes is the only shape left: expert parallel and tensor parallel are both refused at 10.2 GB/s, and the model does not fit on one node.',
  measured_link_gbps: 10.2,
  rejected: [
    'EP=2: cross-node expert parallel refused below 40.0 GB/s (measured 10.2 GB/s); 8 of 256 experts per token would put an all-to-all on every decode step',
    'TP=2: link 10.2 GB/s below 40.0 GB/s all-reduce threshold',
    'SINGLE_NODE: 624.9 GB of weights exceeds 107.7 GB usable on one node',
  ],
}

const FIT_DEEPSEEK = {
  verdict: 'wont_fit' as const,
  breakdown: {
    weights: gb(312.5),
    kv_cache: gb(17.2),
    activations: gb(2.0),
    comm_buffers: gb(1.5),
    replicated: gb(0.0),
    framework_overhead: gb(1.0),
    total: gb(334.2),
  },
  usable_per_node: USABLE_GB10,
  headroom: USABLE_GB10 - gb(334.2),
  reason:
    'Weights alone need 624.9 GB at fp8, or 312.5 GB per pipeline stage, against 107.7 GB usable per node. No context length makes this fit: 6 nodes at PP=6 would, or a q4_k_m requantisation across the current 2.',
  limiting_term: 'weights',
  max_context_that_fits: null,
  predicted_decode_tps: null,
  warnings: [
    'MLA caches 576 latent dims per layer per token, not kv_lora_rank alone. Charging 512 would under-count the cache by 12.5 percent.',
  ],
}

/** The picker: the four frozen shapes, plus a free-text HuggingFace ID field. */
export const CURATED_MODELS = [
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

// ── Deployments ──────────────────────────────────────────────────────────────

const ALL_DEPLOYMENTS: DeploymentDTO[] = [
  {
    deployment_id: 'd-1',
    served_name: 'gpt-oss-120b',
    model_id: 'openai/gpt-oss-120b',
    runtime: 'vllm',
    state: 'ready',
    context_length: 32768,
    max_concurrent_seqs: 16,
    started_at: 1757193600.0,
    last_error: null,
    node_ids: ['spark-01', 'spark-02'],
    plan: PLAN_GPT_OSS,
    fit: FIT_GPT_OSS,
  },
  {
    deployment_id: 'd-2',
    served_name: 'qwen3-30b-a3b',
    model_id: 'Qwen/Qwen3-30B-A3B',
    runtime: 'vllm',
    state: 'ready',
    context_length: 32768,
    max_concurrent_seqs: 8,
    started_at: 1757194100.0,
    last_error: null,
    node_ids: ['spark-01'],
    plan: PLAN_QWEN,
    fit: FIT_QWEN,
  },
  {
    deployment_id: 'd-3',
    served_name: 'qwen3-30b-a3b',
    model_id: 'Qwen/Qwen3-30B-A3B',
    runtime: 'vllm',
    state: 'ready',
    context_length: 32768,
    max_concurrent_seqs: 8,
    started_at: 1757194160.0,
    last_error: null,
    node_ids: ['spark-02'],
    plan: { ...PLAN_QWEN, node_ids: ['spark-02'] },
    fit: FIT_QWEN,
  },
  {
    deployment_id: 'd-4',
    served_name: 'qwen3-30b-a3b',
    model_id: 'Qwen/Qwen3-30B-A3B',
    runtime: 'vllm',
    state: 'ready',
    context_length: 4096,
    max_concurrent_seqs: 4,
    started_at: 1757194220.0,
    last_error: null,
    node_ids: ['ws-3090'],
    plan: PLAN_QWEN_3090,
    fit: FIT_QWEN_3090,
  },
]

const TPS_BASE: Record<string, number> = {
  'd-1': 127.4,
  'd-2': 44.6,
  'd-3': 43.1,
  'd-4': 8.2,
}

// ── Routing ──────────────────────────────────────────────────────────────────

function baseRouting(): RoutingConfig[] {
  return [
    {
      served_name: 'gpt-oss-120b',
      policy: 'local_first',
      sticky_ttl_s: 0,
      auto_selected: true,
      auto_reason:
        'Selected automatically: this served name has both a local deployment and a remote provider, so local capacity is used first and the remote is held as the overflow valve.',
      flow: 'local',
      targets: [
        {
          target_id: 'd-1',
          kind: 'local',
          backend_url: 'http://10.0.0.11:8001',
          weight: 1.0,
          outstanding: 3,
          healthy: true,
          admitting: true,
          strength: 1.0,
          cost_per_mtok: 0.0,
          node_ids: ['spark-01', 'spark-02'],
          display_name: 'spark-01 + spark-02 · PP 2',
        },
        {
          target_id: 'openrouter:openai/gpt-oss-120b',
          kind: 'remote',
          backend_url: 'https://openrouter.ai/api/v1',
          weight: 0.0,
          outstanding: 0,
          healthy: true,
          admitting: true,
          strength: 0.0,
          cost_per_mtok: 0.3,
          provider_id: 'openrouter',
          display_name: 'openai/gpt-oss-120b',
        },
      ],
    },
    {
      served_name: 'qwen3-30b-a3b',
      policy: 'weighted_capacity',
      sticky_ttl_s: 0,
      auto_selected: true,
      auto_reason:
        'Selected automatically: local target strengths differ by more than 25 percent (1.00, 0.97, 0.14), so traffic is split in proportion to measured decode throughput rather than evenly.',
      flow: null,
      targets: [
        {
          target_id: 'd-2',
          kind: 'local',
          backend_url: 'http://10.0.0.11:8002',
          weight: 0.51,
          outstanding: 2,
          healthy: true,
          admitting: true,
          strength: 1.0,
          cost_per_mtok: 0.0,
          node_ids: ['spark-01'],
          display_name: 'spark-01',
        },
        {
          target_id: 'd-3',
          kind: 'local',
          backend_url: 'http://10.0.0.12:8002',
          weight: 0.49,
          outstanding: 1,
          healthy: true,
          admitting: true,
          strength: 0.97,
          cost_per_mtok: 0.0,
          node_ids: ['spark-02'],
          display_name: 'spark-02',
        },
        {
          target_id: 'd-4',
          kind: 'local',
          backend_url: 'http://10.0.0.50:8002',
          weight: 0.0,
          outstanding: 0,
          healthy: true,
          admitting: true,
          strength: 0.14,
          cost_per_mtok: 0.0,
          node_ids: ['ws-3090'],
          display_name: 'ws-3090',
          zero_weight_reason:
            'Measured 8.2 tok/s sustained against 58.4 on the strongest target, 14 percent, below the 15 percent floor. Its 4096 context refuses most of this pool’s traffic, so it is held as failover only: a small share of requests here would still set the tail latency for the whole cluster.',
        },
      ],
    },
  ]
}

// ── Providers ────────────────────────────────────────────────────────────────
// api_key_ref is an environment variable NAME. No key material exists in this
// file, in the payload, or anywhere the UI can reach.

function baseProviders(): Provider[] {
  return [
    {
      provider_id: 'openrouter',
      kind: 'openrouter',
      display_name: 'OpenRouter',
      base_url: 'https://openrouter.ai/api/v1',
      api_key_ref: 'OPENROUTER_API_KEY',
      enabled: true,
      priority: 10,
      healthy: true,
      last_error: null,
      last_refreshed: 1757193000.0,
      spend_today_usd: 1.84,
      models: [
        {
          served_name: 'gpt-oss-120b',
          upstream_id: 'openai/gpt-oss-120b',
          context_length: 131072,
          supports_streaming: true,
          supports_tools: true,
          input_cost_per_mtok: 0.09,
          output_cost_per_mtok: 0.3,
        },
        {
          served_name: 'llama-3.3-70b',
          upstream_id: 'meta-llama/llama-3.3-70b-instruct',
          context_length: 131072,
          supports_streaming: true,
          supports_tools: true,
          input_cost_per_mtok: 0.12,
          output_cost_per_mtok: 0.3,
        },
      ],
    },
    {
      provider_id: 'lan-ollama',
      kind: 'ollama',
      display_name: 'Ollama on 10.0.0.44',
      base_url: 'http://10.0.0.44:11434/v1',
      api_key_ref: '',
      enabled: true,
      priority: 20,
      healthy: false,
      last_error: 'connection refused after 3 attempts, last at 12:04:11',
      last_refreshed: 1757192400.0,
      spend_today_usd: 0,
      models: [],
    },
    {
      provider_id: 'openai',
      kind: 'openai',
      display_name: 'OpenAI',
      base_url: 'https://api.openai.com/v1',
      api_key_ref: 'OPENAI_API_KEY',
      enabled: false,
      priority: 30,
      healthy: true,
      last_error: null,
      last_refreshed: 1757190000.0,
      spend_today_usd: 0,
      models: [],
    },
  ]
}

// ── Candidates ───────────────────────────────────────────────────────────────

function baseCandidates(): Candidate[] {
  return [
    {
      node_id: 'spark-03',
      hostname: 'spark-03',
      address: '10.0.0.13',
      device_class: 'gb10',
      gpu_name: 'NVIDIA GB10',
      total_memory: GB10_TOTAL_MEMORY,
      discovered_at: 1757194400.0,
      note: null,
    },
    {
      node_id: 'jetson-agx',
      hostname: 'jetson-agx',
      address: '10.0.0.61',
      device_class: 'unknown',
      gpu_name: 'Orin AGX 64GB',
      total_memory: 64 * GiB,
      discovered_at: 1757194460.0,
      note: 'Joins as a member, but 64 GB and a 1.0 GB/s link keep it out of the current gpt-oss-120b pool.',
    },
  ]
}

// ── Mutable stub state ───────────────────────────────────────────────────────
// Admitting a candidate, measuring a link and changing a policy all have to take
// effect without a reload, so the stub keeps state instead of returning
// constants.

let scenario: Scenario = 'nominal'
const listeners = new Set<() => void>()

const state = {
  admitted: new Set<string>(),
  /** Deployments started from the UI this session. */
  launched: [] as DeploymentDTO[],
  candidates: baseCandidates(),
  routing: baseRouting(),
  links: BASE_LINKS.map((l) => ({ ...l })),
  providers: baseProviders(),
}

export function getScenario(): Scenario {
  return scenario
}

export function setScenario(s: Scenario) {
  scenario = s
  state.admitted.clear()
  state.launched = []
  state.candidates = baseCandidates()
  state.routing = baseRouting()
  state.links = BASE_LINKS.map((l) => ({ ...l }))
  state.providers = baseProviders()
  listeners.forEach((fn) => fn())
}

export function onScenarioChange(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

/** Node ids present in the current scenario. */
function activeNodeIds(): string[] {
  if (scenario === 'single-node') return ['spark-01']
  return ['spark-01', 'spark-02', 'ws-3090']
}

function activeDeployments(): DeploymentDTO[] {
  if (scenario === 'single-node') return [...state.launched]
  if (scenario === 'node-down') {
    // The PP=2 deployment loses a stage. Replicas on the surviving nodes keep
    // serving, which is what the plan allows.
    return ALL_DEPLOYMENTS.map((d) =>
      d.node_ids.includes('spark-02')
        ? {
            ...d,
            state: d.node_ids.length > 1 ? ('degraded' as const) : ('failed' as const),
            last_error:
              d.node_ids.length > 1
                ? 'spark-02 missed 3 heartbeats at 12:41:07. Stage 2 of 2 is unreachable; in-flight requests are draining.'
                : 'spark-02 missed 3 heartbeats at 12:41:07. Replica stopped.',
          }
        : d,
    ).concat(state.launched)
  }
  return [...ALL_DEPLOYMENTS, ...state.launched]
}

function nodeHealth(nodeId: string): 'healthy' | 'degraded' | 'unreachable' {
  if (scenario === 'node-down' && nodeId === 'spark-02') return 'unreachable'
  return 'healthy'
}

function nodeError(nodeId: string): string | null {
  if (scenario === 'node-down' && nodeId === 'spark-02')
    return 'Missed 3 heartbeats. Last contact 12:41:07, 94 seconds ago. Values below are the last known reading.'
  return null
}

function activeLinks(): StubLink[] {
  const ids = activeNodeIds()
  return state.links.filter((l) => ids.includes(l.src) && ids.includes(l.dst))
}

function activeRouting(): RoutingConfig[] {
  if (scenario === 'single-node') return []
  if (scenario === 'node-down') {
    return state.routing.map((cfg) => ({
      ...cfg,
      targets: cfg.targets.map((t) =>
        t.node_ids?.includes('spark-02')
          ? { ...t, healthy: false, admitting: false, weight: 0, outstanding: 0 }
          : t,
      ),
    }))
  }
  return state.routing
}

// ── The endpoints ────────────────────────────────────────────────────────────

export const fixtures = {
  cluster(): Cluster {
    const nodes = activeNodeIds().map((id) => {
      const p = PROFILES[id as keyof typeof PROFILES]
      const b = TELEMETRY_BASE[id]!
      const health = nodeHealth(id)
      return {
        profile: p,
        healthy: health === 'healthy',
        state: health,
        role: ROLE[id]!,
        last_seen: health === 'healthy' ? now() : now() - 94,
        memory_used: Math.round((b.mem_pct / 100) * p.addressable_memory),
        power_watts: b.power,
        temperature_c: b.temp,
        utilization_pct: b.util,
        last_error: nodeError(id),
        eligible: INELIGIBLE[id] == null,
        ineligible_reason: INELIGIBLE[id] ?? null,
      }
    })
    return {
      summary: {
        cluster_id: CLUSTER_ID,
        coordinator: 'spark-01',
        node_count: nodes.length,
        healthy_count: nodes.filter((n) => n.healthy).length,
        total_addressable_memory: nodes.reduce(
          (a, n) => a + n.profile.addressable_memory,
          0,
        ),
      },
      nodes,
      links: activeLinks()
        .filter((l) => l.measured)
        .map((l) => ({
          src: l.src,
          dst: l.dst,
          all_reduce_gbps: l.all_reduce_gbps!,
          sendrecv_gbps: l.sendrecv_gbps!,
          latency_us: l.latency_us!,
          gpudirect_rdma: l.gpudirect_rdma!,
          measured_at: l.measured_at!,
          method: l.method!,
        })),
      deployments: activeDeployments(),
    }
  },

  topology(): Topology {
    const deployments = activeDeployments()
    return {
      cluster_id: CLUSTER_ID,
      coordinator: 'spark-01',
      nodes: activeNodeIds().map((id) => {
        const p = PROFILES[id as keyof typeof PROFILES]
        const b = TELEMETRY_BASE[id]!
        return {
          node_id: id,
          hostname: p.hostname,
          device_class: p.device_class,
          gpu_name: p.gpu_name,
          state: nodeHealth(id),
          role: ROLE[id]!,
          memory_used_pct: b.mem_pct,
          power_w: b.power,
          temp_c: b.temp,
          util_pct: b.util,
          strength: STRENGTH[id] ?? 0,
          deployments: deployments
            .filter((d) => d.node_ids.includes(id) && d.state !== 'failed')
            .map((d) => d.deployment_id),
          total_memory: p.total_memory,
          address: p.address,
          last_error: nodeError(id),
        }
      }),
      edges: activeLinks().map((l) => ({
        src: l.src,
        dst: l.dst,
        ...(l.measured
          ? {
              all_reduce_gbps: l.all_reduce_gbps,
              sendrecv_gbps: l.sendrecv_gbps,
              latency_us: l.latency_us,
              gpudirect_rdma: l.gpudirect_rdma,
            }
          : {}),
        medium: l.medium,
        stale: l.stale,
        measured: l.measured,
      })),
      deployments: deployments
        .filter((d) => d.state !== 'failed')
        .map((d) => ({
          deployment_id: d.deployment_id,
          served_name: d.served_name,
          node_ids: d.node_ids,
          state: d.state,
          plan: planShort(d.plan),
          tokens_per_sec: TPS_BASE[d.deployment_id] ?? 0,
        })),
    }
  },

  deployments: (): DeploymentDTO[] => activeDeployments(),

  candidates: (): Candidate[] =>
    state.candidates.filter((c) => !state.admitted.has(c.node_id)),

  routing: (): RoutingConfig[] => activeRouting(),

  providers: (): Provider[] => state.providers,

  setPolicy(servedName: string, policy: RoutingConfig['policy']): RoutingConfig {
    const cfg = state.routing.find((r) => r.served_name === servedName)
    if (!cfg) throw new Error(`no routing config for ${servedName}`)
    cfg.policy = policy
    // A human picked it, so it is no longer auto-selected.
    cfg.auto_selected = false
    cfg.auto_reason = null
    cfg.flow = policy === 'local_first' ? 'local' : null

    const local = cfg.targets.filter((t) => t.kind === 'local' && t.admitting)
    for (const t of cfg.targets) {
      if (policy === 'round_robin') {
        // Round robin ignores strength, so the 15 percent floor stops applying
        // and the slow replica starts taking an equal share. That is the point
        // of offering it: the unfairness becomes visible.
        t.weight = t.kind === 'local' && t.admitting ? 1 / local.length : 0
        t.zero_weight_reason = null
      } else if (policy === 'weighted_capacity') {
        const strong = Math.max(...local.map((x) => x.strength), 0)
        const floored = t.kind === 'local' && t.strength < 0.15 * strong
        const pool = local
          .filter((x) => x.strength >= 0.15 * strong)
          .reduce((a, x) => a + x.strength, 0)
        t.weight = t.kind === 'local' && !floored && pool > 0 ? t.strength / pool : 0
        t.zero_weight_reason = floored
          ? baseRouting()
              .find((r) => r.served_name === servedName)
              ?.targets.find((x) => x.target_id === t.target_id)?.zero_weight_reason ?? null
          : null
      } else {
        t.weight = t.kind === 'local' && t.admitting ? 1 / Math.max(local.length, 1) : 0
        t.zero_weight_reason = null
      }
      t.weight = Math.round(t.weight * 100) / 100
    }
    return cfg
  },

  admit(nodeId: string) {
    state.admitted.add(nodeId)
  },

  measureLink(a: string, b: string) {
    const l = state.links.find(
      (x) => (x.src === a && x.dst === b) || (x.src === b && x.dst === a),
    )
    if (!l) throw new Error(`no link between ${a} and ${b}`)
    Object.assign(l, {
      measured: true,
      stale: false,
      all_reduce_gbps: 1.0,
      sendrecv_gbps: 0.9,
      latency_us: 214.0,
      gpudirect_rdma: false,
      method: 'nccl-tests',
      measured_at: now(),
    })
    return l
  },

  /** The fit gate is a blocking gate, not a warning. A WONT_FIT verdict starts
   *  nothing, and says so with the fit gate's own words. */
  launch(req: LaunchRequest): DeploymentDTO {
    const { plan, fit } = fixtures.plan({
      model_id: req.model_id,
      context: req.context,
      concurrency: req.concurrency,
      target: req.target,
    })
    if (fit.verdict === 'wont_fit') throw new Error(fit.reason)

    const id = `d-${100 + state.launched.length}`
    const served =
      CURATED_MODELS.find((m) => m.model_id === req.model_id)?.label ??
      req.model_id.split('/').pop() ??
      req.model_id

    const deployment: DeploymentDTO = {
      deployment_id: id,
      served_name: served,
      model_id: req.model_id,
      runtime: req.runtime,
      state: 'launching',
      context_length: req.context,
      max_concurrent_seqs: req.concurrency,
      started_at: now(),
      last_error: null,
      node_ids: plan.node_ids,
      plan,
      fit,
    }
    state.launched.push(deployment)
    TPS_BASE[id] = fit.predicted_decode_tps ?? 0

    // Loading weights takes minutes in reality. Four seconds is enough for the
    // LAUNCHING state to be visible without making the stub tedious.
    window.setTimeout(() => {
      const d = state.launched.find((x) => x.deployment_id === id)
      if (d) d.state = 'ready'
      listeners.forEach((fn) => fn())
    }, 4000)

    return deployment
  },

  plan(req: PlanRequest): PlanResponse {
    const base = {
      model_id: req.model_id,
      context_length: req.context,
      concurrency: req.concurrency,
    }
    const id = req.model_id.toLowerCase()
    if (id.includes('gpt-oss')) return { ...base, plan: PLAN_GPT_OSS, fit: FIT_GPT_OSS }
    if (id.includes('qwen3-30b')) return { ...base, plan: PLAN_QWEN, fit: FIT_QWEN }
    if (id.includes('deepseek')) return { ...base, plan: PLAN_DEEPSEEK, fit: FIT_DEEPSEEK }
    // Llama 70B refuses at the requested context and degrades at the fitting one.
    const fit = req.context > 7168 ? FIT_LLAMA_WONT_FIT : FIT_LLAMA_DEGRADED
    return { ...base, plan: PLAN_LLAMA, fit }
  },
}

/** "PP 2", "TP 2 · PP 2", "single node". The short form the graph prints. */
export function planShort(plan: {
  tensor_parallel: number
  pipeline_parallel: number
  expert_parallel: number
}): string {
  const parts: string[] = []
  if (plan.tensor_parallel > 1) parts.push(`TP ${plan.tensor_parallel}`)
  if (plan.pipeline_parallel > 1) parts.push(`PP ${plan.pipeline_parallel}`)
  if (plan.expert_parallel > 1) parts.push(`EP ${plan.expert_parallel}`)
  return parts.length ? parts.join(' · ') : 'single node'
}

// ── Metrics stream ───────────────────────────────────────────────────────────
// One frame per second with plausible drift, so the hero number actually moves
// and the tabular-figure claim gets exercised.

const round1 = (n: number) => Math.round(n * 10) / 10
const clamp = (n: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, n))

export function fixtureFrame(t: number): MetricsFrame {
  const wobble = (seed: number, amp: number) =>
    amp * (Math.sin(t / (7 + seed)) * 0.6 + Math.sin(t / (2.3 + seed)) * 0.4)

  const deployments = activeDeployments()
  const nodes = activeNodeIds().map((id, i) => {
    const b = TELEMETRY_BASE[id]!
    // An unreachable node reports nothing. The UI keeps the last reading and
    // greys it rather than showing a zero that looks live.
    if (nodeHealth(id) === 'unreachable') {
      return {
        node_id: id,
        power_w: null,
        temp_c: null,
        memory_used_pct: null,
        util_pct: null,
      }
    }
    return {
      node_id: id,
      power_w: round1(b.power + wobble(i, b.power * 0.06)),
      temp_c: round1(b.temp + wobble(i + 3, 1.6)),
      memory_used_pct: round1(clamp(b.mem_pct + wobble(i + 5, 0.8), 0, 100)),
      util_pct: round1(clamp(b.util + wobble(i + 9, 5), 0, 100)),
    }
  })

  const live = deployments.filter((d) => d.state === 'ready' || d.state === 'degraded')
  const hero = live.find((d) => d.deployment_id === 'd-1')
  const degraded = hero?.state === 'degraded'

  const frames = live.map((d) => {
    const base = TPS_BASE[d.deployment_id] ?? 0
    // A degraded PP deployment is down a stage: throughput falls, it does not
    // stop.
    const scale = d.state === 'degraded' ? 0.32 : 1
    return {
      deployment_id: d.deployment_id,
      state: d.state,
      tokens_per_sec: round1(base * scale + wobble(d.deployment_id.length, base * 0.06)),
      ttft_ms: Math.round((degraded ? 410 : 142) + wobble(2, 26)),
      queue_depth: Math.max(0, Math.round((degraded ? 11 : 3) + wobble(6, 3))),
    }
  })

  const clusterTps = frames.reduce((a, f) => a + (f.tokens_per_sec ?? 0), 0)
  const power = nodes.reduce((a, n) => a + (n.power_w ?? 0), 0)

  return {
    ts: t,
    cluster: {
      tokens_per_sec: frames.length ? round1(clusterTps) : null,
      total_power_w: round1(power),
      cache_hit_pct: frames.length ? round1(clamp(84 + wobble(4, 4), 0, 100)) : null,
    },
    nodes,
    deployments: frames,
  }
}
