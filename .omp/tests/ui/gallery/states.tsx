// Every variant × state of every registered component, as a flat list the
// style guide renders statically and the browser gallery mounts live.
//
// One entry per registry state. `interactive` entries need a browser (they
// hold state or depend on providers) and are skipped by the static style
// guide. Keep the fixtures small and REAL: strings the server actually sends,
// shapes from api/types.ts, so the gallery shows the product and not a model
// of it.

import { useState, type ReactNode } from 'react'
import { Readout } from '../../../../ui/src/components/Readout'
import { Lamp } from '../../../../ui/src/components/Lamp'
import { SegmentBar, ProportionBar } from '../../../../ui/src/components/Bars'
import { Verbatim, VerbatimList } from '../../../../ui/src/components/Verbatim'
import { Copyable } from '../../../../ui/src/components/Copyable'
import { OverrideGate } from '../../../../ui/src/components/OverrideGate'
import { CacheTable } from '../../../../ui/src/components/CacheTable'
import { Section, Disclosure } from '../../../../ui/src/components/Panel'
import { Popover } from '../../../../ui/src/components/Popover'
import { Icon } from '../../../../ui/src/components/Icon'
import { HostTable } from '../../../../ui/src/components/HostTable'
import { IssueList } from '../../../../ui/src/components/IssueList'
import { BlockFrame } from '../../../../ui/src/components/BlockFrame'
import { ProviderSignInView } from '../../../../ui/src/tabs/settings/ProviderSignIn'
import { PasswordField } from '../../../../ui/src/components/PasswordField'
import { Banner } from '../../../../ui/src/components/Banner'
import { PackChart } from '../../../../ui/src/components/PackChart'
import { ICON_NAMES } from '../../../design/icons/components/icons.ts'
import type { NodeStorage, ProviderBackendOption } from '../../../../ui/src/api/types'

export type GalleryState = {
  component: string
  variant: string
  state: string
  /** Needs a browser: holds state, or depends on the provider tree. */
  interactive?: boolean
  render: () => ReactNode
}

const HOSTS: ProviderBackendOption[] = [
  { tag: 'anthropic', provider_name: 'Anthropic', context_length: 200000, input_cost_per_mtok: 3, output_cost_per_mtok: 15, quantization: null, pinned: false },
  { tag: 'google-vertex', provider_name: 'Google Vertex', context_length: 200000, input_cost_per_mtok: 3.5, output_cost_per_mtok: 17.5, quantization: null, pinned: false },
  { tag: 'amazon-bedrock', provider_name: 'Amazon Bedrock', context_length: 200000, input_cost_per_mtok: 3, output_cost_per_mtok: 15.5, quantization: 'fp8', pinned: false },
]

const REFUSAL =
  'WONT_FIT: weights 68.2 GiB + kv_cache 24.0 GiB at 32768 ctx exceeds usable 88.1 GiB on spark-01 by 4.1 GiB. Reduce --max-model-len to 24576, or quantize to Q4_K_M (38.1 GiB).'
const PLAN = 'TP=2 across spark-01, spark-02 over 25 GbE (measured 2.94 GB/s); PP=1; EP=1.'
/** What `POST /api/keys` returns as `secret_once`: `drt_<10-char key id>_<token_urlsafe(32)>`,
 *  i.e. a 43-char urlsafe secret. `long` is the same shape with no break opportunity
 *  but `-`/`_`, which is what has to wrap inside the card. */
export const REVEAL_FIXTURES = {
  default: { keyId: 'k3v9q2m7xa', secret: 'drt_k3v9q2m7xa_Zq8Lr4WnT1yHc6Pb0VdKs9EuXa3Gm7Jf2NoRi5Ct8Yw' },
  long: { keyId: 'abcdefghij', secret: 'drt_abcdefghij_aB3-xY9_kL2mN8pQ4rS7tU1vW5zA0cD6eF-gH_iJ2kL' },
} as const

