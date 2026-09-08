// The add-provider key field, as pure functions.
//
// A provider's credential can be given two ways: paste the key and let the
// coordinator store it in secrets.json under a reference, or name a reference
// that already resolves. Putting one in the other's field is the mistake this
// module exists to catch, and it used to cost a round trip to find out --
// `looks_like_secret` lives on the server, so the form learned about a pasted
// key only from the 400 it came back as.
//
// The shape predicates themselves are shared with redact.ts and live in
// api/keyshape.ts; they are re-exported here so the form and its verifier have
// one import. keyfield.check.mjs asserts the whole surface against the Python.

import { looksLikeSecret, looksLikeRefName } from '../../api/keyshape'
import type { Provider } from '../../api/types'

export { looksLikeSecret, looksLikeRefName }

const MINTED_REF_PREFIX = 'DERATE_'
const MINTED_REF_SUFFIX = '_API_KEY'
const MAX_REF_LEN = 64

/** The secrets.json name a pasted key is stored under for this provider.
 *  Mirrors `minted_ref` in control_plane/providers/service.py. */
export function mintedRef(providerId: string): string {
  const stem =
    providerId.replace(/[^A-Za-z0-9]+/g, '_').replace(/^_+|_+$/g, '').toUpperCase() || 'PROVIDER'
  const budget = MAX_REF_LEN - MINTED_REF_PREFIX.length - MINTED_REF_SUFFIX.length
  return `${MINTED_REF_PREFIX}${stem.slice(0, budget)}${MINTED_REF_SUFFIX}`
}

/** The id the coordinator will mint for a new provider of this kind.
 *  Mirrors `_mint_id`: the kind, then the first free `-N` suffix. Predicted
 *  rather than known, so the form can name the reference it is about to
 *  create instead of describing it in the abstract. */
export function predictedProviderId(kind: string, taken: readonly string[]): string {
  const base = kind.toLowerCase().replace(/[^a-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '') || 'provider'
  if (!taken.includes(base)) return base
  let n = 2
  while (taken.includes(`${base}-${n}`)) n += 1
  return `${base}-${n}`
}

/** The hint for a kind whose key shape this build recognises nothing about. */
const GENERIC_KEY_PLACEHOLDER = 'sk-…'

/** What a key for this provider kind looks like, as a field placeholder.
 *
 *  Every prefix here is one `api/keyshape.ts` already recognises, rather than
 *  one remembered off a vendor's documentation -- so the hint the field shows
 *  and the screen that reads what was pasted into it cannot disagree about
 *  what a key for this kind is. A kind this build knows no prefix for gets the
 *  generic hint rather than a guess: Together publishes bare hex, and a custom
 *  upstream can issue whatever it likes.
 *
 *  keyfield.check.mjs asserts both halves -- every kind the server says needs
 *  a key has a hint, and every hint reads as key material once completed, so
 *  the form can never suggest a shape its own warning would complain about. */
export function keyPlaceholder(kind: string): string {
  switch (kind) {
    case 'openrouter':
      return 'sk-or-v1-…'
    case 'openai':
      return 'sk-proj-…'
    case 'anthropic':
      return 'sk-ant-…'
    case 'groq':
      return 'gsk_…'
    default:
      return GENERIC_KEY_PLACEHOLDER
  }
}

export type KeyMode = 'key' | 'ref'

/** The warning to show under the field, or null.
 *
 *  Always a warning, never a block: these are heuristics, and a heuristic that
 *  disables the button turns a false positive into an operator who cannot add
 *  their provider at all. The server holds the actual screen. */
export function keyFieldWarning(mode: KeyMode, value: string): string | null {
  const trimmed = value.trim()
  if (!trimmed) return null
  if (mode === 'ref') {
    return looksLikeSecret(trimmed)
      ? 'That looks like a key, not a name. Switch to “Paste a key” and it will be stored in secrets.json for you.'
      : null
  }
  // Paste mode. The inverse mistake: typing the name of an environment
  // variable into the field that takes the key itself.
  return /^[A-Za-z][A-Za-z0-9_]*$/.test(trimmed) && !looksLikeSecret(trimmed)
    ? 'That looks like the name of an environment variable, not a key. Switch to “Name a reference” to use it.'
    : null
}

// -- what is known about a key, which is never the key ----------------------

// Taken off `Provider` rather than restated, so a state the server starts
// sending cannot be one this file silently fails to describe.
export type KeyState = Provider['key_state']
export type KeySource = Provider['key_source']

/** The note under a provider's reference, saying what resolves and from where.
 *
 *  Four cases, and the fourth is the one worth spelling out: a null state is a
 *  provider port that does not answer `key_status`, not a provider without a
 *  key. Rendering that as "missing" would put a warning beside a provider that
 *  is authenticating perfectly well, on nothing but the absence of a method. */
export function keyStateNote(state: KeyState, source: KeySource): string {
  switch (state) {
    case 'set':
      return source ? `resolves from ${source}` : 'resolves'
    case 'missing':
      return 'does not resolve'
    case 'not_needed':
      return 'no key needed'
    default:
      return 'state unknown'
  }
}

/** Which status colour the note takes.
 *
 *  `missing` is a warning, not a fault: nothing is broken, the provider is
 *  switched off for want of a key and a key switches it back on. The row's
 *  State column is where a fault belongs. */
export function keyStateTone(state: KeyState): 'ok' | 'warn' | 'muted' {
  if (state === 'set') return 'ok'
  if (state === 'missing') return 'warn'
  return 'muted'
}
