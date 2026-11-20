import { useEffect, useState } from 'react'
import type { NodeProps } from '@xyflow/react'
import { BlockFrame } from '../../../components/BlockFrame'
import { SegmentBar } from '../../../components/Bars'
import { Icon } from '../../../components/Icon'
import { Lamp } from '../../../components/Lamp'
import { fmt, gbytes } from '../../../format'
import { useMetrics } from '../../../state/metrics'
import { useActivity } from '../../../state/resources'
import type { DeploymentState, ProviderBackendOption } from '../../../api/types'
import { HostTable } from '../../../components/HostTable'
import { useBackend } from '../../../state/backend'
import type { ProviderPin } from '../doc'
import { clock, isArriving, launchView } from '../loading'
import { markProviderLogoMissing, providerLogoMissing, providerLogoUrl, providerMonogram } from '../providerLogo'
import { rowIn, rowOut } from '../sockets'
import { Socket, SocketLabel, socket, toneOf, useEditor } from './editor'
import type { BlockNode } from './Terminals'

/** A machine: one row per deployment running on it, the memory it has, and
 *  a `deploy here` socket for a draft model. Its link sockets top and bottom
 *  carry the measured line to the next machine. */
export function MachineBlock({ data, selected }: NodeProps<BlockNode<'machine'>>) {
  const editor = useEditor()
  const { frame, stale } = useMetrics()
  const activity = useActivity()
  const b = data.block
  const deploy = socket(b.sockets, 'deploy')
  const planned = Object.entries(editor.plans).reduce((sum, [id, p]) => {
    const total = p.result?.fit?.breakdown?.total
    const model = id.includes('#') ? null : p.result?.placement?.node_ids ?? null
    return model?.includes(b.node_id) && total ? sum + total / model.length : sum
  }, 0)
  // A launching row says which of the four slow things it is on, never a rate.
  const arriving = (r: { deployment_id: string; model_id: string; state: DeploymentState }) => {
    const launch = activity.data?.launches.find((l) => l.deployment_id === r.deployment_id) ?? null
    const view = launchView(r, launch, activity.data?.downloads ?? [], Date.now() / 1000)
    const step = view.steps.find((s) => s.state === 'now')
    return `${step?.label ?? 'Launching'}${view.elapsed != null ? ` · ${clock(view.elapsed)}` : ''}`
  }
  return (
    <div className="wnode wmachine">
      <Socket spec={socket(b.sockets, 'link-top')} />
      <BlockFrame
        title={b.title}
        icon="server"
        signal={{ value: b.signal, label: `${b.title} is ${b.signal === 'live' ? 'healthy' : b.signal === 'warn' ? 'degraded' : 'unreachable'}` }}
        selected={selected}
        tone={toneOf(editor.issuesFor(b.id))}
        meta={<span className="unit">{b.rows.length === 1 ? '1 model' : `${b.rows.length} models`}</span>}
        footer={
          <div className="wport">
            <Socket spec={deploy} />
            <SocketLabel spec={deploy} />
          </div>
        }
      >
        {b.memory.total != null ? (
          <div title="memory: used, planned, total">
            <SegmentBar
              segments={[
                { key: 'used', label: 'used', bytes: b.memory.used ?? 0 },
                { key: 'planned', label: 'planned', bytes: planned },
              ]}
              usable={b.memory.total}
              height={8}
            />
            <span className="unit mono">
              {gbytes(b.memory.used ?? 0, 1)} used{planned > 0 ? ` · ${gbytes(planned, 1)} planned` : ''} of {gbytes(b.memory.total, 1)} GB
            </span>
          </div>
        ) : null}
        {b.rows.length === 0 ? <span className="unit">Nothing running here.</span> : null}
        {b.rows.map((r) => {
          const tps = frame?.deployments?.find((d) => d.deployment_id === r.deployment_id)?.tokens_per_sec ?? null
          const inSpec = socket(b.sockets, rowIn(r.id))
          const outSpec = socket(b.sockets, rowOut(r.id))
          return (
            <div key={r.id} className="wrow" data-rank={r.rank ? 'true' : undefined}>
              <Socket spec={inSpec} />
              <Lamp signal={r.signal} label={`${r.title}: ${r.signal === 'live' ? 'admitting' : r.signal === 'warn' ? 'not admitting' : 'failed'}`} />
              <span className="wrow-main">
                <button
                  type="button"
                  className="wrow-title nodrag"
                  title={`${r.title} serves ${r.served}. Double-click for the deployment.`}
                  onDoubleClick={() => editor.openDeployment(r.served)}
                >
                  {r.title}
                </button>
                <span className="unit">
                  {r.served}
                  {r.rank ? ` · rank ${r.rank.k} of ${r.rank.n}` : ''}
                </span>
                <span className="wchips">
                  {r.span ? <span className="pill">{r.span}</span> : null}
                  {r.mtp ? <span className="pill">{r.mtp}</span> : null}
                  {r.flags.map((f) => (
                    <span
                      key={f.text}
                      className={`pill mono wflag${f.removed ? ' wflag-removed' : ''}`}
                      title={f.removed ? `${f.text}: dropped on Save, which relaunches the model` : `launch flag ${f.text}`}
                    >
                      {f.text}
                      {f.removed || r.rank || r.pending === 'stop' || r.state === 'stopping' ? null : (
                        <button
                          type="button"
                          className="ghost nodrag"
                          aria-label={`Remove ${f.flag} (relaunches the model)`}
                          title={`Remove ${f.flag} (relaunches the model)`}
                          onClick={() => editor.dropFlag(r.deployment_id, r.extra_args, f.flag)}
                        >
                          ×
                        </button>
                      )}
                    </span>
                  ))}
                  {r.custom_command ? <span className="pill">custom command</span> : null}
                  {r.pending === 'relaunch' ? <span className="pill wpending">relaunch pending</span> : null}
                  {r.pending === 'stop' ? <span className="pill wpending">stops on Save</span> : null}
                </span>
              </span>
              {r.rank ? null : isArriving(r.state) ? (
                <span className="unit mono" title="launch step and time since it started">
                  {arriving(r)}
                </span>
              ) : (
                <span className="unit mono" style={{ color: stale ? 'var(--ink-muted)' : undefined }}>
                  {tps != null ? `${fmt(tps, 1)} tok/s` : '—'}
                </span>
              )}
              <Socket spec={outSpec} />
            </div>
          )
        })}
      </BlockFrame>
      <Socket spec={socket(b.sockets, 'link-bottom')} />
    </div>
  )
}

