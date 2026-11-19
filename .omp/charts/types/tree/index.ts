// Tidy tree (Reingold–Tilford via d3.tree): one node per tree node, curved links from
// parent to child, depth along x (horizontal) or y (vertical). Labels are truncated to
// the room they have and dropped where they would collide; the tooltip keeps them all.

import { tree as d3tree } from 'd3-hierarchy'
import { defineChart, phrase, SIZES } from '../../core'
import { notes } from '../../core/helpers'
import type { Mark } from '../../core'
import { adaptTree, branchColor, buildTree, displayName, fittedText, nodeLabel, nodeTooltip, Placer, treeFacts, treeTable, type TNode } from '../_shared/FlowB'
import { input, options, type TreeInput, type TreeOptions } from './schema'

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v))

export const tree = defineChart<TreeInput, TreeOptions>({
  id: 'tree',
  name: 'Tree',
  category: 'flow-hierarchy',
  intents: ['hierarchy', 'network'],
  summary: 'A tidy node-link tree: every node placed by depth, children fanned out under their parent.',
  input,
  options,
  shape: 'object',
  fields: ['id', 'parent', 'name', 'value', 'path'],
  numberFields: ['value'],
  adapt: (raw, o) => adaptTree(raw, o),
  isEmpty: (d) => d === null,
  aspect: 0.62,
  minHeight: 240,
  maxHeight: 720,
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    if (!ctx.data) return { marks: [] }
    const W = ctx.width, H = ctx.height
    const b = buildTree(ctx.data, { maxDepth: o.maxDepth })
    const root = b.root
    const nodes = root.descendants()
    const horizontal = o.orientation === 'horizontal'
    const fs = SIZES.tick, lh = fs + 4
    let maxD = 0
    for (const n of nodes) if (n.depth > maxD) maxD = n.depth
    const textOf = (n: TNode) => {
      const hidden = b.hiddenBelow.get(n) ?? 0
      return `${displayName(n)}${hidden ? ` (+${fmt.integer(hidden)})` : ''}`
    }

    // unit layout first: breadth in [0,1], depth in [0,1]; margins then depend on labels
    d3tree<typeof root.data>().size([1, 1])(root)
    const byDepth: TNode[][] = []
    for (const n of nodes) (byDepth[n.depth] ??= []).push(n)
    for (const row of byDepth) row.sort((a, b2) => (a.x ?? 0) - (b2.x ?? 0))

    const leafCap = Math.min((horizontal ? W : H) * 0.3, 180)
    let deepest = 0
    for (const n of byDepth[maxD] ?? []) deepest = Math.max(deepest, ctx.measure(textOf(n), fs))
    deepest = Math.min(deepest, leafCap)

    let x0: number, x1: number, y0: number, y1: number
    let rotateLeaves = false
    if (horizontal) {
      const padY = lh / 2 + 4
      const rootW = Math.min(ctx.measure(textOf(root), fs), W * 0.22)
      if (maxD === 0) {
        x0 = x1 = Math.max(8, (W - rootW) / 2 - 6)
      } else {
        x0 = root.children ? rootW + 14 : 10
        x1 = W - deepest - 14
      }
      y0 = padY
      y1 = H - padY
    } else {
      // leaves fan across x: their labels go below, rotated when the spacing is tight
      const deepRow = byDepth[maxD] ?? []
      const span = W - 20
      const gaps: number[] = []
      for (let i = 1; i < deepRow.length; i++) gaps.push(((deepRow[i]!.x ?? 0) - (deepRow[i - 1]!.x ?? 0)) * span)
      gaps.sort((a, c) => a - c)
      const typicalGap = gaps.length ? gaps[Math.floor(gaps.length / 2)]! : span
      const medianW = (() => {
        const ws = deepRow.map((n) => ctx.measure(textOf(n), fs)).sort((a, c) => a - c)
        return ws[Math.floor(ws.length / 2)] ?? 0
      })()
      rotateLeaves = maxD > 0 && medianW > typicalGap - 6 && typicalGap >= lh * 0.9
      x0 = 10
      x1 = W - 10
      y0 = lh + 10
      y1 = maxD === 0 ? y0 : H - (rotateLeaves ? Math.min(H * 0.35, deepest + 12) : lh + 10)
    }
    const pos = new Map<TNode, [number, number]>()
    for (const n of nodes) {
      const u = n.x ?? 0, v = n.y ?? 0
      pos.set(n, horizontal ? [x0 + v * (x1 - x0), y0 + u * (y1 - y0)] : [x0 + u * (x1 - x0), y0 + v * (y1 - y0)])
    }
    const P = (n: TNode) => pos.get(n)!
    const breadthOf = (n: TNode) => (horizontal ? P(n)[1] : P(n)[0])
    const depthStep = maxD > 0 ? Math.abs((horizontal ? x1 - x0 : y1 - y0) / maxD) : horizontal ? W : H

    // neighbours along the breadth, per depth: node size and label room
    const prevGap = new Map<TNode, number>(), nextGap = new Map<TNode, number>()
    let minGap = Infinity
    for (const row of byDepth) {
      for (let i = 0; i < row.length; i++) {
        const n = row[i]!
        const pg = i > 0 ? breadthOf(n) - breadthOf(row[i - 1]!) : Infinity
        const ng = i < row.length - 1 ? breadthOf(row[i + 1]!) - breadthOf(n) : Infinity
        prevGap.set(n, pg)
        nextGap.set(n, ng)
        minGap = Math.min(minGap, pg, ng)
      }
    }
    const r = clamp(Number.isFinite(minGap) ? minGap * 0.36 : 4.5, 1.5, 4.5)
    const large = nodes.length > 2000

    const marks: Mark[] = []
    // links
    for (const n of nodes) {
      if (!n.parent) continue
      const [sx, sy] = P(n.parent), [tx, ty] = P(n)
      const d = horizontal
        ? `M${sx},${sy}C${(sx + tx) / 2},${sy} ${(sx + tx) / 2},${ty} ${tx},${ty}`
        : `M${sx},${sy}C${sx},${(sy + ty) / 2} ${tx},${(sy + ty) / 2} ${tx},${ty}`
      const c = o.colorBy === 'branch' ? branchColor(theme, b, n, 'branch') : theme.axis
      marks.push({ type: 'path', layer: 'back', key: `l:${n.id}`, d, stroke: c, strokeOpacity: o.colorBy === 'branch' ? 0.55 : 0.75, strokeWidth: large ? 0.6 : 1.25, fill: 'none', inert: true })
    }
    // nodes
    for (const n of nodes) {
      const [cx, cy] = P(n)
      const color = branchColor(theme, b, n, o.colorBy)
      const hidden = b.hiddenBelow.get(n) ?? 0
      const style: Partial<Extract<Mark, { type: 'circle' }>> = hidden
        ? { r: r + 1.5, fill: theme.mix(theme.surface, color, 0.35), stroke: color, strokeWidth: 2 }
        : n.children
          ? { r, fill: color, stroke: theme.surface, strokeWidth: large ? 0 : 1 }
          : { r, fill: theme.surface, stroke: color, strokeWidth: large ? 1 : 1.5 }
      marks.push({ type: 'circle', role: 'node', key: `n:${n.id}`, cx, cy, r, ...style, datum: n.data, label: nodeLabel(n, fmt, hidden), tooltip: nodeTooltip(n, fmt, color, hidden) })
    }

    // labels: shallow first, each truncated to its room, dropped on collision
    const placer = new Placer()
    const minText = ctx.measure('Ab…', fs)
    const order = [...nodes].sort((a, c) => a.depth - c.depth)
    for (const n of order) {
      const [cx, cy] = P(n)
      const full = textOf(n)
      const rr = (b.hiddenBelow.has(n) ? r + 1.5 : r) + 4
      const expanded = !!n.children
      const base = { type: 'text' as const, layer: 'labels' as const, key: `t:${n.id}`, fill: n.depth === 0 ? theme.ink : theme.inkSecondary, size: fs, weight: n.depth === 0 || expanded ? 500 : 400, halo: true }
      if (horizontal) {
        const left = expanded || (n.depth === 0 && maxD > 0)
        const room = Math.min(left ? cx - rr - 4 : W - cx - rr - 4, n.depth === 0 || (!left && n.depth === maxD) ? Infinity : depthStep - 2 * rr - 6)
        const shown = ctx.truncate(full, Math.min(room, 240), fs)
        if (!shown || ctx.measure(shown, fs) < Math.min(minText, ctx.measure(full, fs))) continue
        const w = ctx.measure(shown, fs)
        const x = left ? cx - rr : cx + rr
        const box = { x0: left ? x - w : x, x1: left ? x : x + w, y0: cy - lh / 2, y1: cy + lh / 2 }
        if (box.y0 < 0 || box.y1 > H || !placer.place(box)) continue
        marks.push(fittedText({ ...base, x, y: cy, anchor: left ? 'end' : 'start', baseline: 'central' }, full, shown))
      } else if (n.depth === 0 || (expanded && n.depth > 0)) {
        // root: centred above; expanded inner nodes: to the right
        if (n.depth === 0) {
          const shown = ctx.truncate(full, W - 8, fs)
          const w = ctx.measure(shown, fs)
          const lx = clamp(cx - w / 2, 4, W - 4 - w)
          const box = { x0: lx, x1: lx + w, y0: cy - rr - lh, y1: cy - rr }
          if (!placer.place(box)) continue
          marks.push(fittedText({ ...base, x: lx, y: cy - rr - 2, anchor: 'start', baseline: 'alphabetic' }, full, shown))
        } else {
          const room = Math.min(240, (nextGap.get(n) ?? Infinity) - 2 * rr - 6, W - 4 - (cx + rr))
          const shown = ctx.truncate(full, room, fs)
          if (!shown || ctx.measure(shown, fs) < Math.min(minText, ctx.measure(full, fs))) continue
          const w = ctx.measure(shown, fs)
          const box = { x0: cx + rr, x1: cx + rr + w, y0: cy - lh / 2, y1: cy + lh / 2 }
          if (!placer.place(box)) continue
          marks.push(fittedText({ ...base, x: cx + rr, y: cy, anchor: 'start', baseline: 'central' }, full, shown))
        }
      } else if (rotateLeaves) {
        const room = n.depth === maxD ? H - (cy + rr) - 4 : depthStep - 2 * rr - 6
        const shown = ctx.truncate(full, room, fs)
        if (!shown || ctx.measure(shown, fs) < Math.min(minText, ctx.measure(full, fs))) continue
        const w = ctx.measure(shown, fs)
        const box = { x0: cx - lh / 2, x1: cx + lh / 2, y0: cy + rr, y1: cy + rr + w }
        if (box.x0 < 0 || box.x1 > W || !placer.place(box)) continue
        marks.push(fittedText({ ...base, x: cx, y: cy + rr, anchor: 'start', baseline: 'central', rotate: 90 }, full, shown))
      } else {
        const g = Math.min(prevGap.get(n) ?? Infinity, nextGap.get(n) ?? Infinity)
        const room = Math.min(200, g - 6, W - 8)
        const shown = ctx.truncate(full, room, fs)
        if (!shown || ctx.measure(shown, fs) < Math.min(minText, ctx.measure(full, fs))) continue
        const w = ctx.measure(shown, fs)
        const lx = clamp(cx - w / 2, 4, W - 4 - w)
        const box = { x0: lx, x1: lx + w, y0: cy + rr, y1: cy + rr + lh }
        if (box.y1 > H || !placer.place(box)) continue
        marks.push(fittedText({ ...base, x: lx, y: cy + rr + 1, anchor: 'start', baseline: 'hanging' }, full, shown))
      }
    }
    return {
      marks,
      note: notes([
        [b.duplicates, 'duplicate sibling names kept as separate nodes'],
        [b.collapsed, `nodes below depth ${o.maxDepth ?? 0} collapsed`],
      ]),
    }
  },
  describe({ data, name }) {
    if (!data) return `${name}, no nodes`
    const f = treeFacts(data)
    return `${name}, ${phrase.count(f.nodes, 'node')} over ${phrase.count(f.levels, 'level')}, root ${f.root}${f.top.length ? ` with ${phrase.count(f.top.length, 'branch', 'branches')} (${phrase.listPhrase(f.top)})` : ''}, ${phrase.count(f.leaves, 'leaf', 'leaves')}`
  },
  table: (data) => treeTable(data),
})

export default tree
