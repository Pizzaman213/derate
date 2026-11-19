// Org chart: one box per person (name, title) under their manager, joined by elbow
// connectors. It always fits the width: when a row of reports is too wide it wraps into
// several rows hanging off a side trunk, and the box width and an overall scale are
// chosen together so boxes stay as large as the space allows. Never a scrollbar.

import { defineChart, phrase, SIZES } from '../../core'
import { notes } from '../../core/helpers'
import type { Mark } from '../../core'
import { adaptTree, buildTree, displayName, nodeLabel, nodeTooltip, treeFacts, treeTable, type TNode } from '../_shared/FlowB'
import { input, options, type OrgChartInput, type OrgChartOptions } from './schema'

/** Unscaled spacing (px at scale 1). */
const GAP_X = 10, GAP_Y = 26, TRUNK = 14, PAD_X = 8, PAD_Y = 7, LINE_GAP = 3, MARGIN = 4
const MIN_TEXT_PX = 7.5

export interface Block {
  node: TNode
  w: number
  h: number
  rows: Block[][]
  rowW: number[]
  rowH: number[]
  /** Reports wrapped into several rows on a side trunk. */
  wrapped: boolean
}

/**
 * Lay out a subtree within `avail` px: reports in one row when they fit, else wrapped
 * greedily into rows (all-leaf rows balanced) beside a trunk. Memoised per (node, avail).
 */
export function blockOf(n: TNode, avail: number, bw: number, bh: number, memo: Map<TNode, Map<number, Block>>): Block {
  const hit = memo.get(n)?.get(avail)
  if (hit) return hit
  let out: Block
  if (!n.children) out = { node: n, w: bw, h: bh, rows: [], rowW: [], rowH: [], wrapped: false }
  else {
    let kids = n.children.map((c) => blockOf(c, avail, bw, bh, memo))
    const one = kids.reduce((s, k) => s + k.w, 0) + GAP_X * (kids.length - 1)
    let rows: Block[][]
    let wrapped = false
    if (one <= avail || kids.length === 1) rows = [kids]
    else {
      wrapped = true
      const limit = avail - TRUNK
      kids = n.children.map((c) => blockOf(c, limit, bw, bh, memo))
      rows = []
      let cur: Block[] = [], w = 0
      for (const k of kids) {
        const add = cur.length ? GAP_X + k.w : k.w
        if (cur.length && w + add > limit) {
          rows.push(cur)
          cur = [k]
          w = k.w
        } else {
          cur.push(k)
          w += add
        }
      }
      if (cur.length) rows.push(cur)
      if (kids.every((k) => !k.node.children) && rows.length > 1) {
        const per = Math.ceil(kids.length / rows.length)
        rows = []
        for (let i = 0; i < kids.length; i += per) rows.push(kids.slice(i, i + per))
      }
      if (rows.length === 1) wrapped = false
    }
    const rowW = rows.map((r) => r.reduce((s, k) => s + k.w, 0) + GAP_X * (r.length - 1))
    const rowH = rows.map((r) => Math.max(...r.map((k) => k.h)))
    const content = Math.max(...rowW) + (wrapped ? TRUNK : 0)
    out = { node: n, w: Math.max(bw, content), h: bh + rowH.reduce((s, h) => s + GAP_Y + h, 0), rows, rowW, rowH, wrapped }
  }
  let m = memo.get(n)
  if (!m) memo.set(n, (m = new Map()))
  m.set(avail, out)
  return out
}

/** True when any team in the block was wrapped into several rows. */
export function wrapsAnywhere(b: Block): boolean {
  return b.wrapped || b.rows.some((r) => r.some(wrapsAnywhere))
}

interface Placed {
  boxes: Map<TNode, { x: number; y: number }>
  connectors: Array<{ key: string; d: string }>
}

