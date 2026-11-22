import type { Activity, ProviderKindSpec } from '../../api/types'
import { Verbatim } from '../../components/Verbatim'
import { useState } from 'react'
import type { DownloadActivity } from '../../api/types'
import { useBackend } from '../../state/backend'
import { useActivity } from '../../state/resources'
import { plainClick, useRouter } from '../../state/router'
import { PullCard } from './PullCard'
import { gib } from './verdictView'

export interface DownloadsPanelProps {
  providerKinds: ProviderKindSpec[] | null | undefined
}

/** What the badge counts and the panel lists, from one definition, so the
 *  two cannot disagree: a launch the server has a phase for, and a pull that
 *  has not finished. */
function active(activity: Activity | null) {
  return {
    launches: (activity?.launches ?? []).filter((l) => l.phase != null),
    pulls: (activity?.downloads ?? []).filter((d) => !d.done),
  }
}

/** The toolbar's count. A second `useActivity` poll -- `useResource` does not
 *  deduplicate -- which is the price of the badge being right while the panel
 *  is closed. */
export function useDownloadsBadge(): number {
  const { launches, pulls } = active(useActivity().data)
  return launches.length + pulls.length
}

/** What is downloading or loading right now, as the coordinator reports it.
 *  A Hugging Face prefetch (started by "Download only" on the Serve tab) can
 *  be paused, resumed and cancelled here; launches and provider pulls are
 *  read-only. The form underneath starts a pull onto a provider. */
export function DownloadsPanel({ providerKinds }: DownloadsPanelProps) {
  const activity = useActivity()
  const { launches, pulls } = active(activity.data)

  return (
    <section className="card2 mdl-downloads" aria-label="Downloads">
      <div className="sub">Downloads</div>

      {activity.error ? <Verbatim text={activity.error.message} size="unit" /> : null}

      <AwaitingCopies n={activity.data?.replication?.awaiting ?? 0} />

      {launches.map((l) => (
        <div key={l.deployment_id} className="mdl-dl-row">
          <div className="mdl-dl-line">
            <span className="mono mdl-ellipsis">{l.model_id ?? l.served_name}</span>
            {l.fraction != null ? (
              <span className="unit mdl-num">
                {Math.round(l.fraction * 100)}%
                {l.eta_s != null ? ` · eta ${Math.round(l.eta_s)} s` : ''}
              </span>
            ) : null}
          </div>
          <div className={l.fatal ? 'mdl-dl-status mdl-fault' : 'mdl-dl-status'}>
            <span className="unit">{l.phase}</span>
            {l.status ? <Verbatim text={l.status} size="unit" /> : null}
          </div>
          {/* A bar only where something counted: the shard loader and the
              downloader report a fraction, the image pull and graph capture
              do not, and an indeterminate bar would claim progress nobody
              measured. */}
          {l.fraction != null ? (
            <div className="mdl-bar" aria-hidden="true">
              <div
                className={l.fatal ? 'mdl-bar-fill mdl-bar-fault' : 'mdl-bar-fill'}
                style={{ width: `${Math.min(1, Math.max(0, l.fraction)) * 100}%` }}
              />
            </div>
          ) : null}
        </div>
      ))}

      {pulls.map((d) => (
        <div key={d.pull_id} className="mdl-dl-row">
          <div className="mdl-dl-line">
            <span className="mono mdl-ellipsis">{d.model}</span>
            <span className="unit">{d.provider}</span>
          </div>
          {d.kind === 'hub' && d.download_id ? <HubControls d={d} /> : null}
          {d.kind === 'transfer' && d.transfer_id ? <TransferControls d={d} /> : null}
          {d.total != null ? (
            <div className="mdl-bar" aria-hidden="true">
              <div
                className="mdl-bar-fill"
                style={{ width: `${Math.min(1, d.completed / d.total) * 100}%` }}
              />
            </div>
          ) : null}
          {d.status ? <span className="unit">{d.status}</span> : null}
          {d.error ? (
            <div className="mdl-fault">
              <Verbatim text={d.error} size="unit" />
            </div>
          ) : null}
        </div>
      ))}

      {!launches.length && !pulls.length && !activity.error ? (
        <p className="unit">Nothing is downloading. Weights download when a model is served.</p>
      ) : null}

      <div className="sub mdl-dl-pull">Pull onto a provider</div>
      <PullCard providerKinds={providerKinds} />
    </section>
  )
}