const storage = (repos: NodeStorage['models']): NodeStorage => ({
  node_id: 'spark-01',
  filesystems: [],
  estate: [],
  unreadable: [],
  available: true,
  reason: null,
  models: repos,
})
const CACHE_ROWS: NodeStorage = storage({
  available: true,
  path: '/cache/huggingface/hub',
  total_bytes: 61_523_000_000,
  reason: null,
  repos: [
    { folder: 'models--Qwen--Qwen3-30B-A3B', repo_id: 'Qwen/Qwen3-30B-A3B', bytes: 32_770_000_000, blob_count: 17, revisions: ['main'], last_modified: 1763390000 },
    { folder: 'models--openai--gpt-oss-120b', repo_id: 'openai/gpt-oss-120b', bytes: 25_330_000_000, blob_count: 15, revisions: ['main'], last_modified: 1763300000 },
    { folder: 'models--Qwen--Qwen2.5-0.5B', repo_id: 'Qwen/Qwen2.5-0.5B', bytes: 988_000_000, blob_count: 3, revisions: ['main'], last_modified: null },
  ],
})
const CACHE_EMPTY: NodeStorage = storage({ available: true, path: '/cache/huggingface/hub', total_bytes: 0, reason: null, repos: [] })
const CACHE_UNAVAILABLE: NodeStorage = storage({ available: false, path: null, total_bytes: null, reason: 'no cache mount at /cache/huggingface: the container was started without -v', repos: [] })

function Stateful<T>({ initial, children }: { initial: T; children: (v: T, set: (v: T) => void) => ReactNode }) {
  const [v, set] = useState(initial)
  return <>{children(v, set)}</>
}

/** What `POST /api/sign-in/chatgpt` answers and what the browser lands on: the
 *  real endpoint and parameter names, made-up values. The error is the
 *  coordinator's `sign_in_state_mismatch` refusal. */
const SIGN_IN_AUTHORIZE =
  'https://auth.openai.com/api/accounts/authorize?client_id=dynamic_agent_client&agent_name_hint=derate&response_type=code&redirect_uri=http%3A%2F%2F127.0.0.1%3A1455%2Fauth%2Fcallback&code_challenge_method=S256&state=Qm9x'
const SIGN_IN_CALLBACK =
  'http://127.0.0.1:1455/auth/callback?code=ac_7Hq2LmN0pRsT4uVwX&state=Qm9x&scope=openid+profile+email+offline_access+resource.invoke+chatgpt.tokens.use.direct&client_id=oaiapp_3fK9'
const SIGN_IN_ERROR =
  'That address belongs to a different sign-in attempt (its state does not match). Start again and paste the address from the newest tab.'

const SEGMENTS = [
  { key: 'weights', label: 'weights', bytes: 68.2 * 2 ** 30 },
  { key: 'kv', label: 'kv cache', bytes: 24 * 2 ** 30, limiting: true },
  { key: 'act', label: 'activations', bytes: 2.1 * 2 ** 30 },
]
const GiB = 2 ** 30

