import { describe, expect, it } from 'vitest'
import { AdapterError, columnsToRows, inferFieldTypes, isDateString, mapFields, rowsToColumns, toRows } from './rows'

describe('toRows', () => {
  it('returns an array of objects as-is', () => {
    const rows = [{ a: 1 }, { a: 2 }]
    expect(toRows(rows)).toBe(rows)
  })

  it('parses CSV and TSV text', () => {
    expect(toRows('a,b\n1,x')).toEqual([{ a: 1, b: 'x' }])
    expect(toRows('a\tb\n1\tx')).toEqual([{ a: 1, b: 'x' }])
  })

  it('turns column arrays into rows', () => {
    expect(toRows({ a: [1, 2], b: ['x', 'y'] })).toEqual([
      { a: 1, b: 'x' },
      { a: 2, b: 'y' },
    ])
  })

  it('names the mismatched column exactly', () => {
    expect(() => toRows({ a: [1, 2, 3, 4], b: [1, 2, 3] })).toThrow(
      new AdapterError("column 'b' has 3 values but column 'a' has 4"),
    )
    expect(() => toRows({ a: [1, 2, 3, 4], b: [1, 2, 3] })).toThrow(AdapterError)
  })

  it('reads a header-first table', () => {
    expect(
      toRows([
        ['x', 'y'],
        [1, 2],
        [3],
      ]),
    ).toEqual([
      { x: 1, y: 2 },
      { x: 3, y: null },
    ])
  })

  it('rejects anything else with null', () => {
    expect(toRows(42)).toBeNull()
    expect(toRows('just text')).toBeNull()
    expect(toRows([1, 2])).toBeNull()
    expect(toRows({ a: 1 })).toBeNull()
    expect(toRows(null)).toBeNull()
    expect(toRows([])).toEqual([])
  })
})

describe('mapFields', () => {
  const rows = [
    { date: '2024-01', revenue: 10, region: 'EU' },
    { date: '2024-02', revenue: 12, region: 'US' },
  ]

  it('sets canonical keys and keeps the other columns', () => {
    expect(mapFields(rows, { x: 'date', y: 'revenue', series: 'region' })).toEqual([
      { date: '2024-01', revenue: 10, region: 'EU', x: '2024-01', y: 10, series: 'EU' },
      { date: '2024-02', revenue: 12, region: 'US', x: '2024-02', y: 12, series: 'US' },
    ])
  })

  it('names the missing column and the ones that exist', () => {
    expect(() => mapFields(rows, { x: 'date', y: 'revnue' })).toThrow(
      "field mapping: column 'revnue' not found (columns: date, revenue, region)",
    )
  })

  it('accepts a column that appears in only some rows', () => {
    expect(mapFields([{ a: 1 }, { b: 2 }], { y: 'b' })).toEqual([
      { a: 1, y: null },
      { b: 2, y: 2 },
    ])
  })
})

describe('columnsToRows / rowsToColumns', () => {
  it('round-trips', () => {
    const cols = { a: [1, 2], b: ['x', null] }
    expect(rowsToColumns(columnsToRows(cols))).toEqual(cols)
  })

  it('fills missing cells with null and honours a column list', () => {
    expect(rowsToColumns([{ a: 1 }, { b: 2 }])).toEqual({ a: [1, null], b: [null, 2] })
    expect(rowsToColumns([{ a: 1, b: 2 }], ['b'])).toEqual({ b: [2] })
  })

  it('handles no columns', () => {
    expect(columnsToRows({})).toEqual([])
  })
})

describe('inferFieldTypes', () => {
  it('classifies each column', () => {
    const rows = [
      { n: 1, d: '2024-01-31', dt: new Date(0), s: 'a', b: true, m: 1, e: null },
      { n: 2.5, d: '2024-02-01T10:00:00Z', dt: new Date(1), s: 'b', b: false, m: 'x', e: '' },
      { n: null, d: null, dt: null, s: null, b: null, m: null },
    ]
    expect(inferFieldTypes(rows)).toEqual({
      n: 'number',
      d: 'date',
      dt: 'date',
      s: 'string',
      b: 'boolean',
      m: 'mixed',
      e: 'empty',
    })
  })

  it('recognises common date forms and not plain words', () => {
    for (const s of ['2024-01', '2024/1/31', '01/31/2024', '31.01.2024', 'Jan 31, 2024', '31 January 2024'])
      expect(isDateString(s)).toBe(true)
    for (const s of ['hello', '1,234', '2024', '12:30']) expect(isDateString(s)).toBe(false)
  })
})