/** Absolute (unscaled) box positions and connector paths for a block placed at (x, y). */
function place(root: Block, bw: number, bh: number): Placed {
  const boxes = new Map<TNode, { x: number; y: number }>()
  const connectors: Placed['connectors'] = []
  const visit = (b: Block, x: number, y: number) => {
    const bx = x + (b.w - bw) / 2
    boxes.set(b.node, { x: bx, y })
    if (!b.rows.length) return
    const cx = bx + bw / 2
    const left = b.wrapped ? x + TRUNK : x
    const trunkX = x + TRUNK / 2
    let ry = y + bh + GAP_Y
    const segs: string[] = []
    b.rows.forEach((row, i) => {
      const busY = ry - GAP_Y / 2
      let kx = left + (x + b.w - left - b.rowW[i]!) / 2
      const centres: number[] = []
      for (const k of row) {
        visit(k, kx, ry)
        const kc = kx + k.w / 2
        centres.push(kc)
        segs.push(`M${kc},${busY}V${ry}`)
        kx += k.w + GAP_X
      }
      const from = b.wrapped ? trunkX : Math.min(cx, centres[0]!)
      // the first bus (and an unwrapped one) must also reach the manager's drop line
      const to = Math.max(...centres, !b.wrapped || i === 0 ? cx : -Infinity)
      segs.push(`M${from},${busY}H${to}`)
      if (i === 0) segs.push(`M${cx},${y + bh}V${busY}`)
      if (b.wrapped && i === b.rows.length - 1) segs.push(`M${trunkX},${y + bh + GAP_Y / 2}V${busY}`)
      ry += b.rowH[i]! + GAP_Y
    })
    connectors.push({ key: `c:${b.node.id}`, d: segs.join('') })
  }
  visit(root, 0, 0)
  return { boxes, connectors }
}

export interface OrgLayout {
  bw: number
  bh: number
  scale: number
  block: Block
  placed: Placed
}

/** Pick the box width and wrap width that keep boxes largest once scaled to fit W x H. */
export function layoutOrg(root: TNode, W: number, H: number, natural: number, bh: number): OrgLayout {
  const availW = Math.max(40, W - 2 * MARGIN), availH = Math.max(40, H - 2 * MARGIN)
  const widths: number[] = []
  for (let w = natural; w >= 56; w *= 0.85) widths.push(Math.round(w))
  if (!widths.length) widths.push(56)
  let best: { bw: number; block: Block; s: number; score: number } | null = null
  for (const bw of widths) {
    const memo = new Map<TNode, Map<number, Block>>()
    for (const k of [1, 1.5, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48]) {
      const block = blockOf(root, availW * k, bw, bh, memo)
      const s = Math.min(1, availW / block.w, availH / block.h)
      const score = bw * s * s
      if (!best || score > best.score + 1e-9) best = { bw, block, s, score }
      // nothing wrapped: a wider allowance cannot change the layout
      if (!wrapsAnywhere(block)) break
    }
  }
  const b = best!
  return { bw: b.bw, bh, scale: b.s, block: b.block, placed: place(b.block, b.bw, bh) }
}

