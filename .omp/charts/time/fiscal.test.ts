import { describe, expect, it } from 'vitest'
import { fiscalQuarter, fiscalQuarterStart, fiscalYearStart } from './fiscal'

const at = (s: string) => new Date(`${s}T12:00:00Z`)

describe('fiscalQuarter', () => {
  it.each<[number, string, number, number, string]>([
    [1, '2026-01-01', 2026, 1, 'FY26 Q1'],
    [1, '2026-03-31', 2026, 1, 'FY26 Q1'],
    [1, '2026-04-01', 2026, 2, 'FY26 Q2'],
    [1, '2026-12-31', 2026, 4, 'FY26 Q4'],
    [4, '2026-03-31', 2026, 4, 'FY26 Q4'],
    [4, '2026-04-01', 2027, 1, 'FY27 Q1'],
    [4, '2026-06-30', 2027, 1, 'FY27 Q1'],
    [4, '2026-07-01', 2027, 2, 'FY27 Q2'],
    [4, '2027-01-01', 2027, 4, 'FY27 Q4'],
    [7, '2026-06-30', 2026, 4, 'FY26 Q4'],
    [7, '2026-07-01', 2027, 1, 'FY27 Q1'],
    [7, '2026-10-01', 2027, 2, 'FY27 Q2'],
    [7, '2027-01-01', 2027, 3, 'FY27 Q3'],
    [7, '2027-04-01', 2027, 4, 'FY27 Q4'],
    [10, '2026-09-30', 2026, 4, 'FY26 Q4'],
    [10, '2026-10-01', 2027, 1, 'FY27 Q1'],
    [10, '2026-12-31', 2027, 1, 'FY27 Q1'],
    [10, '2027-01-01', 2027, 2, 'FY27 Q2'],
  ])('startMonth %i: %s -> FY%i Q%i', (startMonth, day, fiscalYear, quarter, label) => {
    expect(fiscalQuarter(at(day), { startMonth })).toEqual({ fiscalYear, quarter, label })
  })
  it('reads the day in the given zone', () => {
    // 2026-04-01T02:00Z is still 31 March in New York.
    const d = new Date('2026-04-01T02:00:00Z')
    expect(fiscalQuarter(d, { startMonth: 4 }).label).toBe('FY27 Q1')
    expect(fiscalQuarter(d, { startMonth: 4, timeZone: 'America/New_York' }).label).toBe('FY26 Q4')
  })
  it('pads two-digit year labels', () => {
    expect(fiscalQuarter(at('2008-05-01')).label).toBe('FY08 Q2')
  })
  it('rejects a bad startMonth', () => {
    expect(() => fiscalQuarter(at('2026-01-01'), { startMonth: 13 })).toThrow(RangeError)
    expect(() => fiscalQuarter(at('2026-01-01'), { startMonth: 0 })).toThrow(RangeError)
  })
})

describe('fiscalYearStart / fiscalQuarterStart', () => {
  it('starts the year in the calendar year before it is named for', () => {
    expect(fiscalYearStart(2027, { startMonth: 4 }).toISOString()).toBe('2026-04-01T00:00:00.000Z')
    expect(fiscalYearStart(2027, { startMonth: 10 }).toISOString()).toBe('2026-10-01T00:00:00.000Z')
    expect(fiscalYearStart(2027).toISOString()).toBe('2027-01-01T00:00:00.000Z')
  })
  it('starts quarters three months apart, crossing calendar years', () => {
    expect(fiscalQuarterStart(2027, 3, { startMonth: 7 }).toISOString()).toBe('2027-01-01T00:00:00.000Z')
    expect(fiscalQuarterStart(2027, 4, { startMonth: 10 }).toISOString()).toBe('2027-07-01T00:00:00.000Z')
    expect(fiscalQuarterStart(2027, 2, { startMonth: 4, timeZone: 'America/New_York' }).toISOString()).toBe('2026-07-01T04:00:00.000Z')
  })
  it('round-trips with fiscalQuarter', () => {
    for (const startMonth of [1, 4, 7, 10]) {
      for (const q of [1, 2, 3, 4] as const) {
        const start = fiscalQuarterStart(2030, q, { startMonth })
        expect(fiscalQuarter(start, { startMonth })).toMatchObject({ fiscalYear: 2030, quarter: q })
        expect(fiscalQuarter(new Date(start.getTime() - 1), { startMonth }).quarter).toBe(q === 1 ? 4 : q - 1)
      }
    }
  })
})