/** Background copies the coordinator proposed and is waiting on a person
 *  for. Nothing copies until they approve, and the approval lives with the
 *  disk it spends -- /settings/storage -- so this links straight there. */
function AwaitingCopies({ n }: { n: number }) {
  const { linkTo, navigate } = useRouter()
  if (n <= 0) return null
  const to = { dest: 'settings' as const, section: 'storage' as const }
  return (
    <p className="unit">
      {n} background {n === 1 ? 'copy' : 'copies'} waiting for your OK in{' '}
      <a
        href={linkTo(to)}
        onClick={(e) => {
          if (!plainClick(e)) return
          e.preventDefault()
          navigate(to)
        }}
      >
        Settings → Storage → Background copies
      </a>
    </p>
  )
}

/** " · 42.1 MB/s · 3m 20s left" from the server's own rate and ETA; empty
 *  while either is unknown, never a guess. */
function rateLine(d: DownloadActivity): string {
  if (d.rate_bps == null) return ''
  const rate = `${(d.rate_bps / 1e6).toFixed(1)} MB/s`
  if (d.eta_s == null) return ` · ${rate}`
  const t = Math.max(0, Math.round(d.eta_s))
  const eta = t >= 60 ? `${Math.floor(t / 60)}m ${t % 60}s` : `${t}s`
  return ` · ${rate} · ${eta} left`
}

/** A coordinator-side weight prefetch can be paused, resumed and cancelled;
 *  a provider pull cannot, and gets none of this. */
function HubControls({ d }: { d: DownloadActivity }) {
  const { backend, invalidate } = useBackend()
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const id = d.download_id!
  const act = (fn: () => Promise<unknown>) => {
    setBusy(true)
    setErr(null)
    fn()
      .then(() => invalidate())
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(false))
  }
  return (
    <>
      <div className="mdl-dl-line">
        <span className="unit mdl-num">
          {gib(d.completed)} / {gib(d.total)}
          {d.paused ? ' · paused' : rateLine(d)}
        </span>
        <span className="mdl-dl-actions">
          <button
            type="button"
            className="ghost"
            disabled={busy}
            onClick={() => act(() => (d.paused ? backend.resumeDownload(id) : backend.pauseDownload(id)))}
          >
            {d.paused ? 'Resume' : 'Pause'}
          </button>
          <button
            type="button"
            className="ghost"
            disabled={busy}
            onClick={() => {
              if (window.confirm('Cancel this download? Files already on disk stay until you delete them.')) {
                act(() => backend.cancelDownload(id))
              }
            }}
          >
            Cancel
          </button>
        </span>
      </div>
      {err ? <Verbatim text={err} size="unit" /> : null}
    </>
  )
}

/** A copy from the coordinator's cache to another node can be cancelled, not
 *  paused: the receiving node discards a session that goes quiet. */
function TransferControls({ d }: { d: DownloadActivity }) {
  const { backend, invalidate } = useBackend()
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const id = d.transfer_id!
  const cancel = () => {
    const sure = window.confirm(
      'Cancel this copy? Files that already arrived and verified stay on the node, so a later copy resumes; unfinished ones are removed.',
    )
    if (!sure) return
    setBusy(true)
    setErr(null)
    backend
      .cancelTransfer(id)
      .then(() => invalidate())
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(false))
  }
  return (
    <>
      <div className="mdl-dl-line">
        <span className="unit mdl-num">
          {gib(d.completed)} / {gib(d.total)}
          {rateLine(d)}
        </span>
        {/* Only a running copy can be stopped: a verifying one is already
            committing on the node (the server refuses), and a finished,
            failed or cancelling one has nothing left to cancel. */}
        {d.status === 'running' ? (
          <span className="mdl-dl-actions">
            <button type="button" className="ghost" disabled={busy} onClick={cancel}>
              Cancel
            </button>
          </span>
        ) : null}
      </div>
      {err ? <Verbatim text={err} size="unit" /> : null}
    </>
  )
}
