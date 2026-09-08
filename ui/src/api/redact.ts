// Provider API keys are the one unrecoverable mistake in a tool people
// screenshot. The contract says a key never leaves the coordinator, and the UI
// trusts that — but trusting it is cheap to back up, so every payload that could
// carry a provider is scrubbed on the way in. If a future backend regression
// ever puts key material in a response, it dies here instead of on screen.
//
// There is deliberately no inverse of this function, and no reveal control.
// A key can now be *sent* — the add-provider form posts one, which the
// coordinator writes to secrets.json and keeps only the name of — but nothing
// sends one back, so this filter is unchanged in kind.
//
// `api_key_ref` is screened with the same predicate the server screens it with
// on the way in (api/keyshape.ts). A stricter test here catches nothing extra,
// because the value already passed that one; it only renders correctly-
// configured references as `***`, which is what /^[A-Z][A-Z0-9_]{0,63}$/ did
// to a secrets.json key named `my-openrouter-key`.
//
// One credential is nevertheless rendered: the enrollment token in the install
// command on Settings -> Add a node. It arrives inside `Enrollment.command` --
// a whole composed shell line, not a field named `token` -- so it passes this
// filter, and that is a decision rather than an oversight. The bound that makes
// it acceptable is what the token IS: minted on demand for one install, spent
// on first use, expiring within the hour, revocable from the same card. No
// endpoint returns the permanent cluster token, which is the secret this
// filter exists to keep off the screen; before this, the documented way to add
// a machine was to copy that one by hand.

import { looksLikeRefName } from './keyshape'

const KEY_LIKE = /^(api_?key|secret|token|authorization|auth|password|bearer)$/i

/** Anything that looks like a credential is replaced before it reaches a
 *  component. `api_key_ref` is an environment variable NAME and is kept. */
export function scrub<T>(value: T): T {
  return walk(value) as T
}

function walk(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(walk)
  if (value && typeof value === 'object') {
    const out: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      if (KEY_LIKE.test(k)) {
        out[k] = '***'
        continue
      }
      if (k === 'api_key_ref') {
        out[k] = looksLikeRefName(v) ? v : '***'
        continue
      }
      out[k] = walk(v)
    }
    return out
  }
  return value
}
