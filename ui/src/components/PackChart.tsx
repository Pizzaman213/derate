import { useEffect, useRef, useState } from 'react'
import type { ChartHandle } from '../../../.omp/charts/core/types'
import { Verbatim } from './Verbatim'

/** The chart-pack types this app mounts. Adding one is a `LOADERS` entry and
 *  a member here; the pack itself is `.omp/charts/` (RULES.md: no one-off
 *  charts in app code). */
export type PackChartType = PackBarType | 'multi-line' | 'pie' | 'donut'
/** The category types, which take `PackRow`s. */
export type PackBarType = 'stacked-bar' | 'stacked-bar-100' | 'grouped-bar'

/** The long-row input of the category types. A null value is left out and
 *  noted by the pack, never drawn as 0. */
export type PackRow = { label: string; series: string; value: number | null }
/** The long-row input of `multi-line`: `x` a date (`YYYY-MM-DD`) or number, one
 *  line per `series`; a null `y` lifts the pen. */
export type PackTrendRow = { x: string | number; series: string; y: number | null }
export type PackPieRow = { label: string; value: number | null }

export type PackChartProps = {
  /** The pack's visible title and the start of its aria-label. */
  title: string
  /** px, the pack's `height`. */
  height: number
  /** Type options: orientation, showValues, showTotals, valueFormat, emptyMessage, … */
  options?: Record<string, unknown>
  /** A click on a datum. Held in a ref: a new closure never redraws. */
  onClick?: (datum: Record<string, unknown>, index: number) => void
  /** Hover over a datum, null on leave. Held in a ref like `onClick`. */
  onHover?: (datum: Record<string, unknown> | null, index: number) => void
} & ({ type: PackBarType; data: readonly PackRow[] } | { type: 'multi-line'; data: readonly PackTrendRow[] } | { type: 'pie' | 'donut'; data: readonly PackPieRow[] })

// The only place the pack is imported. Dynamic, so d3 and zod land in their
// own chunk and the main bundle pays nothing until a chart is on screen.
// Each type module registers itself on import (`defineChart`).
const LOADERS: Record<PackChartType, () => Promise<unknown>> = {
  'stacked-bar': () => import('../../../.omp/charts/types/stacked-bar/index'),
  'stacked-bar-100': () => import('../../../.omp/charts/types/stacked-bar-100/index'),
  'grouped-bar': () => import('../../../.omp/charts/types/grouped-bar/index'),
  'multi-line': () => import('../../../.omp/charts/types/multi-line/index'),
  pie: () => import('../../../.omp/charts/types/pie/index'),
  donut: () => import('../../../.omp/charts/types/donut/index'),
}

/** Mounts one chart-pack type by id. Paint, keyboard, legend and the hidden
 *  table are all the pack's; this owns only the lifecycle. */
export function PackChart({ type, title, data, height, options, onClick, onHover }: PackChartProps) {
  const host = useRef<HTMLDivElement>(null)
  const handle = useRef<ChartHandle | null>(null)
  const cb = useRef({ onClick, onHover })
  cb.current = { onClick, onHover }
  // Stable wrappers, so the callbacks never enter `key` nor force a redraw.
  const wrappers = useRef({
    onClick: (d: unknown, i: number) => cb.current.onClick?.(d as Record<string, unknown>, i),
    onHover: (d: unknown, i: number) => cb.current.onHover?.(d as Record<string, unknown> | null, i),
  })
  const latest = useRef({ data, options: { ...options, title, height, ...wrappers.current } })
  latest.current = { data, options: { ...options, title, height, ...wrappers.current } }
  const [ready, setReady] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    setFailed(null)
    Promise.all([LOADERS[type](), import('../../../.omp/charts/core/registry')])
      .then(([, registry]) => {
        if (!live || !host.current) return
        const mod = registry.getModule(type)
        if (!mod) {
          setFailed(`chart type '${type}' did not register`)
          return
        }
        handle.current = mod.render(host.current, latest.current.data, latest.current.options)
        setReady(true)
      })
      .catch((err: unknown) => {
        if (live) setFailed(err instanceof Error ? err.message : String(err))
      })
    return () => {
      live = false
      handle.current?.destroy()
      handle.current = null
      setReady(false)
    }
  }, [type])

  // Keyed on content, so a poll that returns identical rows redraws nothing.
  const key = JSON.stringify([data, options, title, height])
  useEffect(() => {
    handle.current?.update(latest.current.data, latest.current.options)
  }, [key])

  if (failed) return <Verbatim text={failed} />
  return <div ref={host} style={{ minHeight: height }} aria-busy={ready ? undefined : true} />
}
