import { describe, expect, it } from 'vitest'
import { AdapterError, columnsOf, isNumericString, isRecord, keyOf, toNumber } from './util'
import * as adapters from './index'

describe('util', () => {
  it('toNumber accepts finite numbers and numeric strings only', () => {
    expect(toNumber(3)).toBe(3)
    expect(toNumber(' -2.5e1 ')).toBe(-25)
    expect(toNumber('1,234')).toBeNull()
    expect(toNumber(Number.NaN)).toBeNull()
    expect(toNumber(Number.POSITIVE_INFINITY)).toBeNull()
    expect(toNumber(true)).toBeNull()
    expect(toNumber(null)).toBeNull()
    expect(isNumericString('1.')).toBe(true)
    expect(isNumericString('')).toBe(false)
  })

  it('keyOf separates types and matches Dates by time', () => {
    expect(keyOf(1)).not.toBe(keyOf('1'))
    expect(keyOf(new Date(3))).toBe(keyOf(new Date(3)))
    expect(keyOf(null)).toBe(keyOf(undefined))
  })

  it('columnsOf is first-seen across rows', () => {
    expect(columnsOf([{ b: 1 }, { a: 1, b: 2 }, { c: 3 }])).toEqual(['b', 'a', 'c'])
  })

  it('isRecord accepts plain objects only', () => {
    expect(isRecord({})).toBe(true)
    expect(isRecord([])).toBe(false)
    expect(isRecord(new Date())).toBe(false)
    expect(isRecord(null)).toBe(false)
  })

  it('AdapterError is an Error with its own name', () => {
    const e = new AdapterError('x')
    expect(e).toBeInstanceOf(Error)
    expect(e.name).toBe('AdapterError')
  })

  it('index re-exports every adapter', () => {
    for (const fn of ['parseCSV', 'toRows', 'mapFields', 'pivot', 'unpivot', 'aggregate', 'groupAggregate', 'normalize', 'topN', 'binValues', 'stackRows', 'stratifyRows', 'pathsToTree', 'treeFromNested', 'sumTree', 'flattenTree', 'toGraph', 'graphFromMatrix', 'matrixFromGraph', 'hasCycle', 'inferFieldTypes', 'columnsToRows', 'rowsToColumns', 'looksLikeCSV', 'toCSV'])
      expect(typeof (adapters as Record<string, unknown>)[fn]).toBe('function')
    expect(adapters.AdapterError).toBe(AdapterError)
  })
})
