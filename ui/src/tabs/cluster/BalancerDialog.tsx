// One served name's load balancer, opened by double-clicking its split or
// merge: the routing policy, the rate limit plugged into it, what it routes
// to right now, and removing it. Every control edits the draft; nothing here
// saves -- the edits show in Pending changes like any wire.

import { useEffect, useId, useRef } from 'react'
import { createPortal } from 'react-dom'
import type { RoutingPolicy } from '../../api/types'
import { containTab } from '../../components/focusTrap'
import { Lamp } from '../../components/Lamp'
import { Verbatim } from '../../components/Verbatim'
import { fmt, pct } from '../../format'
import { ROUTING_POLICIES } from '../keys/rows'
import {
  addLimit,
  BALANCER_SENTENCES,
  balancerTargets,
  connect,
  disconnect,
  effectivePolicy,
  limitsSupported,
  removeLimit,
  removalOf,
  removalSentence,
  removeServedName,
  servedNames,
  setLimit,
  rebalance,
  setRoutePolicy,
  setRouteShares,
  targetSplit,
  type Doc,
  type ServerSnapshot,
} from './doc'
import { LB_POLICY, wireId } from './sockets'

function whole(value: string): number | undefined {
  return value.trim() === '' ? undefined : Number(value)
}

export function BalancerDialog({
  served,
  doc,
  snapshot,
  newLimitId,
  onEdit,
  onClose,
}: {
  served: string
  doc: Doc
  snapshot: ServerSnapshot
  /** A fresh block id for the limit the first rpm/tpm creates. */
  newLimitId: () => string
  onEdit: (fn: (d: Doc) => Doc) => void
  onClose: () => void
}) {
  const cardRef = useRef<HTMLDivElement>(null)
  const titleId = useId()
  const closeRef = useRef(onClose)
  closeRef.current = onClose

  useEffect(() => {
    const card = cardRef.current
    card?.focus()
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        closeRef.current()
        return
      }
      containTab(e, card)
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [])

  const routing = snapshot.routing.find((r) => r.served_name === served) ?? null
  const draft = servedNames(doc, snapshot).draft.has(served)
  const { deployments, cloud } = removalOf(doc, snapshot, served)
  const policy = doc.routes[served]?.policy ?? 'auto'
  const resolved = effectivePolicy(doc, snapshot, served)
  const limit = doc.limits.find((l) => l.plugged.includes(served)) ?? null
  const shared = limit ? limit.plugged.filter((n) => n !== served) : []
  const hasLimit = limit != null && (limit.rpm !== undefined || limit.tpm !== undefined)
  const supported = limitsSupported(snapshot)
  const targets = balancerTargets(doc, snapshot, served)
  const { manual, split } = targetSplit(doc, served, targets)
  const setShare = (id: string, value: number) => onEdit((d) => setRouteShares(d, served, rebalance(split, id, value)))

  const setValue = (key: 'rpm' | 'tpm', value: number | undefined) =>
    onEdit((d) => {
      const l = d.limits.find((x) => x.plugged.includes(served))
      if (!l) {
        if (value === undefined) return d
        const id = newLimitId()
        return connect(addLimit(d, id, { [key]: value }), { block: id, socket: 'out' }, { block: `split:${served}`, socket: 'limit' })
      }
      const other = key === 'rpm' ? l.tpm : l.rpm
      if (value === undefined && other === undefined) {
        // Both cleared: unplug it, and drop it when it capped nothing else.
        const next = disconnect(d, wireId({ block: l.id, socket: 'out' }, { block: `split:${served}`, socket: 'limit' }))
        return l.plugged.length === 1 ? removeLimit(next, l.id) : next
      }
      return setLimit(d, l.id, { [key]: value })
    })

  const remove = () => {
    if (!window.confirm(removalSentence(doc, snapshot, served))) return
    onEdit((d) => removeServedName(d, snapshot, served))
    onClose()
  }

  return createPortal(
    <div className="sheet on" role="presentation">
      <div ref={cardRef} className="card full wbalancer" role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1}>
        <h2 id={titleId}>{served} load balancer</h2>
        <p className="unit">
          {deployments.length} local · {cloud.length} cloud
        </p>

        <h3>Policy</h3>
        <label className="wbalancer-field">
          <select
            aria-label="Routing policy"
            value={policy}
            disabled={draft}
            onChange={(e) => onEdit((d) => setRoutePolicy(d, served, e.target.value as RoutingPolicy | 'auto'))}
          >
            <option value="auto">auto — what the router picks ({routing?.policy ?? LB_POLICY})</option>
            {ROUTING_POLICIES.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </label>
        {draft ? <p className="unit">{BALANCER_SENTENCES.draft}</p> : null}
        {policy === 'auto' && routing?.auto_reason ? <Verbatim text={routing.auto_reason} size="unit" /> : null}
        {resolved === LB_POLICY ? <p className="unit">{BALANCER_SENTENCES.triangleRule}</p> : null}

        {supported ? (
          <>
            <h3>Limits</h3>
            <div className="wfields">
              <label className="unit">
                rpm
                <input
                  className="mono"
                  type="number"
                  min={1}
                  step={1}
                  disabled={draft}
                  value={limit?.rpm ?? ''}
                  onChange={(e) => setValue('rpm', whole(e.target.value))}
                />
              </label>
              <label className="unit">
                tpm
                <input
                  className="mono"
                  type="number"
                  min={1}
                  step={1}
                  disabled={draft}
                  value={limit?.tpm ?? ''}
                  onChange={(e) => setValue('tpm', whole(e.target.value))}
                />
              </label>
            </div>
            {shared.length > 0 ? <p className="unit">This limit also caps {shared.join(', ')}.</p> : null}
            {policy === 'auto' && hasLimit ? <p className="unit wbalancer-note">{BALANCER_SENTENCES.limitPins}</p> : null}
          </>
        ) : null}

        <h3>Targets</h3>
        <p className="unit">
          {manual ? `${BALANCER_SENTENCES.manualSplit} ` : BALANCER_SENTENCES.measuredSplit}
          {manual && !draft ? (
            <button type="button" className="ghost" onClick={() => onEdit((d) => setRouteShares(d, served, null))}>
              Use the measured split
            </button>
          ) : null}
        </p>
        {targets.length > 0 ? (
          <table className="wbalancer-targets">
            <thead>
              <tr>
                <th>target</th>
                <th>kind</th>
                <th>admitting</th>
                <th>share</th>
                <th>outstanding</th>
                <th>circuit</th>
                <th>blocks</th>
              </tr>
            </thead>
            <tbody>
              {targets.map(({ id, label, kind, live: t, pending }) => (
                <tr key={id}>
                  <td className="mono">
                    {label}
                    {pending ? <span className="pill wpending">{pending === 'add' ? 'added on Save' : 'removed on Save'}</span> : null}
                  </td>
                  <td>{kind}</td>
                  <td>
                    {t ? (
                      <Lamp
                        signal={t.healthy && t.admitting ? 'live' : t.healthy ? 'warn' : 'fault'}
                        label={`${label}: ${t.healthy && t.admitting ? 'admitting' : 'not admitting'}`}
                      />
                    ) : (
                      '—'
                    )}
                  </td>
                  <td className="wshare">
                    {id in split ? (
                      <>
                        <input
                          type="range"
                          min={0}
                          max={100}
                          step={1}
                          value={split[id]}
                          disabled={draft || Object.keys(split).length < 2}
                          aria-label={`Share of ${served} sent to ${label}`}
                          onChange={(e) => setShare(id, Number(e.target.value))}
                        />
                        <span className="mono">{split[id]}%</span>
                      </>
                    ) : (
                      <span className="mono">{t ? `${pct(t.weight * 100)}%` : '—'}</span>
                    )}
                  </td>
                  <td className="mono">{t ? fmt(t.outstanding) : '—'}</td>
                  <td>{t ? (t.circuit ?? 'closed') : '—'}</td>
                  <td>{t?.admission_blocks?.length ? t.admission_blocks.join(', ') : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="unit">The router has no targets for {served} yet.</p>
        )}

        <div className="wbalancer-actions">
          {draft ? null : (
            <button type="button" onClick={remove}>
              Remove this load balancer
            </button>
          )}
          <span className="wtoolbar-gap" />
          <button type="button" className="btn" onClick={onClose}>
            Done
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