export const STATES: GalleryState[] = [
  // Readout: every tone × size, plus stale and null
  ...(['ink', 'live', 'warn', 'fault', 'muted'] as const).flatMap((tone) =>
    (['xl', 'readout', 'body'] as const).map((size) => ({
      component: 'Readout', variant: `${tone}-${size}`, state: 'default',
      render: () => <Readout value={119.7} decimals={1} unit="GB" width={5} size={size} tone={tone} />,
    })),
  ),
  { component: 'Readout', variant: 'ink-readout', state: 'stale', render: () => <Readout value={273} unit="GB/s" width={4} size="readout" stale /> },
  { component: 'Readout', variant: 'ink-readout', state: 'null', render: () => <Readout value={null} unit="tok/s" width={4} size="readout" /> },

  // Lamp: signal × filled/hollow
  ...(['live', 'warn', 'fault', 'idle'] as const).flatMap((signal) => [
    { component: 'Lamp', variant: signal, state: 'default', render: () => <Lamp signal={signal} label={`${signal} node`} /> },
    { component: 'Lamp', variant: signal, state: 'hollow', render: () => <Lamp signal={signal} hollow label={`${signal} node, stream down`} /> },
  ]),

  // Bars
  { component: 'SegmentBar', variant: 'default', state: 'fits', render: () => <SegmentBar segments={SEGMENTS.map((s) => ({ ...s, limiting: false }))} usable={110 * GiB} ceiling={128 * GiB} /> },
  { component: 'SegmentBar', variant: 'default', state: 'overrun', render: () => <SegmentBar segments={SEGMENTS} usable={88.1 * GiB} ceiling={128 * GiB} /> },
  { component: 'SegmentBar', variant: 'default', state: 'limiting', render: () => <SegmentBar segments={SEGMENTS} usable={100 * GiB} /> },
  { component: 'SegmentBar', variant: 'default', state: 'no-usable', render: () => <SegmentBar segments={SEGMENTS} usable={null} /> },
  ...(['ink', 'live', 'warn', 'fault', 'muted'] as const).map((tone) => ({
    component: 'ProportionBar', variant: tone, state: 'value', render: () => <ProportionBar value={0.62} tone={tone} label={`${tone} share 62%`} width={160} />,
  })),
  { component: 'ProportionBar', variant: 'ink', state: 'null', render: () => <ProportionBar value={null} label="no reading" width={160} /> },
  { component: 'ProportionBar', variant: 'ink', state: 'zero', render: () => <ProportionBar value={0} label="0%" width={160} /> },
  { component: 'ProportionBar', variant: 'ink', state: 'full', render: () => <ProportionBar value={1} label="100%" width={160} /> },

  // Verbatim
  ...(['body', 'label', 'unit'] as const).map((size) => ({
    component: 'Verbatim', variant: size, state: 'default', render: () => <Verbatim text={REFUSAL} size={size} />,
  })),
  { component: 'Verbatim', variant: 'body', state: 'multiline', render: () => <Verbatim text={`${PLAN}\n${REFUSAL}`} /> },
  { component: 'VerbatimList', variant: 'default', state: 'items', render: () => <VerbatimList items={['spark-03: 24 GiB free, needs 38 GiB', 'spark-04: unreachable since 09:14']} /> },
  { component: 'VerbatimList', variant: 'default', state: 'empty', render: () => <div data-empty><VerbatimList items={[]} /></div> },

  // Copyable
  { component: 'Copyable', variant: 'default', state: 'idle', render: () => <Copyable text="curl -fsSL http://spark-01:8080/install.sh | sh -s -- --join 192.168.0.172:8080 --token D7BKuM0t" /> },
  { component: 'Copyable', variant: 'default', state: 'copied', interactive: true, render: () => <Copyable text="curl -fsSL http://spark-01:8080/install.sh | sh" label="Copy" /> },
  { component: 'Copyable', variant: 'default', state: 'failed', interactive: true, render: () => <Copyable text="curl -fsSL http://spark-01:8080/install.sh | sh" label="Copy" /> },

  // OverrideGate
  { component: 'OverrideGate', variant: 'default', state: 'unchecked', render: () => <OverrideGate reason={REFUSAL} sentence="I understand spark-01 has 4.1 GiB less free than this needs right now and the launch may fail at load." checked={false} onChange={() => {}} onLaunch={() => {}} launching={false} /> },
  { component: 'OverrideGate', variant: 'default', state: 'checked', render: () => <OverrideGate reason={REFUSAL} sentence="I understand spark-01 has 4.1 GiB less free than this needs right now and the launch may fail at load." checked onChange={() => {}} onLaunch={() => {}} launching={false} /> },
  { component: 'OverrideGate', variant: 'default', state: 'launching', render: () => <OverrideGate reason={REFUSAL} sentence="I understand spark-01 has 4.1 GiB less free than this needs right now and the launch may fail at load." checked onChange={() => {}} onLaunch={() => {}} launching /> },
  { component: 'OverrideGate', variant: 'default', state: 'stacked', render: () => (
    <div style={{ display: 'grid', gap: 'var(--space-12)' }}>
      <OverrideGate reason={REFUSAL} sentence="I understand spark-01 has 4.1 GiB less free than this needs." checked onChange={() => {}} launching={false} />
      <OverrideGate reason="Qwen3ForCausalLM is not in VLLM_ARCHITECTURES for ghcr.io/pizzaman213/derate/vllm:2026-11-01." sentence="I understand this architecture has not been run through this image." checked={false} onChange={() => {}} launching={false} />
    </div>
  ) },

  // CacheTable
  { component: 'CacheTable', variant: 'default', state: 'rows', render: () => <CacheTable node={CACHE_ROWS} servedFolders={new Set(['models--Qwen--Qwen3-30B-A3B'])} onDelete={() => {}} busy={null} /> },
  { component: 'CacheTable', variant: 'default', state: 'empty', render: () => <CacheTable node={CACHE_EMPTY} /> },
  { component: 'CacheTable', variant: 'default', state: 'busy', render: () => <CacheTable node={CACHE_ROWS} servedFolders={new Set()} onDelete={() => {}} busy="models--openai--gpt-oss-120b" /> },
  { component: 'CacheTable', variant: 'default', state: 'unavailable', render: () => <CacheTable node={CACHE_UNAVAILABLE} /> },
  { component: 'CacheTable', variant: 'default', state: 'read-only', render: () => <CacheTable node={CACHE_ROWS} /> },

  // Section / Disclosure / Popover
  { component: 'Section', variant: 'default', state: 'default', render: () => <Section title="Interconnect"><p>Measured 2.94 GB/s between spark-01 and spark-02.</p></Section> },
  { component: 'Section', variant: 'default', state: 'aside', render: () => <Section title="Interconnect" aside={<span className="unit">measured 09:14</span>}><p>Measured 2.94 GB/s between spark-01 and spark-02.</p></Section> },
  { component: 'Disclosure', variant: 'default', state: 'closed', interactive: true, render: () => <Stateful initial={false}>{(open, set) => <Disclosure summary="Why this plan" open={open} onToggle={() => set(!open)}><Verbatim text={PLAN} size="label" /></Disclosure>}</Stateful> },
  { component: 'Disclosure', variant: 'default', state: 'open', interactive: true, render: () => <Stateful initial={true}>{(open, set) => <Disclosure summary="Why this plan" open={open} onToggle={() => set(!open)}><Verbatim text={PLAN} size="label" /></Disclosure>}</Stateful> },
  { component: 'Popover', variant: 'default', state: 'closed', interactive: true, render: () => <Popover label="Nodes" trigger={<span>2 nodes</span>}>{(close) => <div style={{ display: 'grid', gap: 'var(--space-6)' }}><label><input type="checkbox" defaultChecked /> spark-01</label><label><input type="checkbox" /> spark-02</label><button type="button" onClick={close}>Done</button></div>}</Popover> },
  { component: 'Popover', variant: 'default', state: 'open', interactive: true, render: () => <Popover label="Nodes" trigger={<span>2 nodes</span>}>{(close) => <div style={{ display: 'grid', gap: 'var(--space-6)' }}><label><input type="checkbox" defaultChecked /> spark-01</label><label><input type="checkbox" /> spark-02</label><button type="button" onClick={close}>Done</button></div>}</Popover> },

  // Sheet: provider-bound, rendered only by the browser gallery (main.tsx mounts it)
  ...(['node', 'dep', 'model'] as const).map((kind) => ({
    component: 'Sheet', variant: kind, state: kind, interactive: true, render: () => <div data-sheet-kind={kind} />,
  })),

  // RevealModal: a portal over the whole page, so like the Sheet it is mounted
  // only by ?only=RevealModal&variant=<state> (main.tsx); here a placeholder.
  ...(Object.keys(REVEAL_FIXTURES) as (keyof typeof REVEAL_FIXTURES)[]).map((state) => ({
    component: 'RevealModal', variant: 'default', state, interactive: true, render: () => <div data-reveal-state={state} />,
  })),

  // Icon: every name at every size
  ...([12, 14, 16, 20, 24] as const).map((size) => ({
    component: 'Icon', variant: String(size), state: 'every-name',
    render: () => (
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--space-8)' }}>
        {ICON_NAMES.map((name) => <Icon key={name} name={name} size={size} label={name} />)}
      </div>
    ),
  })),

  // CSS patterns, via plain elements with the product classes
  ...(['', 'ghost'] as const).flatMap((cls) => [
    { component: 'Button', variant: cls || 'bare', state: 'default', render: () => <button type="button" className={cls || undefined}>Serve</button> },
    { component: 'Button', variant: cls || 'bare', state: 'disabled', render: () => <button type="button" className={cls || undefined} disabled>Serve</button> },
    { component: 'Button', variant: cls || 'bare', state: 'icon-only', render: () => <button type="button" className={cls || undefined} aria-label="Close"><Icon name="close" size={12} decorative /></button> },
    { component: 'Button', variant: cls || 'bare', state: 'loading', render: () => <button type="button" className={cls || undefined} aria-busy="true">Killing…</button> },
  ]),
  { component: 'Button', variant: 'chips', state: 'pressed', render: () => <div className="chips"><button type="button" aria-pressed="true">1h</button><button type="button" aria-pressed="false">24h</button></div> },

  { component: 'Input', variant: 'input', state: 'default', render: () => <input aria-label="Served name" defaultValue="Qwen3-30B-A3B" /> },
  { component: 'Input', variant: 'input', state: 'placeholder', render: () => <input aria-label="Search models" placeholder="Search models, on device and on the hub" /> },
  { component: 'Input', variant: 'input', state: 'disabled', render: () => <input aria-label="Served name" defaultValue="Qwen3-30B-A3B" disabled /> },
  { component: 'Input', variant: 'input', state: 'error', render: () => <div><input aria-label="Context length" defaultValue="99999" aria-invalid="true" aria-describedby="g-ctx-err" /><div id="g-ctx-err" className="unit" style={{ color: 'var(--fault)' }}>32768 is the largest context this model declares.</div></div> },
  { component: 'Input', variant: 'select', state: 'default', render: () => <select aria-label="Colour scheme" defaultValue="system"><option value="system">System</option><option value="light">Light</option><option value="dark">Dark</option><option value="high-contrast">High contrast</option></select> },
  { component: 'Input', variant: 'textarea', state: 'default', render: () => <textarea aria-label="Message" rows={3} defaultValue="Summarise the fit gate's refusal in one sentence." /> },

  { component: 'Card', variant: 'card', state: 'default', render: () => <div className="card"><h3>Headroom</h3><p>88.1 GB usable of 119.7 GB on spark-01.</p></div> },
  { component: 'Card', variant: 'card2', state: 'default', render: () => <div className="card2"><h3>Appearance</h3><div className="row"><span>Colour scheme</span><span className="mono">system</span></div></div> },
  { component: 'Card', variant: 'card', state: 'empty', render: () => <div className="card"><h3>Deployments</h3><p className="unit">Nothing is serving — pick a model on the Models screen.</p></div> },
  { component: 'Card', variant: 'card', state: 'error', render: () => <div className="card"><h3>Plan</h3><Verbatim text={REFUSAL} size="label" /></div> },

  { component: 'Chip', variant: 'pill', state: 'default', render: () => <span className="pill">serving</span> },
  { component: 'Chip', variant: 'pill mono', state: 'default', render: () => <span className="pill mono">2 nodes · 2 healthy · 1 serving</span> },
  { component: 'Chip', variant: 'chips button', state: 'toggle', render: () => <div className="chips"><button type="button" aria-pressed="false">p50</button></div> },
  { component: 'Chip', variant: 'chips button', state: 'toggle-pressed', render: () => <div className="chips"><button type="button" aria-pressed="true">p99</button></div> },

  { component: 'Nav', variant: 'default', state: 'default', render: () => <nav className="dest"><a href="#dashboard">Dashboard</a><a href="#models">Models</a><a href="#cluster">Cluster</a></nav> },
  { component: 'Nav', variant: 'default', state: 'current', render: () => <nav className="dest"><a href="#dashboard">Dashboard</a><a href="#models" aria-current="page">Models</a><a href="#cluster">Cluster</a></nav> },

  { component: 'Table', variant: 'default', state: 'rows', render: () => (
    <table>
      <thead><tr><th scope="col">Node</th><th scope="col" className="num">Free, GB</th><th scope="col" className="num">Link, GB/s</th></tr></thead>
      <tbody><tr><td className="mono">spark-01</td><td className="num">88.1</td><td className="num">2.94</td></tr><tr><td className="mono">spark-02</td><td className="num">102.4</td><td className="num">2.91</td></tr></tbody>
    </table>
  ) },
  { component: 'Table', variant: 'default', state: 'empty', render: () => (
    <table>
      <thead><tr><th scope="col">Node</th><th scope="col" className="num">Free, GB</th></tr></thead>
      <tbody><tr><td colSpan={2} className="unit">No nodes yet — Settings → Add a node.</td></tr></tbody>
    </table>
  ) },
  // scaffold:Banner:start
  { component: 'Banner', variant: 'fault', state: 'default', render: () => <Banner tone="fault">spark-02 unreachable since 2m ago · qwen2.5:0.5b: 1 of 2 targets down (circuit open)</Banner> },
  { component: 'Banner', variant: 'warn', state: 'default', render: () => <Banner tone="warn">qwen2.5:0.5b: 1 of 2 targets down (circuit half open)</Banner> },
  { component: 'Banner', variant: 'fault', state: 'with-action', render: () => <Banner tone="fault" action={{ label: 'See requests', href: '#requests' }}>spark-02 unreachable since 2m ago · 3 requests failed over since then</Banner> },
  // scaffold:Banner:end
  // scaffold:PackChart:start
  { component: 'PackChart', variant: 'pie', state: 'default', render: () => <PackChart type="pie" title="Request outcomes" height={240} data={[{ label: 'Rate limited', value: 1 }, { label: 'Not rate limited', value: 72 }]} /> },
  { component: 'PackChart', variant: 'donut', state: 'default', render: () => <PackChart type="donut" title="Request outcomes" height={240} options={{ centerLabel: 'Requests' }} data={[{ label: 'Rate limited', value: 1 }, { label: 'Not rate limited', value: 72 }]} /> },
  { component: 'PackChart', variant: 'stacked-bar', state: 'default', render: () => <PackChart type="stacked-bar" title="Tokens per day" height={200} options={{ valueFormat: 'si' }} data={[
    { label: 'Nov 16', series: 'local', value: 820 }, { label: 'Nov 16', series: 'cloud', value: 310 },
    { label: 'Nov 17', series: 'local', value: 0 }, { label: 'Nov 17', series: 'cloud', value: 0 },
    { label: 'Nov 18', series: 'local', value: 1226 }, { label: 'Nov 18', series: 'cloud', value: 540 },
  ]} /> },
  { component: 'PackChart', variant: 'stacked-bar-100', state: 'default', render: () => <PackChart type="stacked-bar-100" title="Local vs cloud" height={120} options={{ orientation: 'horizontal', showValues: true }} data={[
    { label: 'Tokens', series: 'local', value: 2046 }, { label: 'Tokens', series: 'cloud', value: 850 },
    { label: 'Requests', series: 'local', value: 27 }, { label: 'Requests', series: 'cloud', value: 9 },
  ]} /> },
  { component: 'PackChart', variant: 'stacked-bar', state: 'empty', render: () => <PackChart type="stacked-bar" title="Tokens per day" height={200} options={{ emptyMessage: 'No requests in this window.' }} data={[]} /> },
  { component: 'PackChart', variant: 'grouped-bar', state: 'default', render: () => <PackChart type="grouped-bar" title="Speed per model" height={200} options={{ orientation: 'horizontal', valueFormat: 'integer', xTitle: 'ms' }} data={[
    { label: 'qwen2.5-0.5b', series: 'TTFT avg', value: 42 }, { label: 'qwen2.5-0.5b', series: 'latency avg', value: 310 }, { label: 'qwen2.5-0.5b', series: 'latency p95, worst day', value: 820 },
    { label: 'qwen3.8-27b', series: 'TTFT avg', value: 180 }, { label: 'qwen3.8-27b', series: 'latency avg', value: 1900 }, { label: 'qwen3.8-27b', series: 'latency p95, worst day', value: 4100 },
  ]} /> },
  { component: 'PackChart', variant: 'multi-line', state: 'default', render: () => <PackChart type="multi-line" title="Failures per day, by error type" height={200} options={{ xType: 'time', valueFormat: 'integer', points: 'never' }} data={[
    { x: '2026-11-16', series: 'timeout', y: 0 }, { x: '2026-11-17', series: 'timeout', y: 3 }, { x: '2026-11-18', series: 'timeout', y: 1 },
    { x: '2026-11-16', series: 'upstream_5xx', y: 2 }, { x: '2026-11-17', series: 'upstream_5xx', y: 0 }, { x: '2026-11-18', series: 'upstream_5xx', y: 0 },
  ]} /> },
  // scaffold:PackChart:end
  // scaffold:PasswordField:start
  { component: 'PasswordField', variant: 'default', state: 'hidden', interactive: true, render: () => <Stateful initial="hunter2hunter2">{(v, set) => <div style={{ display: 'grid', gap: 'var(--space-6)' }}><label className="label" htmlFor="pw-hidden">Password</label><PasswordField id="pw-hidden" value={v} onChange={set} autoComplete="current-password" /></div>}</Stateful> },
  { component: 'PasswordField', variant: 'default', state: 'shown', interactive: true, render: () => <Stateful initial="hunter2hunter2">{(v, set) => <div style={{ display: 'grid', gap: 'var(--space-6)' }}><label className="label" htmlFor="pw-shown">Password</label><PasswordField id="pw-shown" value={v} onChange={set} autoComplete="new-password" /></div>}</Stateful> },
  { component: 'PasswordField', variant: 'default', state: 'disabled', render: () => <div style={{ display: 'grid', gap: 'var(--space-6)' }}><label className="label" htmlFor="pw-disabled">Password</label><PasswordField id="pw-disabled" value="hunter2hunter2" onChange={() => {}} autoComplete="current-password" disabled /></div> },
  { component: 'PasswordField', variant: 'default', state: 'error', interactive: true, render: () => (
    <Stateful initial="short">{(v, set) => (
      <div style={{ display: 'grid', gap: 'var(--space-6)' }}>
        <label className="label" htmlFor="pw-error">New password</label>
        <PasswordField id="pw-error" value={v} onChange={set} autoComplete="new-password" aria-invalid aria-describedby="pw-error-note" />
        <span id="pw-error-note" className="label">password must be at least 12 characters</span>
      </div>
    )}</Stateful>
  ) },
  // scaffold:PasswordField:end

  // ProviderSignIn: both flows share a layout with distinct consent/callbacks.
  ...(['chatgpt', 'claude'] as const).flatMap((flow) =>
    (['start', 'paste', 'error', 'done'] as const).map((state) => ({
      component: 'ProviderSignIn', variant: flow, state, interactive: true, render: () => (
        <Stateful initial={state === 'paste' || state === 'error' ? (flow === 'claude' ? 'test-code#Qm9x' : SIGN_IN_CALLBACK) : ''}>{(v, set) => (
          <ProviderSignInView
            flow={flow}
            step={state === 'error' ? 'paste' : state}
            reauth={false}
            authorizeUrl={state === 'start' ? null : flow === 'claude' ? 'https://claude.ai/oauth/authorize?state=Qm9x' : SIGN_IN_AUTHORIZE}
            callbackUrl={v}
            onCallbackUrl={set}
            busy={false}
            error={state === 'error' ? SIGN_IN_ERROR : null}
            onContinue={() => {}}
            onFinish={() => {}}
            onDone={() => {}}
            onCancel={() => {}}
          />
        )}</Stateful>
      ),
    }))),
  // scaffold:BlockFrame:start
  { component: 'BlockFrame', variant: 'default', state: 'default', render: () => <BlockFrame title="spark-01" sub="GB10 · 128 GB" icon="server"><span className="unit">Llama-3.3-70B-Instruct · FP8</span></BlockFrame> },
  { component: 'BlockFrame', variant: 'default', state: 'selected', render: () => <BlockFrame title="Rate limit" icon="gauge" selected onRemove={() => undefined}><span className="unit">caps llama-3.3-70b</span></BlockFrame> },
  { component: 'BlockFrame', variant: 'default', state: 'with-lamp', render: () => <BlockFrame title="spark-02" icon="server" signal={{ value: 'warn', label: 'spark-02 is degraded' }} tone="warn" meta={<span className="pill">2 models</span>} footer={<span className="unit">deploy here</span>}><span className="unit">Qwen3-Next-80B-A3B · FP8</span></BlockFrame> },
  // scaffold:BlockFrame:end
  // scaffold:IssueList:start
  { component: 'IssueList', variant: 'default', state: 'errors', render: () => <IssueList label="Checks" issues={[{ id: 'a', level: 'error', message: 'Wire "deploy to" into at least one machine.', actions: [{ label: 'Focus', onClick: () => undefined }] }, { id: 'b', level: 'warn', message: 'This rate limit is not plugged into a load balancer and will not be saved.' }]} /> },
  { component: 'IssueList', variant: 'default', state: 'warnings', render: () => <IssueList label="Checks" issues={[{ id: 'c', level: 'warn', message: 'llama-3.3-70b changed on the server while you were editing.', actions: [{ label: 'Take server', onClick: () => undefined }] }]} /> },
  { component: 'IssueList', variant: 'default', state: 'empty', render: () => <IssueList label="Checks" issues={[]} empty="Nothing to fix." /> },
  // scaffold:IssueList:end
  // scaffold:HostTable:start
  { component: 'HostTable', variant: 'default', state: 'default', interactive: true, render: () => <HostTable rows={HOSTS} pinned={null} fallback={false} onPin={() => {}} onFallback={() => {}} /> },
  { component: 'HostTable', variant: 'default', state: 'pinned', interactive: true, render: () => <HostTable rows={HOSTS} pinned="google-vertex" fallback={true} onPin={() => {}} onFallback={() => {}} /> },
  { component: 'HostTable', variant: 'default', state: 'loading', interactive: true, render: () => <HostTable rows={[]} pinned={null} fallback={false} loading onPin={() => {}} onFallback={() => {}} /> },
  { component: 'HostTable', variant: 'default', state: 'error', interactive: true, render: () => <HostTable rows={HOSTS} pinned={null} fallback={false} error="openrouter lists no backend 'nope' for anthropic/claude-sonnet-4.5; pick one from its backend list" onPin={() => {}} onFallback={() => {}} /> },
  { component: 'HostTable', variant: 'default', state: 'empty', interactive: true, render: () => <HostTable rows={[]} pinned={null} fallback={false} onPin={() => {}} onFallback={() => {}} /> },
  // scaffold:HostTable:end
]
