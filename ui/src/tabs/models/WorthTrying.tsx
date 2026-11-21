import { Lamp } from '../../components/Lamp'
import { OwnerMark, useAvatars } from './OwnerMark'
import { ownerOf } from './owner'
import { band } from './rows'
import type { ModelRow } from './rows'
import { bandWord, formatParams } from './verdictView'

export interface WorthTryingProps {
  rows: ModelRow[]
  onOpen(row: ModelRow): void
  onBrowse(): void
}

/** Models nothing here has touched that the gate says would run now. A
 *  suggestion, and it says what it is ordered by: the rows arrive sorted by
 *  downloads, a fact on the wire, never by a taste this screen invented. */
export function WorthTrying({ rows, onOpen, onBrowse }: WorthTryingProps) {
  useAvatars()
  if (!rows.length) return null
  return (
    <section className="mdl-worth">
      <div className="mdl-worth-head">
        <span className="mdl-group-title">
          Worth trying
          <span className="unit">
            {' '}
            · {rows.length} not downloaded yet that fit your nodes now
          </span>
        </span>
        <button type="button" className="mdl-textbtn unit" onClick={onBrowse}>
          Browse Hugging Face
        </button>
      </div>
      <div className="mdl-worth-grid">
        {rows.map((row) => {
          const word = bandWord(band(row), row.checking)
          const facts = [
            formatParams(row.total_params),
            row.downloads != null ? `${row.downloads.toLocaleString()} dl` : '',
          ].filter(Boolean)
          return (
            <button key={row.key} type="button" className="mdl-worth-tile" onClick={() => onOpen(row)}>
              <span className="mdl-cell-name">
                <OwnerMark owner={ownerOf(row.model_id)} variant="inline" reserve />
                <span className="mono mdl-ellipsis">{row.model_id}</span>
              </span>
              <span className="mdl-hub-card-verdict">
                <Lamp signal={word.signal} hollow={word.signal === 'idle'} label={word.label} />
                <span className="unit">{facts.join(' · ') || word.label}</span>
              </span>
            </button>
          )
        })}
      </div>
    </section>
  )
}
