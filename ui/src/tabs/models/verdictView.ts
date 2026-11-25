// The words the Models tab puts beside a lamp. Pure, so the list, the detail
// pane and the suggestions cannot disagree about what a verdict is called --
// and so `verdictView.check.mjs` can pin every one without a browser.
//
// Nothing here judges a fit. Every word is read off a server verdict.

import type { PlanResponse, QuantVariant } from '../../api/types'
import { fitLamp } from './ladder'
import type { Band } from './rows'

export type Signal = 'live' | 'warn' | 'fault' | 'idle'

export interface Word {
  label: string
  signal: Signal
}

/** A list row's band as one word. `notnow` is a warn, not a fault: the
 *  hardware holds it once a node is freed, so the remedy is local. */
export function bandWord(band: Band, checking: boolean): Word {
  switch (band) {
    case 'running':
      return { label: 'Running', signal: 'live' }
    case 'fits':
      return { label: 'Fits', signal: 'live' }
    case 'degraded':
      return { label: 'Slow', signal: 'warn' }
    case 'notnow':
      return { label: 'Not now', signal: 'warn' }
    case 'wont':
      return { label: 'Won’t fit', signal: 'fault' }
    case 'unchecked':
      return { label: checking ? 'Checking…' : 'Not checked', signal: 'idle' }
    case 'elsewhere':
      return { label: 'Provider', signal: 'idle' }
    case 'unserved':
      return { label: 'Not served', signal: 'idle' }
  }
}

/** The detail pane's verdict word. First rule wins, and the order matters:
 *  reality (running) outranks any prediction, and the STATIC verdict refusing
 *  is a fault while only the live one refusing is "free a node".
 *
 *  `hostPool` is a runtime that spends host RAM (llamacpp). There is no static
 *  host ceiling to read -- the server's static verdict for it is a refusal
 *  against 0 by design -- so a live verdict, when there is one, is the only
 *  basis, and the static refusal would put "Won't fit" over a live "fits". */
export function planWord(i: {
  running: boolean
  routing: boolean
  providerRuntime: boolean
  variant: QuantVariant | null
  result: PlanResponse | null
  checking: boolean
  hostPool?: boolean
}): Word {
  if (i.running) return { label: 'Running', signal: 'live' }
  if (i.routing) return { label: 'Provider', signal: 'idle' }
  if (i.providerRuntime) {
    if (!i.variant) return { label: 'Not checked', signal: 'idle' }
    const lamp = fitLamp(i.variant, false)
    return { label: lamp.label, signal: lamp.signal }
  }
  if (i.result == null)
    return { label: i.checking ? 'Checking…' : 'Not checked', signal: 'idle' }
  const fit = i.result.fit
  if (fit == null) return { label: 'Not checked', signal: 'idle' }
  const live = i.result.fit_live
  if (fit.verdict === 'wont_fit' && !(i.hostPool && live)) return { label: 'Won’t fit', signal: 'fault' }
  if (live?.verdict === 'wont_fit') return { label: 'Not now', signal: 'warn' }
  if ((live ?? fit).verdict === 'fits_degraded') return { label: 'Slow', signal: 'warn' }
  return { label: 'Fits', signal: 'live' }
}

/** Parameter count as a model card writes it. Only sub-100M stays in M: a
 *  490M checkpoint is conventionally called 0.5B, and the list should use the
 *  name people search for. */
export function formatParams(n: number | null): string {
  if (n == null) return ''
  if (n < 1e8) return `${(n / 1e6).toFixed(0)}M`
  if (n < 1e12) {
    const b = n / 1e9
    return `${b < 100 ? b.toFixed(1) : b.toFixed(0)}B`
  }
  return `${(n / 1e12).toFixed(1)}T`
}

/** Bytes as GiB -- the unit the fit gate's own sentences use, so a figure
 *  beside a reason never reads as a different number. */
export function gib(bytes: number | null): string {
  if (bytes == null) return '—'
  const g = bytes / 2 ** 30
  return `${g < 100 ? g.toFixed(1) : g.toFixed(0)} GiB`
}
