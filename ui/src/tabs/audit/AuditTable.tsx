import type { CSSProperties, KeyboardEvent, MouseEvent } from 'react'
import type { AuditEntry } from '../../api/types'
import { Icon } from '../../components/Icon'
import { plainClick, useRouter } from '../../state/router'
import { ABSENT, changeText, diffEntry, targetDest } from './diff'

// The pieces the log and the actor view share: the one-line table, the
// target link, the pager. Kept out of AuditTab.tsx so ActorPanel.tsx does not
// import the screen that imports it.

/** A cell value that would widen its row past one line is cut with an
 *  ellipsis; the whole value is in its `title`. */
function clip(maxWidth: string): CSSProperties {
  return {
    display: 'inline-block',
    maxWidth,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    verticalAlign: 'bottom',
  }
}

const NOWRAP: CSSProperties = { whiteSpace: 'nowrap' }

/** A row that opens something shows a pointer. The row whose event the
 *  actor view is showing is lifted to the panel (the card is recessed) and
 *  marked down its left edge in the selection colour. */
export function rowStyle(clickable: boolean, selected: boolean): CSSProperties | undefined {
  if (!clickable && !selected) return undefined
  return {
    ...(clickable ? { cursor: 'pointer' } : null),
    ...(selected ? { background: 'var(--panel)', boxShadow: 'inset 2px 0 0 var(--select-on-panel)' } : null),
  }
}

/** The audit rows, one line each. A row's changes are summarised to the first
 *  one plus a count; the full list is the cell's tooltip and the actor view's
 *  "This event". Double-click or Enter on a row opens that row's actor --
 *  unless the row has no actor, which would otherwise query every actor. */
export function AuditTable({
  entries,
  onOpen,
  showActor = true,
  selectedId,
}: {
  entries: AuditEntry[]
  onOpen?: (e: AuditEntry) => void
  showActor?: boolean
  selectedId?: number
}) {
  const open = (e: AuditEntry) => {
    if (!e.actor || !onOpen) return
    onOpen(e)
  }
  return (
    <div style={{ overflowX: 'auto' }}>
      <table>
        <thead>
          <tr>
            <th scope="col">When</th>
            {showActor ? <th scope="col">Actor</th> : null}
            <th scope="col">Action</th>
            <th scope="col">Target</th>
            <th scope="col">IP</th>
            <th scope="col">Changes</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((e) => {
            const [first, ...rest] = diffEntry(e.before, e.after)
            return (
              <tr
                key={e.id}
                tabIndex={onOpen ? 0 : undefined}
                title={onOpen && e.actor ? 'Double-click for the actor breakdown' : undefined}
                style={rowStyle(Boolean(onOpen && e.actor), e.id === selectedId)}
                aria-selected={e.id === selectedId || undefined}
                onDoubleClick={() => {
                  // A double-click also selects the word under the pointer.
                  window.getSelection()?.removeAllRanges()
                  open(e)
                }}
                onKeyDown={(ev: KeyboardEvent<HTMLTableRowElement>) => {
                  // Enter on the target link inside the row stays a link.
                  if (ev.key === 'Enter' && ev.target === ev.currentTarget) open(e)
                }}
              >
                <td className="mono" style={NOWRAP}>
                  <time dateTime={new Date(e.ts * 1000).toISOString()}>
                    {new Date(e.ts * 1000).toLocaleString()}
                  </time>
                </td>
                {showActor ? <td style={NOWRAP}>{e.actor || ABSENT}</td> : null}
                <td className="mono" style={NOWRAP}>
                  {e.action}
                </td>
                <td style={NOWRAP}>
                  <TargetCell kind={e.target_kind} id={e.target_id} />
                </td>
                <td className="mono" style={NOWRAP}>
                  {e.ip ?? ABSENT}
                </td>
                <td style={NOWRAP} title={first ? [first, ...rest].map(changeText).join('\n') : undefined}>
                  {first ? (
                    <>
                      {/* Only the first change is clipped: the count of the
                          rest is what says the row has more to read. */}
                      <span className="mono" style={clip('32ch')}>
                        {changeText(first)}
                      </span>
                      {rest.length > 0 ? <span className="unit"> (+{rest.length} more)</span> : null}
                    </>
                  ) : (
                    <span className="unit">{ABSENT}</span>
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export function TargetCell({ kind, id }: { kind: string; id: string }) {
  const { linkTo, navigate } = useRouter()
  const dest = targetDest(kind, id)
  return (
    <span>
      <span className="unit">{kind || ABSENT}</span>{' '}
      {dest ? (
        <a
          className="mono"
          href={linkTo(dest)}
          title={id}
          style={clip('24ch')}
          onClick={(ev: MouseEvent<HTMLAnchorElement>) => {
            if (!plainClick(ev)) return
            ev.preventDefault()
            navigate(dest)
          }}
        >
          {id}
        </a>
      ) : (
        <span className="mono" title={id || undefined} style={clip('24ch')}>
          {id || ABSENT}
        </span>
      )}
    </span>
  )
}

/** "Page X of Y · N entries" and Newer/Older. `noun` is singular, plural. */
export function Pager({
  page,
  pages,
  total,
  noun,
  onPage,
}: {
  page: number
  pages: number
  total: number
  noun: readonly [string, string]
  onPage: (page: number) => void
}) {
  return (
    <div
      style={{
        display: 'flex',
        gap: 'var(--space-10)',
        alignItems: 'center',
        justifyContent: 'flex-end',
        marginTop: 'var(--space-10)',
      }}
    >
      <span className="unit">
        Page <span className="mono">{page}</span> of <span className="mono">{pages}</span>
        {' · '}
        <span className="mono">{total}</span> {total === 1 ? noun[0] : noun[1]}
      </span>
      <button type="button" disabled={page <= 1} onClick={() => onPage(page - 1)}>
        <Icon name="chevron-left" size={14} decorative />
        Newer
      </button>
      <button type="button" disabled={page >= pages} onClick={() => onPage(page + 1)}>
        Older
        <Icon name="chevron-right" size={14} decorative />
      </button>
    </div>
  )
}
