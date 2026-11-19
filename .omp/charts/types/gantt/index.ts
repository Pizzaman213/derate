// Gantt chart: one bar per task on a time axis (top), grouped under header rows, with
// progress fills, milestone diamonds, finish-to-start dependency arrows and an optional
// 'today' line.

import { defineChart, phrase, SIZES } from '../../core'
import type { Formatters, Mark } from '../../core'
import { offsetBy } from '../../time/granularity'
import { categoryColors, diamond, fit, paddedDomain, resolveToday, todayMarks, zonedRows } from '../_shared/TimelineA'
import { input, options, type GanttOptions, type GanttRow } from './schema'

export interface Task {
  key: string
  id: string
  name: string
  start: Date
  end: Date
  /** Fraction done, 0-1, or null when not given. */
  progress: number | null
  deps: string[]
  milestone: boolean
  group: string | undefined
  /** Row number in the input (1-based). */
  row: number
}

/** Resolve ends, progress scale, milestones and unique keys. */
export function tasksOf(rows: GanttRow[], timeZone: string): { tasks: Task[]; duplicates: number } {
  const percent = rows.some((r) => r.progress !== null && r.progress > 1)
  const seen = new Map<string, number>()
  let duplicates = 0
  const tasks = rows.map((r, i): Task => {
    const id = r.id ?? r.name
    const n = (seen.get(id) ?? 0) + 1
    seen.set(id, n)
    if (n > 1) duplicates++
    let end = r.end
    if (!end && r.duration !== null) {
      const whole = Math.floor(r.duration)
      end = new Date(+offsetBy(r.start, 'day', whole, { timeZone }) + (r.duration - whole) * 864e5)
    }
    end ??= r.start
    const progress = r.progress === null ? null : Math.min(1, Math.max(0, percent ? r.progress / 100 : r.progress))
    return { key: n > 1 ? `${id}#${n}` : id, id, name: r.name, start: r.start, end, progress, deps: r.dependsOn, milestone: r.milestone ?? +end === +r.start, group: r.group, row: i + 1 }
  })
  return { tasks, duplicates }
}

const days = (t: Task) => (+t.end - +t.start) / 864e5

type Line = { kind: 'group'; key: string; label: string; tasks: Task[] } | { kind: 'task'; key: string; task: Task }

/** Display order: ungrouped tasks first, then each group (first-seen order) under its header. */
function linesOf(tasks: Task[]): Line[] {
  if (!tasks.some((t) => t.group !== undefined)) return tasks.map((t) => ({ kind: 'task', key: `t:${t.key}`, task: t }))
  const out: Line[] = tasks.filter((t) => t.group === undefined).map((t) => ({ kind: 'task', key: `t:${t.key}`, task: t }))
  const groups = new Map<string, Task[]>()
  for (const t of tasks) if (t.group !== undefined) groups.set(t.group, [...(groups.get(t.group) ?? []), t])
  for (const [g, ts] of groups) {
    out.push({ kind: 'group', key: `g:${g}`, label: g, tasks: ts })
    for (const t of ts) out.push({ kind: 'task', key: `t:${t.key}`, task: t })
  }
  return out
}

function taskLabel(t: Task, fmt: Formatters): string {
  if (t.milestone) return `${t.name} (milestone, ${fmt.date(t.start, 'day')})${t.progress === 1 ? ': done' : ''}`
  return `${t.name} (${fmt.date(t.start, 'day')} – ${fmt.date(t.end, 'day')})${t.progress !== null ? `: ${fmt.pct(t.progress)} done` : ''}`
}