export const orgChart = defineChart<OrgChartInput, OrgChartOptions>({
  id: 'org-chart',
  name: 'Org chart',
  category: 'flow-hierarchy',
  intents: ['hierarchy', 'network'],
  summary: 'Boxes for people under their managers, top-down, with elbow connectors.',
  input,
  options,
  shape: 'object',
  fields: ['id', 'parent', 'name', 'title'],
  adapt: (raw, o) => adaptTree(raw, o),
  isEmpty: (d) => d === null,
  aspect: 0.62,
  minHeight: 280,
  maxHeight: 760,
  draw(ctx) {
    const { theme, options: o, fmt } = ctx
    if (!ctx.data) return { marks: [] }
    const W = ctx.width, H = ctx.height
    const b = buildTree(ctx.data)
    const root = b.root
    const nodes = root.descendants()
    const fsName = SIZES.label, fsTitle = SIZES.tick
    const titled = nodes.some((n) => n.data.title)
    const bh = PAD_Y * 2 + fsName + (titled ? LINE_GAP + fsTitle : 0)
    let widest = 0
    for (const n of nodes) widest = Math.max(widest, ctx.measure(displayName(n), fsName, 600), n.data.title ? ctx.measure(n.data.title, fsTitle) : 0)
    const natural = Math.max(72, Math.min(200, Math.ceil(widest + 2 * PAD_X)))
    const L = layoutOrg(root, W, H, natural, bh)
    const s = L.scale
    const ox = (W - L.block.w * s) / 2
    const oy = MARGIN + Math.max(0, Math.min(16, (H - 2 * MARGIN - L.block.h * s) / 2))
    const X = (v: number) => ox + v * s
    const Y = (v: number) => oy + v * s

    const marks: Mark[] = []
    for (const c of L.placed.connectors) {
      // scale the unscaled path: every coordinate pair is absolute, M/V/H only
      const d = c.d.replace(/([MVH])([-\d.e]+)(?:,([-\d.e]+))?/g, (_m, cmd: string, a: string, bb: string | undefined) =>
        cmd === 'M' ? `M${X(+a)},${Y(+(bb ?? 0))}` : cmd === 'V' ? `V${Y(+a)}` : `H${X(+a)}`)
      marks.push({ type: 'path', layer: 'back', key: c.key, d, stroke: theme.axis, strokeWidth: 1, fill: 'none', inert: true })
    }
    const showName = fsName * s >= MIN_TEXT_PX
    const showTitle = titled && fsTitle * s >= MIN_TEXT_PX
    const inner = L.bw - 2 * PAD_X
    for (const n of nodes) {
      const p = L.placed.boxes.get(n)!
      const i = b.branch.get(n) ?? -1
      // branch hues only mean something when the top-level reports run organisations of their own
      const color = o.colorBy === 'single' || i < 0 || !b.top.some((t) => t.children) ? (n.parent ? theme.cat(0) : theme.inkSecondary) : theme.cat(i)
      const x = X(p.x), y = Y(p.y), w = L.bw * s, h = bh * s
      const reports = n.children?.length ?? 0
      marks.push({
        type: 'rect', role: 'node', key: `n:${n.id}`, x, y, width: w, height: h, rx: Math.min(4, 4 * s),
        fill: theme.mix(theme.surface, color, n.parent ? 0.1 : 0.18), stroke: theme.mix(theme.surface, color, 0.7), strokeWidth: n.parent ? 1 : 1.5,
        datum: n.data, label: nodeLabel(n, fmt), tooltip: nodeTooltip(n, fmt, color, 0, n.data.value, reports ? [['Team size', fmt.integer(n.descendants().length - 1)]] : []),
      })
      if (!showName) continue
      const name = displayName(n)
      const nameShown = ctx.truncate(name, inner, fsName)
      const ny = titled && showTitle ? Y(p.y + PAD_Y + fsName / 2) : y + h / 2
      if (nameShown) {
        const t: Mark = { type: 'text', layer: 'labels', key: `t:${n.id}`, x: x + w / 2, y: ny, text: nameShown, anchor: 'middle', baseline: 'central', fill: theme.ink, size: fsName * s, weight: 600 }
        marks.push(nameShown === name ? t : { ...t, full: name })
      }
      if (showTitle && n.data.title) {
        const title = n.data.title
        const shown = ctx.truncate(title, inner, fsTitle)
        if (shown) {
          const t: Mark = { type: 'text', layer: 'labels', key: `s:${n.id}`, x: x + w / 2, y: Y(p.y + PAD_Y + fsName + LINE_GAP + fsTitle / 2), text: shown, anchor: 'middle', baseline: 'central', fill: theme.muted, size: fsTitle * s }
          marks.push(shown === title ? t : { ...t, full: title })
        }
      }
    }
    return {
      marks,
      note: notes([
        [b.duplicates, 'duplicate names under one manager kept as separate boxes'],
        [showName ? 0 : nodes.length, 'boxes too small for text (hover for names)'],
      ]),
      plot: { x: ox, y: oy, width: L.block.w * s, height: L.block.h * s },
    }
  },
  describe({ data, name }) {
    if (!data) return `${name}, no people`
    const f = treeFacts(data)
    const { root } = buildTree(data)
    let widest = root
    for (const n of root.descendants()) if ((n.children?.length ?? 0) > (widest.children?.length ?? 0)) widest = n
    const span = widest.children?.length ? `, largest team ${displayName(widest)} with ${phrase.count(widest.children.length, 'direct report')}` : ''
    return `${name}, ${phrase.count(f.nodes, 'person', 'people')} in ${phrase.count(f.levels, 'level')} under ${displayName(root)}${root.data.title ? ` (${root.data.title})` : ''}${span}`
  },
  table: (data) => treeTable(data, { title: true }),
})

export default orgChart
