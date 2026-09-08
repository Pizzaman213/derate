// What a provider credential looks like, as two predicates.
//
// A port of control_plane/providers/secrets.py `looks_like_secret`. It lives
// beside redact.ts because that is one of its two callers -- the other is the
// add-provider field in tabs/settings/keyfield.ts, which re-exports it -- and
// because api/ must not import from tabs/.
//
// The two sides must agree. Where they drift, the UI either warns about input
// the server accepts or stays quiet about input it refuses, and neither is
// visible to tsc. ui/src/tabs/settings/keyfield.check.mjs asserts the
// agreement by running the Python.

/** Token shapes worth catching even when we have never seen the value.
 *  Conservative: a known vendor prefix plus a long opaque tail. */
const KEY_PATTERNS: RegExp[] = [
  /\bsk-or-v1-[A-Za-z0-9_-]{16,}/,
  /\bsk-ant-[A-Za-z0-9_-]{16,}/,
  /\bsk-proj-[A-Za-z0-9_-]{16,}/,
  /\bsk-[A-Za-z0-9_-]{20,}/,
  /\bgsk_[A-Za-z0-9_-]{20,}/,
  /\bhf_[A-Za-z0-9]{20,}/,
  /\bAIza[A-Za-z0-9_-]{20,}/,
  /\bbearer\s+[A-Za-z0-9._-]{20,}/i,
  /\b(api[-_]?key|access[-_]?token)\s*[=:]\s*["']?[A-Za-z0-9._-]{20,}/i,
]

/** True when a string looks like key material rather than a reference. */
export function looksLikeSecret(value: string): boolean {
  if (!value) return false
  if (KEY_PATTERNS.some((p) => p.test(value))) return true
  // A reference is short, name-shaped, and has no long random run. Anything
  // with 32+ contiguous characters mixing case and digits is a key.
  return (
    /[A-Za-z0-9_-]{32,}/.test(value) &&
    /\d/.test(value) &&
    /[a-z]/.test(value) &&
    /[A-Z0-9]/.test(value)
  )
}

/** True when a value is safe to display as a reference *name*.
 *
 *  Deliberately the inverse of `looksLikeSecret`, because that is the exact
 *  screen the value passed on the way in. A narrower rule here does not catch
 *  anything extra -- it only renders correctly-configured names as `***`,
 *  which is what a `^[A-Z][A-Z0-9_]{0,63}$` test did to `my-openrouter-key`. */
export function looksLikeRefName(value: unknown): boolean {
  if (typeof value !== 'string') return false
  if (value === '') return true
  if (value.length > 128) return false
  return !looksLikeSecret(value)
}
