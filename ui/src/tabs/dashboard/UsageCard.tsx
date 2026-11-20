import { useMemo } from 'react'
import type { Cluster } from '../../api/types'
import type { SafeMetricsFrame } from '../../state/useMetrics'
import { PackChart } from '../../components/PackChart'
import { AggregateRow, Tile } from './AggregateRow'
import { Verbatim } from '../../components/Verbatim'
import { useKeyedResource, useResource } from '../../state/backend'
import { count, percent } from '../keys/rows'
import {
  USAGE_DAYS,
  dayStarts,
  failuresPerDay,
  headline,
  localCloudShare,
  modelShare,
  requestsPerDayByKey,
  tokensPerDay,
  usageStart,
  type UsageDays,
} from './usageSummary'

const POLL_MS = 60_000
const SHARE_OPTIONS = { orientation: 'horizontal', showValues: true }
const DAY_OPTIONS = { valueFormat: 'si' }
const COUNT_DAY_OPTIONS = { valueFormat: 'integer' }
const FAILURE_OPTIONS = { xType: 'time', valueFormat: 'integer', points: 'never' }
// Two per line at desktop width, one on a narrow screen: three side by side
// squeezed the legends and bar labels.
const ROW_STYLE = { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 420px), 1fr))', gap: 'var(--space-24) var(--space-16)' } as const

/** Dashboard → Overview: one line of tiles (the live cluster figures from
 *  `AggregateRow`, then the window's usage, failures, spend, cache and
 *  fallbacks) and the usage charts under it. Two `GET /api/usage` reads over
 *  the same window -- by model and provider, and by key -- plus the key list
 *  for the keys' names. The window is chosen by `UsageWindow`. */
export function UsageCard({ days, cluster, frame }: { days: UsageDays; cluster: Cluster | null; frame: SafeMetricsFrame | null }) {
  const usage = useKeyedResource(
    `dash-usage|${days}`,
    (b) => b.usage({ start: usageStart(days), bucket: '1d', group_by: ['model', 'provider'] }),
    POLL_MS,
  )
  const byKey = useKeyedResource(
    `dash-usage-keys|${days}`,
    (b) => b.usage({ start: usageStart(days), bucket: '1d', group_by: ['key_id'] }),
    POLL_MS,
  )
  const keys = useResource((b) => b.keys(), POLL_MS)

  const result = usage.data
  const summary = useMemo(() => {
    if (!result) return null
    const rows = result.rows
    const dayList = dayStarts(result.to, days)
    return {
      head: headline(rows),
      perDay: tokensPerDay(rows, dayList),
      sides: localCloudShare(rows),
      models: modelShare(rows),
      failures: failuresPerDay(rows, dayList),
    }
  }, [result, days])
  const keysPerDay = useMemo(() => {
    if (!byKey.data) return []
    const names = new Map((keys.data ?? []).map((k) => [k.key_id, k.name]))
    return requestsPerDayByKey(byKey.data.rows, dayStarts(byKey.data.to, days), names)
  }, [byKey.data, keys.data, days])

  const head = summary?.head ?? null
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr)', gap: 'var(--space-16)' }}>
      <AggregateRow cluster={cluster} frame={frame}>
        <Tile
          value={head?.tokens ?? null}
          width={7}
          label={`tokens, ${days}d`}
          title={head ? `${count(head.tokensIn)} in · ${count(head.tokensOut)} out` : undefined}
        />
        <Tile value={head?.requests ?? null} width={5} label={`requests, ${days}d`} />
        <Tile value={head?.failed ?? null} width={4} label={`failed, ${days}d`} />
        <Tile value={head?.cloudShare == null ? null : percent(head.cloudShare)} unit="%" width={3} label="cloud share" />
        <Tile
          value={head?.cost ?? null}
          decimals={2}
          width={6}
          label={`$ spent, ${days}d${head?.costFloor ? ' (at least)' : ''}`}
          title={head?.costFloor ? 'Some requests had no price, so this is a floor.' : undefined}
        />
        <Tile value={head?.cachedShare == null ? null : percent(head.cachedShare)} unit="%" width={3} label="prompt cache hits" />
        <Tile value={head?.fallbacks ?? null} width={4} label={`fallbacks, ${days}d`} />
        <Tile value={head?.models ?? null} width={3} label="models used" />
      </AggregateRow>
      {usage.error ? (
        <Verbatim text={usage.error.message} />
      ) : !summary ? (
        <p className="unit" aria-busy="true">
          Reading usage…
        </p>
      ) : summary.head.requests > 0 ? (
        <>
          <div style={ROW_STYLE}>
            <PackChart type="stacked-bar" title={`Tokens per day, last ${days} days (UTC)`} height={220} data={summary.perDay} options={DAY_OPTIONS} />
            <PackChart type="stacked-bar" title="Requests per day by key" height={220} data={keysPerDay} options={COUNT_DAY_OPTIONS} />
          </div>
          <div style={ROW_STYLE}>
            <PackChart type="stacked-bar-100" title="Local vs cloud" height={160} data={summary.sides} options={SHARE_OPTIONS} />
            <PackChart type="stacked-bar-100" title="Models" height={160} data={summary.models} options={SHARE_OPTIONS} />
          </div>
          <PackChart
            type="multi-line"
            title="Failures per day, by error type"
            height={200}
            data={summary.failures}
            // Counts start at 0; an all-zero window would otherwise centre the line on -1..1,
            // and a top of at least 5 keeps the ticks whole numbers.
            options={{ ...FAILURE_OPTIONS, yDomain: [0, Math.max(5, ...summary.failures.map((r) => r.y ?? 0))] }}
          />
        </>
      ) : (
        <p className="unit">No requests in the last {days} days.</p>
      )}
    </div>
  )
}

/** The usage window, for the dashboard's tab bar. */
export function UsageWindow({ days, onChange }: { days: UsageDays; onChange: (d: UsageDays) => void }) {
  return (
    <span className="chips" role="group" aria-label="Usage window">
      {USAGE_DAYS.map((d) => (
        <button type="button" key={d} aria-pressed={d === days} onClick={() => onChange(d)}>
          {d}d
        </button>
      ))}
    </span>
  )
}
