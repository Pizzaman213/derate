import { useMemo } from 'react'
import type { QuantTable } from '../../api/types'
import { gbytes } from '../../format'
import { dominantColor } from './dominant'
import { avatarUrl, hashString, isFirstParty, ownerAccent, ownerInitials } from './owner'
import type { ModelRow } from './rows'
import { classifySupport } from './support'

/** One model as a card.
 *
 *  The geometry follows Unsloth Studio's hub card, which is the thing that
 *  makes a long list of models scannable instead of exhausting: a fixed tile,
 *  the publisher's mark large enough to recognise without reading, the name at
 *  two lines maximum, and the numbers pushed to the bottom edge where the eye
 *  can skip them. Written from that behaviour -- `studio/**` is AGPL-3.0-only
 *  and none of it is copied here.
 *
 *  Three things are said with colour and nothing else is:
 *
 *  - the accent, which identifies the publisher and means nothing more,
 *  - the status dots, which are the three signal colours doing their usual job,
 *  - and that is all. There is no colour for "popular" or "new".
 *
 *  Every dot has a sentence behind it. A dot on its own is a puzzle; the
 *  tooltip is where it becomes an answer, and the same text is the card's
 *  accessible name so it is not mouse-only. */
export function ModelCard({
  row,
  table,
  onOpen,
}: {
  row: ModelRow
  table: QuantTable | null
  onOpen: (row: ModelRow) => void
}) {
  const owner = row.model_id.includes('/') ? row.model_id.split('/')[0]! : ''
  const repo = row.model_id.includes('/') ? row.model_id.split('/').slice(1).join('/') : row.label

  const support = useMemo(() => classifySupport(row, table), [row, table])
  const url = avatarUrl(owner)
  const dominant = dominantColor(url)
  const accent = dominant ?? ownerAccent(owner)

  const onDevice = row.cachedOn.length > 0
  const unsupported = support.status === 'unsupported'
  const dots = [
    unsupported
      ? { key: 'unsupported', color: 'var(--fault-solid, var(--fault))', label: support.reason! }
      : null,
    onDevice
      ? {
          key: 'ondevice',
          color: 'var(--live-solid, var(--live))',
          label:
            `Already on ${row.cachedOn.length === 1 ? row.cachedOn[0] : `${row.cachedOn.length} nodes`}` +
            (row.bytesOnDisk != null ? `, ${gbytes(row.bytesOnDisk)} GiB` : '') +
            '. The first launch does not have to pull it.',
        }
      : null,
    row.verdict === 'wont_fit'
      ? { key: 'wontfit', color: 'var(--warn-solid, var(--warn))', label: row.reason ?? 'Will not fit.' }
      : null,
  ].filter(Boolean) as { key: string; color: string; label: string }[]

  // A deterministic aura position per card, so a grid does not look stamped
  // from one template but also never moves between renders.
  const h = hashString(row.model_id)
  const glowX = 12 + (h % 76)
  const glowY = 6 + ((h >>> 8) % 44)

  const size =
    row.total_params != null
      ? `${(row.total_params / 1e9).toFixed(row.total_params >= 1e11 ? 0 : 1)}B`
      : row.bytesOnDisk != null
        ? `${gbytes(row.bytesOnDisk)} GiB`
        : null

  const description = [
    `${repo} by ${owner || 'unknown publisher'}`,
    ...dots.map((d) => d.label),
  ].join('. ')

  return (
    <button
      type="button"
      className="mcard"
      aria-label={description}
      title={dots.length ? dots.map((d) => d.label).join('\n\n') : undefined}
      onClick={() => onOpen(row)}
      style={
        {
          '--accent': accent,
          '--glow-x': `${glowX}%`,
          '--glow-y': `${glowY}%`,
        } as React.CSSProperties
      }
    >
      <span className="mcard-top">
        <span className="mcard-avatar" style={{ background: accent }}>
          {url ? (
            <img src={url} alt="" loading="lazy" decoding="async" />
          ) : (
            <span className="mcard-initials">{ownerInitials(owner || repo)}</span>
          )}
        </span>

        <span className="mcard-id">
          <span className="mcard-name">{repo}</span>
          <span className="mcard-owner">
            <span className="mcard-owner-name">{owner || '—'}</span>
            {isFirstParty(owner) ? (
              <span className="mcard-verified" aria-label="Published by the team that trained it">
                ✓
              </span>
            ) : null}
          </span>
        </span>

        <span className="mcard-dots">
          {row.gated ? (
            <span className="mcard-glyph" aria-label="Gated repository">
              ⚿
            </span>
          ) : null}
          {dots.map((d) => (
            <span
              key={d.key}
              role="img"
              aria-label={d.label}
              className="mcard-dot"
              style={{ background: d.color }}
            />
          ))}
        </span>
      </span>

      <span className="mcard-foot">
        <span className="mcard-stats">
          {row.downloads != null ? (
            <span className="mcard-stat">
              <span aria-hidden>⤓</span> {compact(row.downloads)}
            </span>
          ) : null}
          {row.likes != null && row.likes > 0 ? (
            <span className="mcard-stat">
              <span aria-hidden>♥</span> {compact(row.likes)}
            </span>
          ) : null}
          {row.predicted_decode_tps != null ? (
            <span className="mcard-stat">{row.predicted_decode_tps.toFixed(0)} tok/s</span>
          ) : null}
        </span>
        {size ? <span className="mcard-chip">{size}</span> : null}
      </span>
    </button>
  )
}

/** 2.1M, 890k, 402. The hub's own counters run to eight figures and a card is
 *  204px wide. */
function compact(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`
  if (n >= 1e3) return `${Math.round(n / 1e3)}k`
  return String(n)
}
