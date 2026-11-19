import { useState, type FormEvent } from 'react'
import type { AuditAction, AuditEntry, AuditPage } from '../../api/types'
import { Icon } from '../../components/Icon'
import { Verbatim } from '../../components/Verbatim'
import { useKeyedResource } from '../../state/backend'
import { ActorPanel } from './ActorPanel'
import { AuditTable, Pager } from './AuditTable'
import {
  AUDIT_ACTIONS,
  AUDIT_PAGE_SIZE,
  AUDIT_RANGES,
  auditQuery,
  pageCount,
  type AuditFilter,
  type AuditRange,
} from './diff'

const RANGE_LABEL: Record<AuditRange, string> = {
  '24h': '24 hours',
  '7d': '7 days',
  '30d': '30 days',
  all: 'All time',
}

/** The Settings → Audit log sub-tab: every admin action the coordinator recorded,
 *  newest first, with what each one changed.
 *
 *  The server writes the whole object on each side of an edit; the table shows
 *  only the leaves that moved (`diff.ts`). Nothing here can render a key
 *  secret: the audit rows carry `authz.key_dict`, which has no secret or hash
 *  field, and every response is scrubbed on the way in regardless.
 *
 *  Text filters apply on Enter or blur rather than per keystroke, so typing
 *  an actor's name is one query, not one per letter. Action and window apply
 *  at once. Any filter change goes back to page 1.
 *
 *  Every row is one line. Double-clicking one opens its actor as a second
 *  sub-tab (`ActorPanel`): that event in full, the addresses the actor acted
 *  from, the keys it created and everything else it ever did -- what you
 *  re-check after a suspected intrusion. One actor tab at a time; the log
 *  stays mounted underneath so its filters and page survive. */
export function AuditTab() {
  const [view, setView] = useState<'log' | 'actor'>('log')
  const [open, setOpen] = useState<{ actor: string; entry: AuditEntry } | null>(null)
  const [filter, setFilter] = useState<AuditFilter>({
    actor: '',
    action: '',
    target: '',
    range: '7d',
    page: 1,
  })
  const [actorDraft, setActorDraft] = useState('')
  const [targetDraft, setTargetDraft] = useState('')

  const key = JSON.stringify(filter)
  const page = useKeyedResource<AuditPage>(
    key,
    (b) => b.audit(auditQuery(filter, Date.now() / 1000)),
    15000,
  )

  const apply = (patch: Partial<AuditFilter>) => setFilter((f) => ({ ...f, ...patch, page: patch.page ?? 1 }))
  const commitText = (e?: FormEvent) => {
    e?.preventDefault()
    if (actorDraft.trim() === filter.actor && targetDraft.trim() === filter.target) return
    apply({ actor: actorDraft.trim(), target: targetDraft.trim() })
  }

  const data = page.data
  const pages = data ? pageCount(data.total, data.page_size || AUDIT_PAGE_SIZE) : 1

  return (
    <div className="card2">
      <div className="subs" role="tablist" aria-label="Audit views">
        <button
          id="au-tab-log"
          type="button"
          role="tab"
          aria-selected={view === 'log'}
          aria-controls="au-log"
          onClick={() => setView('log')}
        >
          Log
        </button>
        {open ? (
          <>
            <button
              id="au-tab-actor"
              type="button"
              role="tab"
              aria-selected={view === 'actor'}
              aria-controls="au-actor"
              onClick={() => setView('actor')}
            >
              Actor · <span className="mono">{open.actor}</span>
            </button>
            <button
              type="button"
              aria-label="Close actor view"
              onClick={() => {
                setOpen(null)
                setView('log')
              }}
            >
              <Icon name="close" size={12} decorative />
            </button>
          </>
        ) : null}
      </div>

      <div id="au-log" role="tabpanel" aria-labelledby="au-tab-log" hidden={view !== 'log'}>
        {/* No title or blurb: the Settings sub-tab already says "Audit log", and
            the placeholders say what each box filters. One row -- filters on the
            left, the window in the card's top-right corner -- all controls
            centred on one line (check/alignment.mjs). On a phone it wraps. */}
        <form
          onSubmit={commitText}
          style={{ display: 'flex', gap: 'var(--space-8)', alignItems: 'center', flexWrap: 'wrap' }}
        >
          <input
            aria-label="Actor"
            value={actorDraft}
            onChange={(e) => setActorDraft(e.target.value)}
            onBlur={() => commitText()}
            placeholder="any admin"
            autoComplete="off"
          />
          <select
            aria-label="Action"
            className="mono"
            value={filter.action}
            onChange={(e) => apply({ action: e.target.value as AuditAction | '' })}
          >
            <option value="">any action</option>
            {AUDIT_ACTIONS.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
          <input
            aria-label="Target"
            className="mono"
            value={targetDraft}
            onChange={(e) => setTargetDraft(e.target.value)}
            onBlur={() => commitText()}
            placeholder="key or model id"
            autoComplete="off"
          />
          {/* Enter in either text field submits; the button is for a pointer. */}
          <button type="submit">Filter</button>
          {/* `.chips`' bottom margin is for sitting above a grid; here it would
              hang the buttons above the inputs. */}
          <span
            className="chips"
            role="group"
            aria-label="Audit window"
            style={{ margin: 0, marginLeft: 'auto' }}
          >
            {AUDIT_RANGES.map((r) => (
              <button
                type="button"
                key={r}
                aria-pressed={filter.range === r}
                onClick={() => apply({ range: r })}
              >
                {RANGE_LABEL[r]}
              </button>
            ))}
          </span>
        </form>

        <div style={{ marginTop: 'var(--space-12)' }}>
          {page.error ? <Verbatim text={page.error.message} size="unit" /> : null}
          {!data ? (
            page.error ? null : (
              <div className="unit" aria-busy="true" aria-live="polite">
                Reading the audit log…
              </div>
            )
          ) : data.entries.length === 0 ? (
            <div className="unit">
              No admin action matches these filters
              {filter.range === 'all' ? '.' : ` in the last ${RANGE_LABEL[filter.range]}.`}
            </div>
          ) : (
            <AuditTable
              entries={data.entries}
              onOpen={(e) => {
                setOpen({ actor: e.actor, entry: e })
                setView('actor')
              }}
            />
          )}
        </div>

        {data && data.total > 0 ? (
          <Pager
            page={data.page}
            pages={pages}
            total={data.total}
            noun={['entry', 'entries']}
            onPage={(p) => apply({ page: p })}
          />
        ) : null}
      </div>

      {open && view === 'actor' ? (
        <div id="au-actor" role="tabpanel" aria-labelledby="au-tab-actor">
          <ActorPanel
            key={open.actor}
            actor={open.actor}
            entry={open.entry}
            onSelect={(entry) => setOpen({ actor: open.actor, entry })}
          />
        </div>
      ) : null}
    </div>
  )
}

