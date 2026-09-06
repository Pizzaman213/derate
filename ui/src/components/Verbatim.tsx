/** Text that came from the planner or the fit gate.
 *
 *  These strings are more precise than any rewrite, and they are the product:
 *  the reason a plan was chosen and the reason a launch was refused. They are
 *  rendered exactly as received — never truncated, reflowed into a summary,
 *  sentence-cased, or paraphrased. This component exists so that intent is
 *  visible at every call site.
 */
export function Verbatim({
  text,
  size = 'body',
}: {
  text: string
  size?: 'body' | 'label'
}) {
  return (
    <p
      className={size === 'label' ? 'label' : undefined}
      style={{
        margin: 0,
        fontWeight: 400,
        color: 'var(--ink)',
        // Preserve whatever whitespace the component sent, without collapsing
        // an intentional line break into a space.
        whiteSpace: 'pre-wrap',
      }}
    >
      {text}
    </p>
  )
}

/** The planner's rejected list, one line each, verbatim. */
export function VerbatimList({ items }: { items: string[] }) {
  if (items.length === 0) return null
  return (
    <ul
      style={{
        listStyle: 'none',
        margin: 0,
        padding: 0,
        display: 'grid',
        gap: 6,
      }}
    >
      {items.map((line) => (
        <li
          key={line}
          className="label"
          style={{
            fontWeight: 400,
            color: 'var(--ink-muted)',
            display: 'grid',
            gridTemplateColumns: '10px 1fr',
            gap: 6,
            whiteSpace: 'pre-wrap',
          }}
        >
          <span aria-hidden className="mono">
            {'×'}
          </span>
          <span>{line}</span>
        </li>
      ))}
    </ul>
  )
}
