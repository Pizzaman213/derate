import { describe, expect, it } from 'vitest'
import { daysInMonth, fromZoned, isLeapYear, isValidTimeZone, tzOffsetMinutes, wallToUtcMs, zonedParts } from './tz'

const NY = 'America/New_York'

describe('zonedParts', () => {
  it('reads wall fields in a zone', () => {
    expect(zonedParts(new Date('2026-03-08T07:30:00Z'), NY)).toEqual({ year: 2026, month: 3, day: 8, hour: 3, minute: 30, second: 0, weekday: 0 })
    expect(zonedParts(new Date('2026-01-01T00:00:00Z'), 'Asia/Tokyo')).toMatchObject({ year: 2026, month: 1, day: 1, hour: 9, weekday: 4 })
  })
  it('defaults to UTC', () => {
    expect(zonedParts(new Date('2026-12-31T23:59:59Z'))).toEqual({ year: 2026, month: 12, day: 31, hour: 23, minute: 59, second: 59, weekday: 4 })
  })
  it('reports midnight as hour 0', () => {
    expect(zonedParts(new Date('2026-06-01T04:00:00Z'), NY).hour).toBe(0)
  })
})

describe('tzOffsetMinutes', () => {
  it('follows DST', () => {
    expect(tzOffsetMinutes(new Date('2026-01-15T12:00:00Z'), NY)).toBe(-300)
    expect(tzOffsetMinutes(new Date('2026-07-15T12:00:00Z'), NY)).toBe(-240)
    expect(tzOffsetMinutes(new Date('2026-07-15T12:00:00Z'), 'Europe/London')).toBe(60)
    expect(tzOffsetMinutes(new Date('2026-07-15T12:00:00Z'), 'Asia/Kolkata')).toBe(330)
    expect(tzOffsetMinutes(new Date('2026-07-15T12:00:00Z'))).toBe(0)
  })
})

describe('fromZoned', () => {
  it('resolves ordinary wall times', () => {
    expect(fromZoned({ year: 2026, month: 1, day: 15, hour: 9 }, NY).toISOString()).toBe('2026-01-15T14:00:00.000Z')
    expect(fromZoned({ year: 2026, month: 3, day: 1, hour: 10 }, 'Asia/Tokyo').toISOString()).toBe('2026-03-01T01:00:00.000Z')
  })
  it('moves a spring-forward gap time forward', () => {
    const d = fromZoned({ year: 2026, month: 3, day: 8, hour: 2, minute: 30 }, NY)
    expect(d.toISOString()).toBe('2026-03-08T07:30:00.000Z')
    expect(zonedParts(d, NY)).toMatchObject({ hour: 3, minute: 30 })
    const london = fromZoned({ year: 2026, month: 3, day: 29, hour: 1, minute: 30 }, 'Europe/London')
    expect(london.toISOString()).toBe('2026-03-29T01:30:00.000Z')
    expect(zonedParts(london, 'Europe/London').hour).toBe(2)
  })
  it('picks the earlier instant of a fall-back overlap', () => {
    expect(fromZoned({ year: 2026, month: 11, day: 1, hour: 1, minute: 30 }, NY).toISOString()).toBe('2026-11-01T05:30:00.000Z')
    expect(fromZoned({ year: 2026, month: 10, day: 25, hour: 1, minute: 30 }, 'Europe/London').toISOString()).toBe('2026-10-25T00:30:00.000Z')
  })
  it('rolls over out-of-range days', () => {
    expect(fromZoned({ year: 2026, month: 3, day: 0 }, NY).toISOString()).toBe('2026-02-28T05:00:00.000Z')
  })
})

describe('calendar helpers', () => {
  it('knows leap years', () => {
    expect(isLeapYear(2024)).toBe(true)
    expect(isLeapYear(2000)).toBe(true)
    expect(isLeapYear(2100)).toBe(false)
    expect(isLeapYear(2026)).toBe(false)
    expect(daysInMonth(2024, 2)).toBe(29)
    expect(daysInMonth(2100, 2)).toBe(28)
    expect(daysInMonth(2026, 4)).toBe(30)
  })
  it('keeps years 0-99 literal', () => {
    expect(new Date(wallToUtcMs(50, 1, 1)).getUTCFullYear()).toBe(50)
  })
  it('validates time zones', () => {
    expect(isValidTimeZone('America/New_York')).toBe(true)
    expect(isValidTimeZone('UTC')).toBe(true)
    expect(isValidTimeZone('Mars/Olympus')).toBe(false)
    expect(isValidTimeZone('')).toBe(false)
    expect(() => zonedParts(new Date(0), 'Mars/Olympus')).toThrow(RangeError)
  })
})
