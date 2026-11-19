import { describe, expect, it } from 'vitest'
import { graphFromMatrix, hasCycle, matrixFromGraph, toGraph } from './graph'

describe('toGraph', () => {
  it('collects nodes first-seen, sums duplicate links, defaults value to 1', () => {
    const g = toGraph(
      [
        { from: 'b', to: 'a' },
        { from: 'a', to: 'c' },
        { from: 'b', to: 'a' },
      ],
      { source: 'from', target: 'to' },
    )
    expect(g).toEqual({
      nodes: [{ id: 'b' }, { id: 'a' }, { id: 'c' }],
      links: [
        { source: 'b', target: 'a', value: 2 },
        { source: 'a', target: 'c', value: 1 },
      ],
    })
  })

  it('reads a value column', () => {
    const g = toGraph(
      [
        { s: 'x', t: 'y', w: 2.5 },
        { s: 'x', t: 'y', w: '1.5' },
      ],
      { source: 's', target: 't', value: 'w' },
    )
    expect(g.links).toEqual([{ source: 'x', target: 'y', value: 4 }])
  })

  it('refuses an empty endpoint', () => {
    expect(() => toGraph([{ s: 'x', t: '' }], { source: 's', target: 't' })).toThrow('row 1: target is empty')
  })
})

describe('matrix conversions', () => {
  it('graphFromMatrix keeps non-zero cells', () => {
    const g = graphFromMatrix(
      ['a', 'b'],
      [
        [0, 3],
        [1, 0],
      ],
    )
    expect(g).toEqual({
      nodes: [{ id: 'a' }, { id: 'b' }],
      links: [
        { source: 'a', target: 'b', value: 3 },
        { source: 'b', target: 'a', value: 1 },
      ],
    })
  })

  it('refuses a non-square matrix', () => {
    expect(() => graphFromMatrix(['a', 'b'], [[1, 2]])).toThrow('matrix has 1 rows but there are 2 names')
    expect(() => graphFromMatrix(['a'], [[1, 2]])).toThrow('matrix row 0 has 2 values but there are 1 names')
  })

  it('matrixFromGraph round-trips and appends unknown endpoints', () => {
    const m = { names: ['a', 'b', 'c'], matrix: [[0, 2, 0], [0, 0, 5], [1, 0, 0]] }
    expect(matrixFromGraph(graphFromMatrix(m.names, m.matrix))).toEqual(m)
    expect(
      matrixFromGraph({
        nodes: [{ id: 'a' }],
        links: [
          { source: 'a', target: 'z', value: 1 },
          { source: 'a', target: 'z', value: 2 },
        ],
      }),
    ).toEqual({ names: ['a', 'z'], matrix: [[0, 3], [0, 0]] })
  })
})

describe('hasCycle', () => {
  const g = (pairs: [string, string][]) => ({ nodes: [], links: pairs.map(([source, target]) => ({ source, target, value: 1 })) })

  it('detects cycles and self-loops', () => {
    expect(hasCycle(g([['a', 'b'], ['b', 'c'], ['c', 'a']]))).toBe(true)
    expect(hasCycle(g([['a', 'a']]))).toBe(true)
  })

  it('accepts DAGs, including diamonds', () => {
    expect(hasCycle(g([['a', 'b'], ['a', 'c'], ['b', 'd'], ['c', 'd']]))).toBe(false)
    expect(hasCycle(g([]))).toBe(false)
  })
})
