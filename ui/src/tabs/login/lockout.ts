// What a refused sign-in says, reduced to the two facts the form needs: which
// refusal it was, and how long until the next attempt can succeed.
//
// Pure, so login.check.mjs can load it without a browser or a coordinator.
// The gateway's own message is rendered verbatim beside this; the only thing
// computed here is the wait, because a lockout that says "429" and nothing else
// leaves the person guessing when to try again.

/** The `code` field of an OpenAI-shaped error envelope
 *  (`{"error":{"code": ...}}`, gateway/errors.py), or null. */
export function errorCode(body: string | null | undefined): string | null {
  const parsed = parse(body)
  const code = parsed?.error?.code ?? parsed?.code
  return typeof code === 'string' ? code : null
}

/** Seconds until a locked sign-in may be retried.
 *
 *  The `Retry-After` header wins over the body's `retry_after_s`: the header
 *  is what HTTP defines for exactly this, and when both are present and differ
 *  the header is the one any other client would obey. The header may be
 *  delta-seconds or an HTTP-date (RFC 9110 §10.2.3); a date is measured from
 *  `now`. Null when neither says anything usable -- never a guessed default,
 *  because a made-up wait is a promise the gateway never made. */
export function retryAfterSeconds(
  header: string | null | undefined,
  body: string | null | undefined,
  now: number = Date.now(),
): number | null {
  const fromHeader = headerSeconds(header, now)
  if (fromHeader !== null) return fromHeader
  const parsed = parse(body)
  const raw = parsed?.error?.retry_after_s ?? parsed?.retry_after_s
  const n = typeof raw === 'number' ? raw : typeof raw === 'string' ? Number(raw) : NaN
  return Number.isFinite(n) && n >= 0 ? Math.ceil(n) : null
}

function headerSeconds(header: string | null | undefined, now: number): number | null {
  const v = header?.trim()
  if (!v) return null
  if (/^\d+$/.test(v)) return Number(v)
  const at = Date.parse(v)
  if (!Number.isFinite(at)) return null
  return Math.max(0, Math.ceil((at - now) / 1000))
}

/** "Try again in 45 s" / "in 2 min" / "in 1 h".
 *
 *  Always rounded UP to the unit shown: "in 1 min" for 61 s would send the
 *  person back while the lock still holds, and the next refusal costs them a
 *  longer one. So 61 s is "2 min" and 3599 s is "1 h". */
export function formatRetry(seconds: number): string {
  const s = Math.max(0, Math.ceil(seconds))
  if (s === 0) return 'Try again now'
  if (s < 60) return `Try again in ${s} s`
  const min = Math.ceil(s / 60)
  if (min < 60) return `Try again in ${min} min`
  return `Try again in ${Math.ceil(s / 3600)} h`
}

interface Envelope {
  code?: unknown
  retry_after_s?: unknown
  error?: { code?: unknown; retry_after_s?: unknown }
}

function parse(body: string | null | undefined): Envelope | null {
  if (!body) return null
  try {
    const v = JSON.parse(body) as unknown
    return v && typeof v === 'object' ? (v as Envelope) : null
  } catch {
    return null
  }
}
