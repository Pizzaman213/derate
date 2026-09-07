import { useMemo } from 'react'
import type { QuantTable } from '../../api/types'
import { Lamp } from '../../components/Lamp'
import { sizeLabel } from '../../format'
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
  current,
}: {
  row: ModelRow
  table: QuantTable | null
  onOpen: (row: ModelRow) => void
  current?: boolean
}) {
  // A bare repo id (`gpt2`) has no publisher at all. That is different from a
  // publisher we could not identify, so the owner line is omitted rather than
  // drawn as an em dash.
  const slash = row.model_id.indexOf('/')
  const owner = slash > 0 ? row.model_id.slice(0, slash) : ''
  const repo = slash > 0 ? row.model_id.slice(slash + 1) : row.model_id

  const support = useMemo(() => classifySupport(row, table), [row, table])
  const url = avatarUrl(owner)
  const dominant = dominantColor(url)
  const accent = dominant ?? ownerAccent(owner)

  const onDevice = row.cachedOn.length > 0
  const unsupported = support.status === 'unsupported'
  const dots = [
    unsupported
      ? { key: 'unsupported', signal: 'fault' as const, label: support.reason! }
      : null,
    onDevice
      ? {
          key: 'ondevice',
          signal: 'live' as const,
          // No promise about the first launch. There is no expected size for a
          // base repository to check a cache against, so "it will not have to
          // pull this" is a claim nothing here can support -- several of these
          // hold a config file and no weights.
          label:
            `Cached on ${row.cachedOn.length === 1 ? row.cachedOn[0] : `${row.cachedOn.length} nodes`}` +
            (row.bytesOnDisk != null ? `: ${sizeLabel(row.bytesOnDisk)} on disk` : ''),
        }
      : null,
    row.verdict === 'wont_fit'
      ? { key: 'wontfit', signal: 'warn' as const, label: row.reason ?? 'Will not fit.' }
      : null,
  ].filter(Boolean) as { key: string; signal: 'live' | 'warn' | 'fault'; label: string }[]

  // A deterministic aura position per card, so a grid does not look stamped
  // from one template but also never moves between renders.
  const h = hashString(row.model_id)
  const glowX = 12 + (h % 76)
  const glowY = 6 + ((h >>> 8) % 44)

  const size =
    row.total_params != null
      ? `${(row.total_params / 1e9).toFixed(row.total_params >= 1e11 ? 0 : 1)}B`
      : row.bytesOnDisk != null
        ? sizeLabel(row.bytesOnDisk)
        : null

  const description = [
    owner ? `${repo} by ${owner}` : repo,
    ...dots.map((d) => d.label),
  ].join('. ')

  return (
    <button
      type="button"
      className="mcard"
      aria-current={current ? 'true' : undefined}
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
          <span className="mcard-owner" hidden={!owner}>
            <span className="mcard-owner-name">{owner}</span>
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
          {/* The app's own lamp, at card scale. A parallel dot component would
              have been a second indicator with the same job and its own rules
              about what the colours mean. */}
          {dots.map((d) => (
            <Lamp key={d.key} signal={d.signal} label={d.label} size={6} />
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
        {size ? <span className="pill">{size}</span> : null}
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
