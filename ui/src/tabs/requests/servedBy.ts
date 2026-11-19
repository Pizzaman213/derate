// Who answered a request, in words. Pure: the rows come from /api/requests,
// the two lists from /api/providers and /api/cluster.

import type { RequestRow } from '../../api/types'

/** Last explicit 429 attempt. Never infer a past rate limit from current state. */
export function rateLimitAttempt(row: Pick<RequestRow, 'attempt_chain'>) {
  const chain = row.attempt_chain
  if (!chain) return undefined
  for (let i = chain.length - 1; i >= 0; i--) {
    const attempt = chain[i]
    if (attempt?.status === 429) return attempt
  }
  return undefined
}

export interface ServedByRow {
  target_id: string
  target_kind: string
  provider_id: string
  deployment_id: string
  node_id: string
}

export interface ServedByProvider {
  provider_id: string
  display_name?: string | null
}

export interface ServedByDeployment {
  deployment_id: string
  served_name: string
  node_ids: string[]
}

export function servedBy(
  row: ServedByRow,
  providers: readonly ServedByProvider[],
  deployments: readonly ServedByDeployment[],
): string {
  if (row.target_id === '') return '—'
  if (row.target_kind === 'remote') {
    const p = providers.find((x) => x.provider_id === row.provider_id)
    return `${p?.display_name ?? row.provider_id} · ${row.provider_id}`
  }
  const d = deployments.find((x) => x.deployment_id === row.deployment_id)
  if (d) return `${d.served_name} on ${d.node_ids.join(', ')}`
  return row.node_id || row.target_id
}

export type RequestWindow = '24h' | '7d' | '30d'

/** The `start=` a window asks /api/requests for. No window is the last 24h. */
export function windowStart(win: string | null | undefined): string {
  if (win === '7d') return '-7d'
  if (win === '30d') return '-30d'
  return '-24h'
}

/** How much of the prompt the prefix cache served, as "42% of prompt". Null
 *  when either side was not reported or there was no prompt to share. A
 *  nonzero read never rounds to "0%". */
export function cachedShare(cacheRead: number | null, promptTokens: number | null): string | null {
  if (cacheRead == null || promptTokens == null || promptTokens <= 0) return null
  const pct = (cacheRead / promptTokens) * 100
  if (cacheRead > 0 && pct < 1) return '<1% of prompt'
  return `${Math.round(pct)}% of prompt`
}