/** A provider: one row per enabled model, each fed from a balancer's cloud
 *  port. Cutting a row's wire, or removing the row, switches the model off. */
export function ProviderBlock({ data, selected }: NodeProps<BlockNode<'provider'>>) {
  const editor = useEditor()
  const { frame, stale } = useMetrics()
  const b = data.block
  const [adding, setAdding] = useState(false)
  const [filter, setFilter] = useState('')
  // One footer popover at a time: the catalogue, or one row's host table.
  const [hostFor, setHostFor] = useState<string | null>(null)
  const hostRow = hostFor ? (b.rows.find((r) => r.upstream_id === hostFor) ?? null) : null
  const missing = providerLogoMissing(b.provider_id)
  // The whole catalogue, never a first page: OpenRouter publishes several
  // hundred models and the list scrolls (`.waddmodel ul`), so a cap only hid
  // the ones past it until somebody guessed a filter.
  const catalogue = adding ? editor.catalogueFor(b.provider_id) : []
  const needle = filter.trim().toLowerCase()
  const offer = needle ? catalogue.filter((m) => m.upstream_id.toLowerCase().includes(needle)) : catalogue
  return (
    <div className="wnode wprovider">
      <BlockFrame
        title={b.title}
        sub={`priority ${b.priority}${b.pinned > 0 ? ` · ${b.pinned} pinned` : ''}`}
        selected={selected}
        tone={toneOf(editor.issuesFor(b.id))}
        meta={
          <span className="wprovider-meta">
            {missing ? (
              <span className="wmonogram" aria-hidden="true">
                {providerMonogram(b.provider_id, b.title)}
              </span>
            ) : (
              <img
                className="wlogo"
                src={providerLogoUrl(b.provider_id)}
                alt={`${b.title} logo`}
                onError={() => markProviderLogoMissing(b.provider_id)}
              />
            )}
            <label className="unit nodrag">
              <input
                type="checkbox"
                checked={b.enabled}
                onChange={(e) => editor.setProviderEnabled(b.provider_id, e.target.checked)}
              />{' '}
              enabled
            </label>
          </span>
        }
        footer={
          hostRow ? (
            <HostPicker
              providerId={b.provider_id}
              upstream={hostRow.upstream_id}
              pin={hostRow.pin}
              onClose={() => setHostFor(null)}
            />
          ) : adding ? (
            <div className="waddmodel nodrag nowheel">
              <input
                type="search"
                placeholder={`Filter ${catalogue.length} models`}
                aria-label={`Filter ${b.title}'s catalogue`}
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
              />
              <ul>
                {offer.map((m) => (
                  <li key={m.upstream_id}>
                    <button
                      type="button"
                      className="ghost"
                      onClick={() => {
                        editor.addProviderModel(b.provider_id, m.upstream_id, m.served_name)
                        setAdding(false)
                        setFilter('')
                      }}
                    >
                      {m.upstream_id}
                    </button>
                  </li>
                ))}
                {offer.length === 0 ? <li className="unit">Nothing else in the catalogue matches.</li> : null}
              </ul>
              <button type="button" className="ghost" onClick={() => setAdding(false)}>
                Close
              </button>
            </div>
          ) : (
            <button
              type="button"
              className="ghost nodrag"
              onClick={() => {
                setHostFor(null)
                setAdding(true)
              }}
            >
              <Icon name="plus" size={12} decorative /> Add model
            </button>
          )
        }
      >
        {b.rows.length === 0 ? <span className="unit">No models switched on.</span> : null}
        {b.rows.map((r) => {
          const tps = frame?.remotes?.find((x) => x.target_id === r.target_id)?.tokens_per_sec ?? null
          return (
            <div key={r.id} className="wrow">
              <Socket spec={socket(b.sockets, rowIn(r.id))} />
              <span className="wrow-main">
                <span className="wrow-title" title={r.title}>
                  {r.title}
                </span>
                <span className="unit">{r.sub}</span>
              </span>
              <span className="unit mono" style={{ color: stale ? 'var(--ink-muted)' : undefined }}>
                {tps != null ? `${fmt(tps, 1)} tok/s` : ''}
              </span>
              {b.provider_kind === 'openrouter' ? (
                <button
                  type="button"
                  className="ghost nodrag"
                  aria-label={`Choose host for ${r.upstream_id}`}
                  title={`Choose host for ${r.upstream_id}`}
                  aria-pressed={hostFor === r.upstream_id}
                  onClick={() => {
                    setAdding(false)
                    setHostFor(hostFor === r.upstream_id ? null : r.upstream_id)
                  }}
                >
                  <Icon name="route" size={12} decorative />
                </button>
              ) : null}
              <button
                type="button"
                className="ghost nodrag"
                aria-label={`Switch ${r.title} off`}
                title={`Switch ${r.title} off`}
                onClick={() => editor.removeProviderModel(b.provider_id, r.upstream_id)}
              >
                <Icon name="x-circle" size={12} decorative />
              </button>
              <Socket spec={socket(b.sockets, rowOut(r.id))} />
            </div>
          )
        })}
      </BlockFrame>
    </div>
  )
}

