import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import { cleanTree, toTree } from '../_shared/PartWholeB'
import sunburst, { sunburstLayout } from './index'

chartSuite({
  module: sunburst,
  dir: import.meta.dirname,
  role: 'arc',
  marks: { basic: 12, realistic: 26, 'edge-single': 1, 'edge-negatives': 5, 'edge-nulls': 7, 'edge-duplicates': 7, 'edge-large': 5417, 'edge-outliers': 15 },
  summary: /^Sunburst, 4 groups, 8 items, total 800, largest Engineering at 390 \(4[89]%\)$/,
  empty: ['edge-empty', 'edge-zeros'],
  malformed: [
    { name: 'a text value in rows', data: [{ path: 'a/b', value: 1 }, { path: 'a/c', value: 'lots' }], message: /field 'value' must be a number, got 'lots' at row 2/ },
    { name: 'a scalar', data: 'just text', message: /the data must be a tree/ },
    { name: 'an unknown parent', data: [{ id: 'a', parent: '' }, { id: 'b', parent: 'zz', value: 2 }], message: /row 2: parent 'zz' not found/ },
    { name: 'children that are not a list', data: { name: 'r', children: { name: 'a' } }, message: /root\.children must be an array/ },
  ],
})

const tree = { name: 'r', children: [{ name: 'a', children: [{ name: 'a1', value: 7 }, { name: 'a2', value: 3 }, { name: 'a3', children: [{ name: 'x', value: 2 }, { name: 'y', value: 5 }] }] }, { name: 'b', value: 11 }, { name: 'c', children: [{ name: 'c1', value: 1 }, { name: 'c2', value: 23 }] }] }

describe('sunburst: geometry', () => {
  it("children's angles sum to their parent's angle; the root spans the full turn", () => {
    const laid = sunburstLayout(cleanTree(toTree(tree, {})).root!)
    expect(laid.x1 - laid.x0).toBeCloseTo(Math.PI * 2, 9)
    for (const n of laid.descendants()) {
      if (!n.children) continue
      const sum = n.children.reduce((s, c) => s + (c.x1 - c.x0), 0)
      expect(sum).toBeCloseTo(n.x1 - n.x0, 9)
      // and each child's angle is its share of the parent's
      for (const c of n.children) expect((c.x1 - c.x0) / (n.x1 - n.x0)).toBeCloseTo(c.data.value / n.data.value, 9)
    }
  })
  it('one ring per level, the root as the centre disc', () => {
    const laid = sunburstLayout(cleanTree(toTree(tree, {})).root!)
    for (const n of laid.descendants()) {
      expect(n.y0).toBe(n.depth)
      expect(n.y1).toBe(n.depth + 1)
    }
  })
  it('renders every node as an arc, the root first', () => {
    const el = host(500, 500)
    const h = sunburst.render(el, tree, { animate: false, height: 500 })
    const arcs = [...el.querySelectorAll('[data-role="arc"][data-mark]')]
    expect(arcs).toHaveLength(11)
    expect(arcs[0]!.getAttribute('data-key')).toBe('path:r')
    expect(el.querySelector('[aria-live]')).not.toBeNull()
    h.destroy()
  })
  it('negatives are omitted and noted', () => {
    const el = host()
    const h = sunburst.render(el, [{ label: 'a', value: 4 }, { label: 'b', value: -1 }, { label: 'c', value: 6 }], { animate: false })
    expect(h.markCount()).toBe(3)
    expect(el.querySelector('.oc-note')!.textContent).toBe('1 negative values omitted')
    h.destroy()
  })
  it('hiding a branch re-divides the turn among the rest', () => {
    const el = host(500, 500)
    const h = sunburst.render(el, tree, { animate: false, height: 500 })
    ;(el.querySelector('.oc-legend button[data-key="c"]') as HTMLElement).click()
    const keys = [...el.querySelectorAll('[data-role="arc"][data-mark]')].map((a) => a.getAttribute('data-key'))
    expect(keys).not.toContain('path:r › c')
    expect(h.markCount()).toBe(8)
    h.focusMark(0)
    expect(el.querySelector('[aria-live]')!.textContent).toBe('1 of 8: r: 28 in total')
    h.destroy()
  })
})
