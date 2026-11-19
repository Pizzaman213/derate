import { Icon } from './Icon'

export interface IssueAction {
  label: string
  onClick: () => void
}

export interface IssueRow {
  id: string
  level: 'error' | 'warn'
  /** Shown verbatim: a refusal names what to change, and rewording it loses that. */
  message: string
  actions?: IssueAction[]
}

export type IssueListProps = {
  /** What the list is about; announced, so it is text. */
  label: string
  issues: IssueRow[]
  /** Said when there is nothing to report. */
  empty?: string
}

/** A list of problems, errors first. Red is broken, amber is worth a look --
 *  the colour and the icon both say which, so neither is the only channel. */
export function IssueList({ label, issues, empty = 'Nothing to report.' }: IssueListProps) {
  const rows = [...issues].sort((a, b) => (a.level === b.level ? 0 : a.level === 'error' ? -1 : 1))
  if (rows.length === 0) {
    return (
      <p className="unit" style={{ margin: 0 }} aria-label={label}>
        {empty}
      </p>
    )
  }
  return (
    <ul aria-label={label} style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 'var(--space-6)' }}>
      {rows.map((i) => (
        <li
          key={i.id}
          style={{
            display: 'flex',
            alignItems: 'flex-start',
            gap: 'var(--space-8)',
            color: i.level === 'error' ? 'var(--fault)' : 'var(--warn)',
          }}
        >
          <Icon name={i.level === 'error' ? 'x-circle' : 'alert-triangle'} size={14} label={i.level === 'error' ? 'Error' : 'Warning'} />
          <span style={{ flex: 1, whiteSpace: 'pre-wrap' }}>{i.message}</span>
          {(i.actions ?? []).map((a) => (
            <button key={a.label} type="button" className="ghost" onClick={a.onClick}>
              {a.label}
            </button>
          ))}
        </li>
      ))}
    </ul>
  )
}
