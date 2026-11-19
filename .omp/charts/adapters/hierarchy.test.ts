import { describe, expect, it } from 'vitest'
import { flattenTree, pathsToTree, stratifyRows, sumTree, treeFromNested } from './hierarchy'
import { AdapterError } from './util'

describe('stratifyRows', () => {
  it('builds a single-root tree with names and values', () => {
    const tree = stratifyRows(
      [
        { id: 'r', parent: null, label: 'Root' },
        { id: 'a', parent: 'r', label: 'A', size: 2 },
        { id: 'b', parent: 'r', label: 'B', size: '3' },
        { id: 'a1', parent: 'a', label: 'A1', size: 5 },
      ],
      { id: 'id', parent: 'parent', name: 'label', value: 'size' },
    )
    expect(tree).toEqual({
      name: 'Root',
      id: 'r',
      children: [
        { name: 'A', id: 'a', value: 2, children: [{ name: 'A1', id: 'a1', value: 5 }] },
        { name: 'B', id: 'b', value: 3 },
      ],
    })
  })

  it('wraps several roots in a synthetic All', () => {
    const tree = stratifyRows(
      [
        { id: 'x', parent: '' },
        { id: 'y', parent: undefined },
      ],
      { id: 'id', parent: 'parent' },
    )
    expect(tree.name).toBe('All')
    expect(tree.children?.map((c) => c.name)).toEqual(['x', 'y'])
  })

  it('names the row whose parent does not exist', () => {
    const rows = [
      { id: 'r', parent: null },
      { id: 'a', parent: 'r' },
      { id: 'b', parent: 'r' },
      { id: 'c', parent: 'a' },
      { id: 'd', parent: 'X' },
    ]
    expect(() => stratifyRows(rows, { id: 'id', parent: 'parent' })).toThrow(new AdapterError("row 5: parent 'X' not found"))
  })

  it('refuses a cycle', () => {
    const rows = [
      { id: 'r', parent: null },
      { id: 'a', parent: 'b' },
      { id: 'b', parent: 'a' },
    ]
    expect(() => stratifyRows(rows, { id: 'id', parent: 'parent' })).toThrow(AdapterError)
    expect(() => stratifyRows(rows, { id: 'id', parent: 'parent' })).toThrow("row 2: cycle 'a' -> 'b' -> 'a'")
    expect(() => stratifyRows([{ id: 's', parent: 's' }], { id: 'id', parent: 'parent' })).toThrow("cycle 's' -> 's'")
  })

  it('refuses duplicate ids', () => {
    expect(() =>
      stratifyRows(
        [
          { id: 1, parent: null },
          { id: '1', parent: null },
        ],
        { id: 'id', parent: 'parent' },
      ),
    ).toThrow("row 2: duplicate id '1'")
  })
})

describe('pathsToTree', () => {
  it('builds nested nodes and sums repeated paths', () => {
    const tree = pathsToTree(
      [
        { p: 'src/a.ts', n: 10 },
        { p: 'src/lib/b.ts', n: 5 },
        { p: 'src/a.ts', n: 1 },
      ],
      { path: 'p', value: 'n' },
    )
    expect(tree).toEqual({
      name: 'src',
      children: [{ name: 'a.ts', value: 11 }, { name: 'lib', children: [{ name: 'b.ts', value: 5 }] }],
    })
  })

  it('uses a separator and an All root for several tops', () => {
    const tree = pathsToTree(
      [
        { p: 'x.y', n: 1 },
        { p: 'z', n: 2 },
      ],
      { path: 'p', separator: '.', value: 'n' },
    )
    expect(tree).toEqual({ name: 'All', children: [{ name: 'x', children: [{ name: 'y', value: 1 }] }, { name: 'z', value: 2 }] })
  })
})

describe('treeFromNested', () => {
  it('accepts a valid tree and copies it', () => {
    const src = { name: 'r', extra: 1, children: [{ name: 'a', value: 2 }, { name: 3 }] }
    const tree = treeFromNested(src)
    expect(tree).toEqual({ name: 'r', extra: 1, children: [{ name: 'a', value: 2 }, { name: '3' }] })
    expect(tree).not.toBe(src)
  })

  it('names the bad node', () => {
    expect(() => treeFromNested({ name: 'r', children: [{ name: 'a' }, { value: 1 }] })).toThrow(
      'root.children[1].name must be a string',
    )
    expect(() => treeFromNested({ name: 'r', value: 'x' })).toThrow('root.value must be a finite number')
    expect(() => treeFromNested({ name: 'r', children: {} })).toThrow('root.children must be an array')
    expect(() => treeFromNested(5)).toThrow('root must be an object')
  })
})

describe('sumTree / flattenTree', () => {
  const tree = {
    name: 'r',
    children: [{ name: 'a', children: [{ name: 'a1', value: 2 }, { name: 'a2' }, { name: 'a3', value: 3 }] }, { name: 'b', value: 4 }],
  }

  it('sums bottom-up with missing leaves as 0, without mutating', () => {
    const summed = sumTree(tree)
    expect(summed.value).toBe(9)
    expect(summed.children?.[0]?.value).toBe(5)
    expect(summed.children?.[0]?.children?.[1]?.value).toBe(0)
    expect('value' in tree).toBe(false)
  })

  it('flattens pre-order with path ids and depth', () => {
    expect(flattenTree(sumTree(tree))).toEqual([
      { id: 'r', parent: null, name: 'r', depth: 0, value: 9 },
      { id: 'r/a', parent: 'r', name: 'a', depth: 1, value: 5 },
      { id: 'r/a/a1', parent: 'r/a', name: 'a1', depth: 2, value: 2 },
      { id: 'r/a/a2', parent: 'r/a', name: 'a2', depth: 2, value: 0 },
      { id: 'r/a/a3', parent: 'r/a', name: 'a3', depth: 2, value: 3 },
      { id: 'r/b', parent: 'r', name: 'b', depth: 1, value: 4 },
    ])
  })
})
