import { describe, expect, it } from 'vitest'
import { coerceCell, detectDelimiter, looksLikeCSV, parseCSV, toCSV } from './csv'

describe('parseCSV', () => {
  it('keeps commas inside quoted fields', () => {
    expect(parseCSV('name,city\n"Smith, J","Paris, FR"\n')).toEqual([{ name: 'Smith, J', city: 'Paris, FR' }])
  })

  it('keeps an embedded newline and escaped quotes', () => {
    expect(parseCSV('a,b\n"line1\nline2","say ""hi"""\n')).toEqual([{ a: 'line1\nline2', b: 'say "hi"' }])
  })

  it('strips a BOM from the first header', () => {
    const rows = parseCSV('\uFEFFid,v\n1,2')
    expect(Object.keys(rows[0] ?? {})).toEqual(['id', 'v'])
    expect(rows).toEqual([{ id: 1, v: 2 }])
  })

  it('handles CRLF line endings', () => {
    expect(parseCSV('a,b\r\n1,x\r\n2,y\r\n')).toEqual([
      { a: 1, b: 'x' },
      { a: 2, b: 'y' },
    ])
  })

  it('auto-detects semicolon, tab and pipe', () => {
    expect(parseCSV('a;b\n1;2')).toEqual([{ a: 1, b: 2 }])
    expect(parseCSV('a\tb\n1\t2')).toEqual([{ a: 1, b: 2 }])
    expect(parseCSV('a|b\n1|2')).toEqual([{ a: 1, b: 2 }])
    // a semicolon file whose values contain decimal commas in quotes
    expect(parseCSV('price;qty\n"1,5";3')).toEqual([{ price: '1,5', qty: 3 }])
  })

  it('honours an explicit delimiter', () => {
    expect(parseCSV('a;b,c\n1;2,3', { delimiter: ',' })).toEqual([{ 'a;b': '1;2', c: 3 }])
  })

  it('skips blank lines and pads short rows with null', () => {
    expect(parseCSV('a,b\n1,2\n\n3\n')).toEqual([
      { a: 1, b: 2 },
      { a: 3, b: null },
    ])
  })

  it('returns [] for empty text', () => {
    expect(parseCSV('')).toEqual([])
  })
})

describe('coerceCell', () => {
  it.each([
    ['', null],
    ['null', null],
    ['NA', null],
    ['N/A', null],
    ['42', 42],
    ['-7', -7],
    ['3.25', 3.25],
    ['-1.5e3', -1500],
    ['2E-2', 0.02],
    ['.5', 0.5],
    ['true', true],
    ['false', false],
    ['2024-01-31', '2024-01-31'],
    ['1,234', '1,234'],
    ['abc', 'abc'],
    ['True', 'True'],
    ['1e', '1e'],
  ])('%j -> %j', (cell, expected) => {
    expect(coerceCell(cell)).toBe(expected)
  })
})

describe('detectDelimiter / looksLikeCSV', () => {
  it('picks the delimiter occurring most in the header line', () => {
    expect(detectDelimiter('a;b;c\n1,2;3;4')).toBe(';')
    expect(detectDelimiter('"x,y"\tz\n')).toBe('\t')
    expect(detectDelimiter('single\n1')).toBe(',')
  })

  it('needs a newline and a delimiter on the first line', () => {
    expect(looksLikeCSV('a,b\n1,2')).toBe(true)
    expect(looksLikeCSV('a\tb\r\n1\t2')).toBe(true)
    expect(looksLikeCSV('a,b')).toBe(false)
    expect(looksLikeCSV('hello\nworld')).toBe(false)
  })
})

describe('toCSV', () => {
  it('quotes per RFC 4180 and renders null/Date', () => {
    const out = toCSV(
      ['name', 'note', 'when', 'n'],
      [
        ['a,b', 'say "hi"', new Date('2024-01-02T03:04:05.000Z'), 1],
        [null, 'two\nlines', undefined, -2.5],
      ],
    )
    expect(out).toBe(
      'name,note,when,n\r\n"a,b","say ""hi""",2024-01-02T03:04:05.000Z,1\r\n,"two\nlines",,-2.5',
    )
  })

  it('round-trips through parseCSV', () => {
    const csv = toCSV(['x', 'y'], [['a,"b"', 1], ['c', null]])
    expect(parseCSV(csv)).toEqual([
      { x: 'a,"b"', y: 1 },
      { x: 'c', y: null },
    ])
  })
})
