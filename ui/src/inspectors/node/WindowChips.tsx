import { HISTORY_WINDOWS, windowLabel, type HistoryWindow } from '../../state/history'

/** Which window every chart, table and percentile on the page is reading.
 *
 *  `live` is a different KIND of answer, not just a shorter one: it is the
 *  1 Hz frame accumulated in this tab, so it is always current and never
 *  survives a reload. The others come off the coordinator's archive, survive a
 *  restart, and can report that part of the window is missing. The page says
 *  which it is showing rather than making the chips imply a smooth continuum. */
export function WindowChips({
  value,
  onChange,
}: {
  value: HistoryWindow
  onChange: (w: HistoryWindow) => void
}) {
  return (
    <div className="chips" role="group" aria-label="Telemetry window">
      {HISTORY_WINDOWS.map((w) => (
        <button key={w} aria-pressed={w === value} onClick={() => onChange(w)}>
          {windowLabel(w)}
        </button>
      ))}
    </div>
  )
}
