// What a provider row says about its last key check, kept apart from React.
//
// A plain module because `check.mjs` bundles verifiers with esbuild's
// `platform: 'neutral'`, so anything importing React cannot be verified. The
// warning sentence itself is NOT authored here: the server writes it once
// (`ProviderService._key_warning`) and the row renders it verbatim.

import type { Provider } from '../../api/types'

type Check = Provider['key_check']

/** Seconds as the coarse "N ago" a row has room for. */
function ago(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  if (s < 90) return `${s}s ago`
  if (s < 5400) return `${Math.round(s / 60)}m ago`
  if (s < 172800) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}

/** One line on what the last key check found, for the key that resolves now. */
export function keyCheckNote(check: Check, now: number): string {
  if (check == null) return 'key not checked yet'
  const when = ago(now - check.checked_at)
  if (check.state === 'ok') return `key check passed ${when}`
  if (check.state === 'rejected') return `key check rejected ${when}`
  return `key check inconclusive ${when}: ${check.detail ?? ''}`
}

export function keyCheckTone(check: Check): 'ok' | 'fault' | 'muted' {
  if (check?.state === 'ok') return 'ok'
  if (check?.state === 'rejected') return 'fault'
  return 'muted'
}

/** Said beside a key read from the coordinator's environment, because
 *  changing that variable does nothing until a restart, and pasting a key
 *  does something at once. Null for every other source. */
export function environmentKeyNote(source: Provider['key_source'], ref: string): string | null {
  if (source !== 'environment' || !ref) return null
  return `${ref} is read from the coordinator's environment at startup: a new value there takes effect only after a restart. Replace key stores a key in secrets.json, which takes effect at once.`
}

/** The warn-days field: a whole number from 0 to 365, or null. */
export function parseWarnDays(text: string): number | null {
  const t = text.trim()
  if (!/^\d+$/.test(t)) return null
  const n = Number(t)
  return n <= 365 ? n : null
}