export const gantt = defineChart<GanttRow[], GanttOptions>({
  id: 'gantt',
  name: 'Gantt chart',
  category: 'timeline',
  intents: ['schedule', 'timeline'],
  summary: 'Tasks as bars on a time axis, with progress, milestones and dependencies.',
  input,
  options,
  shape: 'rows',
  fields: ['id', 'name', 'start', 'end', 'duration', 'progress', 'dependsOn', 'milestone', 'group'],
  dateFields: ['start', 'end'],
  numberFields: ['duration', 'progress'],
  zoom: 'x',
  aspect: 0.6,
  minHeight: 280,
  maxHeight: 720,
  adapt: (raw, o) => zonedRows(raw, ['start', 'end'], o.timeZone),
  draw(ctx) {
    const { theme, options: o, fmt, width: W } = ctx
    const FS = SIZES.tick
    const { tasks, duplicates } = tasksOf(ctx.data, fmt.timeZone)
    const lines = linesOf(tasks)
    const grouped = lines.some((l) => l.kind === 'group')
    const indent = grouped ? 12 : 0
    const colors = categoryColors(tasks.map((t) => t.group), theme)
    const colorOf = (t: Task) => (grouped ? colors.color(t.group) : theme.cat(0))
    const byId = new Map<string, Task>()
    for (const t of tasks) if (!byId.has(t.id)) byId.set(t.id, t)

    // label column
    const widest = Math.max(...lines.map((l) => (l.kind === 'group' ? ctx.measure(l.label, FS, 600) : ctx.measure(l.task.name, FS) + indent)))
    const labelW = Math.max(Math.min(64, W * 0.25), Math.min(widest, W * 0.3))

    const today = resolveToday(o.today, fmt.timeZone)
    let lo = Math.min(...tasks.map((t) => +t.start)), hi = Math.max(...tasks.map((t) => +t.end))
    if (today && +today >= lo - (hi - lo) * 0.25 && +today <= hi + (hi - lo) * 0.25) { lo = Math.min(lo, +today); hi = Math.max(hi, +today) }
    const f = ctx.cartesian({
      x: { type: 'time', domain: paddedDomain(lo, hi, 0.04) },
      y: { type: 'band', domain: lines.map((l) => l.key), padding: 0.3, paddingOuter: 0.2 },
      xAxis: 'top', yAxis: 'none', grid: 'x', zoom: true,
      pad: { left: labelW + 8 },
    })
    const { plot } = f
    const step = f.bh / (1 - 0.3) // band step: paddingInner is 0.3
    const bh = Math.max(0.5, f.bh)
    const marks: Mark[] = [...f.marks]
    const cy = (key: string) => f.py(key) + f.bh / 2
    // label thinning: group headers first, then tasks, never two labels closer than k rows
    const k = step >= FS * 1.2 ? 1 : Math.ceil((FS * 1.2) / Math.max(step, 0.01))
    const labelled = new Array<boolean>(lines.length).fill(k === 1)
    if (k > 1) {
      const free = (i: number) => { for (let j = Math.max(0, i - k + 1); j < Math.min(lines.length, i + k); j++) if (labelled[j]) return false; return true }
      lines.forEach((l, i) => { if (l.kind === 'group' && free(i)) labelled[i] = true })
      lines.forEach((l, i) => { if (l.kind === 'task' && free(i)) labelled[i] = true })
    }
    const inPlot = (x: number) => x >= plot.x && x <= plot.x + plot.width

    // dependency arrows first, so bars paint over the elbows
    const arrows = o.arrows && step >= 4
    const depCount = tasks.reduce((n, t) => n + t.deps.length, 0)
    if (arrows) {
      const g = Math.min(8, Math.max(3, step * 0.3)), hs = Math.min(5, Math.max(2.5, bh * 0.35))
      for (const t of tasks) for (const d of t.deps) {
        const from = byId.get(d)
        if (!from) continue
        const r = Math.min(bh / 2 + 1, 8)
        const y1 = cy(`t:${from.key}`), y2 = cy(`t:${t.key}`)
        const down = y2 >= y1 ? 1 : -1
        // leave from the bottom (or top) edge just inside the predecessor's finish, so the
        // label after the bar stays clear; enter the successor at its start
        const fx = f.px(from.end), fw = fx - f.px(from.start)
        const x1 = from.milestone ? fx : fx - Math.min(g, fw / 2)
        const e1 = y1 + down * (from.milestone ? r : bh / 2)
        const x2 = t.milestone ? f.px(t.start) - r : f.px(t.start)
        let d0: string
        if (x2 - x1 >= g) d0 = `M${x1},${e1}V${y2}H${x2}`
        else {
          const ym = y2 - down * step / 2
          d0 = `M${x1},${e1}V${ym}H${x2 - g}V${y2}H${x2}`
        }
        marks.push({ type: 'path', role: 'dependency', key: `dep:${from.key}>${t.key}`, d: `${d0}M${x2 - hs},${y2 - hs * 0.75}L${x2},${y2}L${x2 - hs},${y2 + hs * 0.75}`, stroke: theme.muted, strokeWidth: 1.25, fill: 'none', linejoin: 'round', linecap: 'round', inert: true })
      }
    }

    lines.forEach((l, i) => {
      const y = f.py(l.key)
      const showLabel = labelled[i]! && step >= 2
      if (l.kind === 'group') {
        const c = colors.color(l.label)
        marks.push({ type: 'rect', layer: 'back', key: `gbg:${l.key}`, x: 0, y: y - (step - f.bh) / 2, width: plot.x + plot.width, height: step, fill: theme.track, fillOpacity: 0.45, inert: true })
        const g0 = Math.min(...l.tasks.map((t) => +t.start)), g1 = Math.max(...l.tasks.map((t) => +t.end))
        const x0 = f.px(new Date(g0)), x1 = f.px(new Date(g1)), th = Math.max(1, Math.min(5, bh * 0.35))
        marks.push({ type: 'path', layer: 'marks', key: `gspan:${l.key}`, d: `M${x0},${y + bh / 2 + th}V${y + bh / 2 - th / 2}H${x1}V${y + bh / 2 + th}`, stroke: c, strokeWidth: 2, fill: 'none', linejoin: 'round', inert: true })
        if (showLabel) {
          const t = fit(ctx, l.label, labelW, FS)
          marks.push({ type: 'text', layer: 'labels', key: `glabel:${l.key}`, x: 4, y: y + f.bh / 2, text: t.text, full: t.full, className: t.full ? 'oc-trunc' : undefined, anchor: 'start', baseline: 'central', size: FS, weight: 600, fill: theme.ink })
        }
        return
      }
      const t = l.task
      const c = colorOf(t)
      const tip = {
        title: t.name,
        color: c,
        rows: [
          ...(t.milestone ? [['Date', fmt.dateTime(t.start)] as [string, string]] : [['Start', fmt.dateTime(t.start)], ['End', fmt.dateTime(t.end)], ['Duration', `${fmt.num(days(t), 1)} days`]] as Array<[string, string]>),
          ...(t.progress !== null ? [['Progress', fmt.pct(t.progress)] as [string, string]] : []),
          ...(t.deps.length ? [['Depends on', t.deps.map((d) => byId.get(d)?.name ?? d).join(', ')] as [string, string]] : []),
          ...(t.group !== undefined ? [['Group', t.group] as [string, string]] : []),
        ],
      }
      const ci = grouped ? colors.index(t.group) : 0
      if (t.milestone) {
        const x = f.px(t.start), r = Math.max(2, Math.min(bh / 2 + 1, 8)), yc = y + f.bh / 2
        marks.push({ type: 'path', role: 'milestone', key: t.key, d: diamond(x, yc, r), cx: x, cy: yc, box: { x: x - r, y: yc - r, width: 2 * r, height: 2 * r }, fill: t.progress === null || t.progress >= 1 ? c : theme.surface, stroke: c, strokeWidth: 1.5, pattern: grouped ? ci : undefined, datum: t, label: taskLabel(t, fmt), tooltip: tip })
      } else {
        const x0 = f.px(t.start), w = Math.max(1, f.px(t.end) - x0), rx = Math.min(3, bh / 4, w / 2)
        marks.push({ type: 'rect', role: 'task', key: t.key, x: x0, y, width: w, height: bh, rx, fill: theme.mix(c, theme.surface, 0.6), stroke: c, strokeWidth: bh >= 4 ? 1 : 0, pattern: grouped ? ci : undefined, datum: t, label: taskLabel(t, fmt), tooltip: tip })
        if (t.progress !== null && t.progress > 0) marks.push({ type: 'rect', role: 'progress', key: `p:${t.key}`, x: x0, y, width: w * t.progress, height: bh, rx, fill: c, inert: true })
        const pct = fmt.pct(t.progress)
        const tx = x0 + w + 4
        if (o.showProgress && t.progress !== null && bh >= FS * 0.8 && inPlot(tx) && tx + ctx.measure(pct, FS) <= plot.x + plot.width) {
          marks.push({ type: 'text', layer: 'labels', key: `pl:${t.key}`, x: tx, y: y + bh / 2, text: pct, anchor: 'start', baseline: 'central', size: FS, fill: theme.muted, inert: true })
        }
      }
      if (showLabel) {
        const lt = fit(ctx, t.name, labelW - indent, FS)
        marks.push({ type: 'text', layer: 'labels', key: `tlabel:${l.key}`, x: 4 + indent, y: y + f.bh / 2, text: lt.text, full: lt.full, className: lt.full ? 'oc-trunc' : undefined, anchor: 'start', baseline: 'central', size: FS, fill: theme.inkSecondary })
      }
    })

    if (today) {
      const x = f.px(today)
      if (inPlot(x)) marks.push(...todayMarks(ctx, x, plot))
    }
    const notes: string[] = []
    if (duplicates) notes.push(`${duplicates} duplicate task ids drawn as separate tasks; dependencies use the first`)
    if (o.arrows && !arrows && depCount) notes.push(`${depCount} dependency arrows hidden: rows are too dense`)
    if (today && !marks.some((m) => m.key === 'today-line')) notes.push(`today (${fmt.date(today, 'day')}) is outside the schedule`)
    return { marks, plot, clip: plot, invertX: f.invertX, note: notes.length ? notes.join('; ') : undefined }
  },
  describe({ data, fmt, name }) {
    const { tasks } = tasksOf(data, fmt.timeZone)
    const groups = new Set(tasks.map((t) => t.group).filter((g) => g !== undefined)).size
    const ms = tasks.filter((t) => t.milestone).length
    const deps = tasks.reduce((n, t) => n + t.deps.length, 0)
    const weighed = tasks.filter((t) => !t.milestone && t.progress !== null)
    const total = weighed.reduce((n, t) => n + days(t), 0)
    const done = total > 0 ? weighed.reduce((n, t) => n + days(t) * (t.progress ?? 0), 0) / total : null
    const parts = [
      `${tasks.length > ms ? phrase.count(tasks.length - ms, 'task') : phrase.count(ms, 'milestone')}${groups ? ` in ${phrase.count(groups, 'group')}` : ''}`,
      phrase.dateSpan(tasks.flatMap((t) => [t.start, t.end]), fmt),
    ]
    if (ms && tasks.length > ms) parts.push(phrase.count(ms, 'milestone'))
    if (deps) parts.push(phrase.count(deps, 'dependency', 'dependencies'))
    if (done !== null) parts.push(`${fmt.pct(done)} complete`)
    return `${name}, ${parts.join(', ')}`
  },
  table(data, o, fmt) {
    const { tasks } = tasksOf(data, fmt.timeZone)
    return {
      columns: ['ID', 'Task', 'Group', 'Start', 'End', 'Duration (days)', 'Progress', 'Depends on', 'Milestone'],
      rows: tasks.map((t) => [t.id, t.name, t.group ?? null, t.start.toISOString(), t.end.toISOString(), Math.round(days(t) * 1000) / 1000, t.progress, t.deps.join(', ') || null, t.milestone ? 'yes' : 'no']),
    }
  },
})

export default gantt
