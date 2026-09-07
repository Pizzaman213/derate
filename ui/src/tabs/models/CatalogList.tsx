export interface CatalogEntry {
  model_id: string
  label: string
  detail: string
  group: string
  default_context?: number
  default_concurrency?: number
  /** Served by somebody else's hardware: there is nothing here to resolve it
   *  against, so it opens nothing. */
  remote?: boolean
}

/** The list itself: rule-topped group headings, one `.deprow` per model.
 *
 *  `.deprow` rather than a table because these rows are the primary thing you
 *  click, and it is the row pattern the deployments strip already established
 *  for that -- hover and selection wash to `--panel-sunk`, edge to edge. */
export function CatalogList({
  entries,
  loading,
  error,
  emptyNote,
  onOpen,
}: {
  entries: CatalogEntry[]
  loading: boolean
  error: Error | null
  emptyNote: string
  onOpen: (entry: CatalogEntry) => void
}) {
  if (error) {
    return (
      <p
        className="label"
        style={{ fontWeight: 400, color: 'var(--fault)', whiteSpace: 'pre-wrap' }}
      >
        {error.message}
      </p>
    )
  }
  if (!entries.length) {
    return <p className="unit">{loading ? 'Loading…' : emptyNote}</p>
  }

  const groups = new Map<string, CatalogEntry[]>()
  for (const entry of entries) {
    const bucket = groups.get(entry.group)
    if (bucket) bucket.push(entry)
    else groups.set(entry.group, [entry])
  }

  return (
    <div>
      {[...groups.entries()].map(([name, rows]) => (
        <section key={name}>
          <div className="sub">{name}</div>
          {rows.map((entry) => (
            <div
              key={`${entry.group}::${entry.model_id}::${entry.label}`}
              className="deprow"
              style={{ gridTemplateColumns: '1fr 2fr auto' }}
              role={entry.remote ? undefined : 'button'}
              tabIndex={entry.remote ? undefined : 0}
              onClick={() => {
                if (!entry.remote) onOpen(entry)
              }}
              onKeyDown={(e) => {
                if (entry.remote) return
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  onOpen(entry)
                }
              }}
            >
              <span className="mono" style={{ wordBreak: 'break-all' }}>
                {entry.label}
              </span>
              <span className="unit" style={{ wordBreak: 'break-all' }}>
                {entry.detail}
              </span>
              <span className="unit">
                {entry.remote ? 'remote' : 'quantizations ▸'}
              </span>
            </div>
          ))}
        </section>
      ))}
    </div>
  )
}
