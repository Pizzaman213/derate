import { describe, expect, it } from 'vitest'
import { chartSuite, host } from '../../tests/harness'
import gantt, { tasksOf } from './index'

chartSuite({
  module: gantt,
  dir: import.meta.dirname,
  role: 'task',
  marks: { basic: 11, realistic: 14, 'edge-single': 1, 'edge-zeros': 0, 'edge-duplicates': 10, 'edge-large': 5200 },
  summary: /^Gantt chart, 11 tasks in 4 groups, Mar 2026 to May 2026, 2 milestones, 16 dependencies, \d+% complete$/,
  malformed: [
    { name: 'end before start', data: [{ id: 'a', name: 'A', start: '2026-03-05', end: '2026-03-01' }], message: /field 'end': end 2026-03-01 is before start 2026-03-05 at row 1/ },
    { name: 'an unknown dependency', data: [{ id: 'a', name: 'A', start: '2026-03-01', end: '2026-03-02' }, { id: 'b', name: 'B', start: '2026-03-02', end: '2026-03-04', dependsOn: 'a, zzz' }], message: /field 'dependsOn': unknown task id 'zzz' .* at row 2/ },
    { name: 'a self dependency', data: [{ id: 'a', name: 'A', start: '2026-03-01', end: '2026-03-02', dependsOn: ['a'] }], message: /task 'a' depends on itself at row 1/ },
    { name: 'a bad start date', data: [{ name: 'A', start: 'soon' }], message: /field 'start' must be a date, got 'soon' at row 1/ },
    { name: 'a text duration', data: [{ name: 'A', start: '2026-03-01', duration: 'long' }], message: /field 'duration' must be a number, got 'long' at row 1/ },
    { name: 'a scalar', data: 42, message: /must be a list of rows, CSV text, or column arrays/ },
  ],
})

const twelve = Array.from({ length: 12 }, (_, i) => ({
  id: `t${i}`,
  name: `Task ${i}`,
  start: `2026-03-${String(2 + i).padStart(2, '0')}`,
  end: `2026-03-${String(6 + i).padStart(2, '0')}`,
  progress: (i % 5) / 4,
  dependsOn: i > 0 ? `t${i - 1}` : undefined,
  group: i < 6 ? 'A' : 'B',
}))
const attr = (e: Element, k: string) => Number(e.getAttribute(k))

describe('gantt: geometry', () => {
  it('draws 12 bars for 12 tasks, one arrow per dependency, progress = share of the bar', () => {
    const el = host(800, 500)
    const data = [...twelve, { id: 'm1', name: 'Gate', start: '2026-03-18', milestone: true, dependsOn: 't11, t5' }, { id: 'm2', name: 'Done', start: '2026-03-20', end: '2026-03-20', dependsOn: ['m1'] }]
    const h = gantt.render(el, data, { animate: false })
    expect(el.querySelectorAll('[data-role="task"][data-mark]').length).toBe(12)
    expect(el.querySelectorAll('[data-role="milestone"][data-mark]').length).toBe(2)
    expect(h.markCount()).toBe(14)
    expect(el.querySelectorAll('[data-role="dependency"]').length).toBe(11 + 2 + 1)
    for (const t of twelve) {
      const bar = el.querySelector(`[data-role="task"][data-key="rect:t${twelve.indexOf(t)}"]`)!
      const fill = el.querySelector(`[data-role="progress"][data-key="rect:p:t${twelve.indexOf(t)}"]`)
      if (t.progress === 0) { expect(fill).toBeNull(); continue }
      expect(attr(fill!, 'width')).toBeCloseTo(attr(bar, 'width') * t.progress, 1)
      expect(attr(fill!, 'x')).toBeCloseTo(attr(bar, 'x'), 1)
    }
    h.destroy()
  })
  it('places a bar from start to end, proportional to duration', () => {
    const el = host(800, 300)
    const h = gantt.render(el, [{ id: 'a', name: 'A', start: '2026-01-01', end: '2026-01-11' }, { id: 'b', name: 'B', start: '2026-01-11', duration: 5 }], { animate: false })
    const [a, b] = [...el.querySelectorAll('[data-role="task"]')]
    expect(attr(a!, 'width') / attr(b!, 'width')).toBeCloseTo(2, 1)
    expect(attr(a!, 'x') + attr(a!, 'width')).toBeCloseTo(attr(b!, 'x'), 1)
    h.destroy()
  })
  it('routes a finish-to-start arrow from the predecessor end to the successor start', () => {
    const el = host(800, 300)
    const h = gantt.render(el, [{ id: 'a', name: 'A', start: '2026-01-01', end: '2026-01-11' }, { id: 'b', name: 'B', start: '2026-01-15', end: '2026-01-20', dependsOn: 'a' }], { animate: false })
    const [a, b] = [...el.querySelectorAll('[data-role="task"]')]
    const d = el.querySelector('[data-role="dependency"]')!.getAttribute('d')!
    const nums = d.match(/-?[\d.]+/g)!.map(Number)
    const aEnd = attr(a!, 'x') + attr(a!, 'width'), bx = attr(b!, 'x')
    // leaves A just inside its finish, from its bottom edge; ends on B's start
    expect(nums[0]).toBeLessThanOrEqual(aEnd)
    expect(nums[0]).toBeGreaterThan(aEnd - 9)
    expect(nums[1]).toBeCloseTo(attr(a!, 'y') + attr(a!, 'height'), 1)
    expect(nums[nums.length - 4]).toBeCloseTo(bx, 1)
    h.destroy()
  })
  it('reads progress as percent when any value is above 1, and durations in days', () => {
    const { tasks } = tasksOf([
      { id: 'a', name: 'A', start: new Date('2026-01-01T00:00:00Z'), end: null, duration: 3, progress: 50, dependsOn: [], milestone: undefined, group: undefined },
      { id: 'b', name: 'B', start: new Date('2026-01-01T00:00:00Z'), end: null, duration: null, progress: 1, dependsOn: [], milestone: undefined, group: undefined },
    ], 'UTC')
    expect(tasks[0]!.progress).toBe(0.5)
    expect(tasks[0]!.end.toISOString()).toBe('2026-01-04T00:00:00.000Z')
    expect(tasks[1]!.progress).toBe(0.01)
    expect(tasks[1]!.milestone).toBe(true)
  })
  it('parses tz-less dates in options.timeZone', () => {
    const v = gantt.validate([{ name: 'A', start: '2026-03-02', end: '2026-03-03' }], { timeZone: 'Asia/Tokyo' })
    expect(v.data![0]!.start.toISOString()).toBe('2026-03-01T15:00:00.000Z')
  })
  it('draws a today line when asked, and notes when it falls outside', () => {
    const el = host(800, 300)
    const h = gantt.render(el, twelve, { animate: false, today: '2026-03-10' })
    expect(el.querySelector('[data-key="line:today-line"]')).not.toBeNull()
    h.update(twelve, { today: '2030-01-01' })
    expect(el.querySelector('[data-key="line:today-line"]')).toBeNull()
    expect(el.querySelector('.oc-note')!.textContent).toMatch(/outside the schedule/)
    h.destroy()
  })
  it('lists groups under header rows', () => {
    const el = host(800, 500)
    const h = gantt.render(el, twelve, { animate: false })
    const labels = [...el.querySelectorAll('.oc-labels text')].map((t) => t.textContent)
    expect(labels.indexOf('A')).toBeLessThan(labels.indexOf('Task 0'))
    expect(labels.indexOf('B')).toBeGreaterThan(labels.indexOf('Task 5'))
    h.destroy()
  })
})
