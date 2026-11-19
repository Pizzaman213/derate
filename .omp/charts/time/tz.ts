/**
 * Time-zone math on top of `Intl.DateTimeFormat` alone (Node 20 ships full ICU).
 * No tz database is bundled: the runtime's ICU is the only source of offsets.
 */

/** Wall-clock fields of an instant as seen in a time zone. */
export interface ZonedParts {
  year: number
  /** 1-12 */
  month: number
  day: number
  hour: number
  minute: number
  second: number
  /** 0 = Sunday .. 6 = Saturday */
  weekday: number
}

/** Wall-clock fields to resolve into an instant; time fields default to 0. */
export interface WallTime {
  year: number
  month: number
  day: number
  hour?: number
  minute?: number
  second?: number
  millisecond?: number
}

const WEEKDAYS: Record<string, number> = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 }
const formatters = new Map<string, Intl.DateTimeFormat>()

function isUtc(timeZone: string): boolean {
  return timeZone === 'UTC' || timeZone === 'Etc/UTC' || timeZone === 'GMT'
}

function formatterFor(timeZone: string): Intl.DateTimeFormat {
  let f = formatters.get(timeZone)
  if (!f) {
    try {
      f = new Intl.DateTimeFormat('en-US', {
        timeZone,
        hourCycle: 'h23',
        era: 'short',
        year: 'numeric',
        month: 'numeric',
        day: 'numeric',
        hour: 'numeric',
        minute: 'numeric',
        second: 'numeric',
        weekday: 'short',
      })
    } catch {
      throw new RangeError(`'${timeZone}' is not a time zone this runtime knows (try an IANA name like America/New_York)`)
    }
    formatters.set(timeZone, f)
  }
  return f
}

/** True when the runtime's ICU recognises `tz` as a time zone. */
export function isValidTimeZone(tz: string): boolean {
  if (typeof tz !== 'string' || tz.length === 0) return false
  try {
    formatterFor(tz)
    return true
  } catch {
    return false
  }
}

/** True for Gregorian leap years (2000 yes, 2100 no). */
export function isLeapYear(year: number): boolean {
  return (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0
}

/** Days in a 1-based month of a year, leap-aware. */
export function daysInMonth(year: number, month: number): number {
  if (month === 2) return isLeapYear(year) ? 29 : 28
  return [4, 6, 9, 11].includes(month) ? 30 : 31
}

/** UTC epoch ms of wall fields read as UTC; out-of-range fields roll over and years 0-99 are literal. */
export function wallToUtcMs(year: number, month: number, day: number, hour = 0, minute = 0, second = 0, ms = 0): number {
  const d = new Date(Date.UTC(year, month - 1, day, hour, minute, second, ms))
  if (year >= 0 && year < 100) d.setUTCFullYear(d.getUTCFullYear() - 1900)
  return d.getTime()
}

/** Wall-clock parts of `d` in `timeZone` (milliseconds dropped). */
export function zonedParts(d: Date, timeZone = 'UTC'): ZonedParts {
  if (isUtc(timeZone)) {
    return {
      year: d.getUTCFullYear(),
      month: d.getUTCMonth() + 1,
      day: d.getUTCDate(),
      hour: d.getUTCHours(),
      minute: d.getUTCMinutes(),
      second: d.getUTCSeconds(),
      weekday: d.getUTCDay(),
    }
  }
  const out: ZonedParts = { year: 0, month: 0, day: 0, hour: 0, minute: 0, second: 0, weekday: 0 }
  let bc = false
  for (const p of formatterFor(timeZone).formatToParts(d)) {
    switch (p.type) {
      case 'year': out.year = Number(p.value); break
      case 'month': out.month = Number(p.value); break
      case 'day': out.day = Number(p.value); break
      case 'hour': out.hour = Number(p.value) % 24; break
      case 'minute': out.minute = Number(p.value); break
      case 'second': out.second = Number(p.value); break
      case 'weekday': out.weekday = WEEKDAYS[p.value] ?? 0; break
      case 'era': bc = p.value.startsWith('B'); break
      default: break
    }
  }
  if (bc) out.year = 1 - out.year
  return out
}

/** Offset of `timeZone` from UTC at instant `d`, in minutes (New York in winter: -300). */
export function tzOffsetMinutes(d: Date, timeZone = 'UTC'): number {
  if (isUtc(timeZone)) return 0
  const t = d.getTime()
  const whole = t - (((t % 1000) + 1000) % 1000)
  const p = zonedParts(d, timeZone)
  return Math.round((wallToUtcMs(p.year, p.month, p.day, p.hour, p.minute, p.second) - whole) / 60000)
}

/**
 * Instant at which `timeZone`'s clock shows the given wall time. A wall time skipped by a
 * spring-forward resolves forward by the gap (02:30 -> 03:30 EDT); a repeated one resolves
 * to the earlier instant.
 */
export function fromZoned(p: WallTime, timeZone = 'UTC'): Date {
  const wall = wallToUtcMs(p.year, p.month, p.day, p.hour ?? 0, p.minute ?? 0, p.second ?? 0, p.millisecond ?? 0)
  if (isUtc(timeZone)) return new Date(wall)
  const DAY = 86_400_000
  const before = tzOffsetMinutes(new Date(wall - DAY), timeZone)
  const candidates = new Set([before, tzOffsetMinutes(new Date(wall), timeZone), tzOffsetMinutes(new Date(wall + DAY), timeZone)])
  let best: number | null = null
  for (const off of candidates) {
    const t = wall - off * 60_000
    if (tzOffsetMinutes(new Date(t), timeZone) === off && (best === null || t < best)) best = t
  }
  // No offset reproduces the wall time: it fell in a gap. Read it with the pre-transition
  // offset, which lands the same distance past the jump.
  return new Date(best ?? wall - before * 60_000)
}
