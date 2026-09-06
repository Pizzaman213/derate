// Deterministic graph layout.
//
// No force simulation. At two to four machines a physics layout produces
// drifting, unrepeatable positions and reads as a toy; worse, a machine moves
// between refreshes, so you cannot learn where anything is. Positions here are a
// pure function of the sorted node ids: the same cluster draws identically every
// time, and adding a node moves things predictably.

export const BOX_W = 184
export const BOX_H = 112
export const GAP_X = 116
export const MARGIN = 28
/** Space above the row for arcs between non-adjacent machines. */
export const ARC_HEADROOM = 72
export const BAND_H = 40
export const BAND_GAP = 10

export interface Placed {
  node_id: string
  x: number
  y: number
  /** Position on the row or around the ring. Used to decide edge routing. */
  index: number
}

export interface Layout {
  kind: 'row' | 'ring'
  nodes: Placed[]
  width: number
  /** Height of the machine area, before any deployment bands. */
  headerHeight: number
}

/** Row up to four machines, ring beyond. Both are indexed by sorted node id. */
export function layoutNodes(nodeIds: string[]): Layout {
  const ids = [...nodeIds].sort()
  const n = ids.length

  if (n === 0) {
    return { kind: 'row', nodes: [], width: 320, headerHeight: ARC_HEADROOM + BOX_H }
  }

  if (n <= 4) {
    const width = MARGIN * 2 + n * BOX_W + (n - 1) * GAP_X
    return {
      kind: 'row',
      width,
      headerHeight: ARC_HEADROOM + BOX_H + MARGIN,
      nodes: ids.map((node_id, index) => ({
        node_id,
        index,
        x: MARGIN + index * (BOX_W + GAP_X),
        y: ARC_HEADROOM,
      })),
    }
  }

  // Ring, first node at twelve o'clock, clockwise by sorted index.
  const radius = Math.max(150, (n * (BOX_W + 40)) / (2 * Math.PI))
  const width = Math.ceil(2 * (radius + BOX_W / 2 + MARGIN))
  const height = Math.ceil(2 * (radius + BOX_H / 2 + MARGIN))
  const cx = width / 2
  const cy = height / 2
  return {
    kind: 'ring',
    width,
    headerHeight: height,
    nodes: ids.map((node_id, index) => {
      const angle = -Math.PI / 2 + (index / n) * 2 * Math.PI
      return {
        node_id,
        index,
        x: cx + radius * Math.cos(angle) - BOX_W / 2,
        y: cy + radius * Math.sin(angle) - BOX_H / 2,
      }
    }),
  }
}

export const centerOf = (p: Placed) => ({ x: p.x + BOX_W / 2, y: p.y + BOX_H / 2 })

/** Edge geometry. Adjacent machines on a row get a straight segment between the
 *  facing edges of their boxes; anything else arcs over the top so it never
 *  runs underneath a machine. */
export function edgeGeometry(a: Placed, b: Placed, kind: 'row' | 'ring') {
  const [l, r] = a.x <= b.x ? [a, b] : [b, a]
  const adjacent = Math.abs(a.index - b.index) === 1

  if (kind === 'ring') {
    const c1 = centerOf(l)
    const c2 = centerOf(r)
    return {
      d: `M${c1.x} ${c1.y} L${c2.x} ${c2.y}`,
      mid: { x: (c1.x + c2.x) / 2, y: (c1.y + c2.y) / 2 },
    }
  }

  if (adjacent) {
    const y = l.y + BOX_H / 2
    const x1 = l.x + BOX_W
    const x2 = r.x
    return {
      d: `M${x1} ${y} L${x2} ${y}`,
      mid: { x: (x1 + x2) / 2, y },
    }
  }

  // Non-adjacent machines arc over the top of everything between them, leaving
  // from the top edge of each box rather than its centre. Routing through the
  // row would draw a link straight across a machine it does not touch.
  const ax = l.x + BOX_W / 2
  const ay = l.y
  const bx = r.x + BOX_W / 2
  const by = r.y
  const span = Math.abs(bx - ax)
  const lift = Math.min(ARC_HEADROOM * 1.25, 34 + span * 0.075)
  const mx = (ax + bx) / 2
  const cy = Math.min(ay, by) - lift
  return {
    d: `M${ax} ${ay} Q${mx} ${cy} ${bx} ${by}`,
    // Apex of the quadratic, so the label sits on the curve.
    mid: { x: mx, y: (ay + 2 * cy + by) / 4 },
  }
}

/** Thickness carries the measurement, scaled against the 40 GB/s tensor-parallel
 *  threshold. A link drawn at full weight is a link where TP is viable. */
export function edgeWidth(gbps: number): number {
  const TP_VIABLE_THRESHOLD = 40
  return 1.5 + 6 * Math.min(1, gbps / TP_VIABLE_THRESHOLD)
}