/** The backend hosts OpenRouter lists for one model, fetched when opened,
 *  with the draft's pin checked. Picking one edits the draft only; the dry
 *  run checks the tag against the hosts the server itself lists. */
function HostPicker({
  providerId,
  upstream,
  pin,
  onClose,
}: {
  providerId: string
  upstream: string
  pin: ProviderPin | null
  onClose: () => void
}) {
  const editor = useEditor()
  const { backend } = useBackend()
  const [rows, setRows] = useState<ProviderBackendOption[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    setRows(null)
    setError(null)
    backend
      .providerBackends(providerId, upstream)
      .then((r) => {
        if (live) setRows(r)
      })
      .catch((e: unknown) => {
        if (live) setError(e instanceof Error ? e.message : String(e))
      })
    return () => {
      live = false
    }
  }, [backend, providerId, upstream])
  return (
    <div className="waddmodel nodrag nowheel" role="group" aria-label={`Hosts for ${upstream}`}>
      <span className="unit mono">{upstream}</span>
      <HostTable
        className="whosts"
        rows={rows ?? []}
        pinned={pin?.tag ?? null}
        fallback={pin?.fallback ?? false}
        loading={rows === null && error === null}
        error={error}
        onPin={(tag) => editor.setProviderPin(providerId, upstream, tag ? { tag, fallback: pin?.fallback ?? false } : null)}
        onFallback={(v) => {
          if (pin) editor.setProviderPin(providerId, upstream, { tag: pin.tag, fallback: v })
        }}
      />
      <button type="button" className="ghost" onClick={onClose}>
        Close
      </button>
    </div>
  )
}
