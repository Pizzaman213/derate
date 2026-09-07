// Ported from mockups-next/derate.html's two static "Not built yet" /
// "Deliberately out of scope" cards, transcribed verbatim, plus the reversals
// card that grew alongside them.
//
// The "Containment" card that used to lead this file lives in
// ContainmentCard.tsx now. These three read rather than write: nothing here
// is a setting, which is why the sub-tab holding them is called About and not
// Policy.

const NOT_BUILT: [string, string][] = [
  ['Link utilisation', 'no bytes-on-the-wire telemetry exists'],
  ['Managed remote node tier', 'a target is local or a provider; there is no third kind'],
  ['Prefix cache hit rate', 'the backend reports null for it'],
  ['Scheduled model swaps', 'time-based placement'],
  ['Auto-eviction policy', 'currently always manual'],
  ['Alerting', 'node down, cap reached, OOM'],
]

//: Reversed non-goals. Kept on screen rather than quietly deleted: the card
//: below exists precisely because these get built by accident, so building one
//: on purpose has to be visible and dated, not tidied away.
const SCOPE_CHANGED: [string, string][] = [
  [
    'Manual placement',
    'built after all — a machine picker and manual TP/PP, with the planner’s recommendation and its own rejection line kept on screen. agents/E-planner.md always specified a recommendation, not a lock. Amended 2026-09-07',
  ],
  [
    'Model catalog browser',
    'built after all — browse, quantizations and fit. 00-architecture.md §1 amended 2026-09-07',
  ],
  [
    'Deep-dive metrics page',
    'a machine’s own page charts the durable archive. One node, not the cluster. Amended 2026-09-07',
  ],
  [
    'Log browser',
    'narrowed, not dropped: a node’s recent lines on its own page, with no search and no logger filter. Amended 2026-09-07',
  ],
]

const OUT_OF_SCOPE: [string, string][] = [
  ['Chat history', 'the Chat tab is a test console; the transcript is never stored'],
  ['Searchable logs', 'no query box and no logger filter, though the endpoint has both'],
  ['Cluster-wide metrics destination', 'depth lives on the machine it is about'],
  ['WAN endpoint', 'the gateway binds to the LAN'],
]

export function ScopeCards() {
  return (
    <>
      <div className="card2 soon">
        <h3>
          Not built yet <span className="pill">planned</span>
        </h3>
        {NOT_BUILT.map(([label, note]) => (
          <div className="row" key={label}>
            <span>{label}</span>
            <span className="unit">{note}</span>
          </div>
        ))}
      </div>

      <div className="card2">
        <h3>
          Scope changed <span className="pill">was out of scope</span>
        </h3>
        <div className="unit" style={{ marginBottom: 6 }}>
          A named non-goal that was built anyway. Recorded here rather than
          removed, because the card below is the thing that stops these
          happening by accident and it only works if reversals are visible.
        </div>
        {SCOPE_CHANGED.map(([label, note]) => (
          <div className="row" key={label}>
            <span>{label}</span>
            <span className="unit">{note}</span>
          </div>
        ))}
      </div>

      <div className="card2 soon">
        <h3>
          Deliberately out of scope <span className="pill">will not build</span>
        </h3>
        <div className="unit" style={{ marginBottom: 6 }}>
          Named non-goals. Each looks like a small addition to a screen that already exists, which
          is exactly how they get built by accident.
        </div>
        {OUT_OF_SCOPE.map(([label, note]) => (
          <div className="row" key={label}>
            <span>{label}</span>
            <span className="unit">{note}</span>
          </div>
        ))}
      </div>
    </>
  )
}
