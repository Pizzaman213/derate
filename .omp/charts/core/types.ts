// The contract every chart is written against. A chart is a pure function from
// (validated data, options, size, theme) to a list of marks; core/ owns everything
// else -- the DOM, scales/axes layout helpers, legend, tooltip, keyboard, the hidden
// table, export, transitions, canvas fallback -- so every chart looks and behaves alike.

import type { z } from 'zod'
import type { Formatters } from './format'
import type { Palette } from './theme'
import type { CartesianFrame, CartesianSpec } from './cartesian'

export type Category =
  | 'comparison'
  | 'part-to-whole'
  | 'trend'
  | 'timeline'
  | 'distribution'
  | 'relationship'
  | 'flow-hierarchy'
  | 'matrix'
  | 'geo'
  | 'kpi'
  | 'sets'

export const CATEGORIES: Category[] = [
  'comparison', 'part-to-whole', 'trend', 'timeline', 'distribution', 'relationship',
  'flow-hierarchy', 'matrix', 'geo', 'kpi', 'sets',
]

export type Intent =
  | 'compare'
  | 'rank'
  | 'composition'
  | 'trend'
  | 'timeline'
  | 'schedule'
  | 'distribution'
  | 'relationship'
  | 'correlation'
  | 'flow'
  | 'hierarchy'
  | 'network'
  | 'geo'
  | 'kpi'
  | 'change-over-time'
  | 'deviation'
  | 'sets'

export const INTENTS: Intent[] = [
  'compare', 'rank', 'composition', 'trend', 'timeline', 'schedule', 'distribution', 'relationship',
  'correlation', 'flow', 'hierarchy', 'network', 'geo', 'kpi', 'change-over-time', 'deviation', 'sets',
]
/** Spellings the API accepts for an intent. */
export const INTENT_ALIASES: Record<string, Intent> = {
  comparison: 'compare', ranking: 'rank', 'part-to-whole': 'composition', 'project': 'schedule',
  'schedule/project': 'schedule', progress: 'kpi', 'progress/kpi': 'kpi', time: 'trend', 'over-time': 'change-over-time',
  correlations: 'correlation', relationships: 'relationship', map: 'geo', tree: 'hierarchy', graph: 'network', set: 'sets',
}

export type Paint = string
export type Layer = 'back' | 'grid' | 'marks' | 'labels' | 'axes' | 'front'
export const LAYERS: Layer[] = ['back', 'grid', 'marks', 'labels', 'axes', 'front']

export interface TooltipContent {
  title?: string
  rows?: Array<[string, string]>
  color?: Paint
}

interface MarkBase {
  /** Stable identity across updates; drives enter/update/exit transitions. */
  key?: string
  layer?: Layer
  /** Kind of mark, exposed as data-role and counted by tests ("bar", "arc", "point", "task"). */
  role?: string
  /** Present => a data mark: counted, focusable with the keyboard, hoverable, clickable. */
  datum?: unknown
  /** Accessible label; announced on focus and used as the default tooltip. */
  label?: string
  tooltip?: TooltipContent
  /** Legend key: hover highlight and legend toggling act on it. */
  series?: string
  /** Hovering this mark dims marks with a different hoverKey (default: by series). */
  hoverKey?: string
  fill?: Paint
  stroke?: Paint
  strokeWidth?: number
  opacity?: number
  fillOpacity?: number
  strokeOpacity?: number
  dash?: string
  linecap?: 'butt' | 'round' | 'square'
  linejoin?: 'miter' | 'round' | 'bevel'
  /** Pattern index (0-7) used when options.patterns is on. */
  pattern?: number
  transform?: string
  className?: string
  /** Pointer events off (decorations under data marks). */
  inert?: boolean
}
export interface RectMark extends MarkBase { type: 'rect'; x: number; y: number; width: number; height: number; rx?: number }
export interface CircleMark extends MarkBase { type: 'circle'; cx: number; cy: number; r: number }
export interface PathMark extends MarkBase {
  type: 'path'
  d: string
  /** Anchor for tooltip placement, canvas hit-testing and the focus ring. */
  cx?: number
  cy?: number
  /** Focus-ring box; defaults to a box around (cx, cy). */
  box?: Rect
}
export interface LineMark extends MarkBase { type: 'line'; x1: number; y1: number; x2: number; y2: number }
export interface TextMark extends MarkBase {
  type: 'text'
  x: number
  y: number
  text: string
  /** The untruncated text when `text` was shortened; shown in the tooltip. */
  full?: string
  anchor?: 'start' | 'middle' | 'end'
  baseline?: 'auto' | 'middle' | 'hanging' | 'central' | 'alphabetic'
  size?: number
  weight?: number | string
  rotate?: number
  mono?: boolean
  /** Surface-coloured outline so the label reads over marks. */
  halo?: boolean
}
export type Mark = RectMark | CircleMark | PathMark | LineMark | TextMark

export interface Rect { x: number; y: number; width: number; height: number }

export interface LegendItem {
  key: string
  label: string
  color: Paint
  shape?: 'square' | 'line' | 'circle' | 'dash'
  pattern?: number
}
export interface LegendSpec {
  title?: string
  items?: LegendItem[]
  /** Continuous colour legend. */
  gradient?: { stops: Paint[]; min: string; max: string; mid?: string }
  /** Clicking an item hides/shows its series (default true when >1 item). */
  toggle?: boolean
}

export interface DrawResult {
  marks: Mark[]
  legend?: LegendSpec
  /** Clip the marks layer to this rect (zoomable charts). */
  clip?: Rect
  /** Visible note under the chart, e.g. "3 negative values omitted". */
  note?: string
  /** Plot rect, for brushes and zoom extents. */
  plot?: Rect
  /** Scale inverters for brush/zoom callbacks. */
  invertX?: (px: number) => unknown
  invertY?: (py: number) => unknown
}

export interface ZoomState { k: number; x: number; y: number }

export interface DrawContext<D = unknown, O = Record<string, unknown>> {
  data: D
  options: O & BaseOptions
  width: number
  height: number
  theme: Palette
  fmt: Formatters
  /** Text width in px at a font size (defaults to the tick size). */
  measure: (text: string, size?: number, weight?: number | string) => number
  /** Shorten to fit maxWidth with an ellipsis. */
  truncate: (text: string, maxWidth: number, size?: number, weight?: number | string) => string
  /** Series hidden through the legend. */
  hidden: ReadonlySet<string>
  zoom: ZoomState | null
  reducedMotion: boolean
  /** Seeded PRNG in [0,1): deterministic jitter and layouts. */
  random: () => number
  /** Axes, grid and margins for x/y charts. */
  cartesian: (spec: CartesianSpec) => CartesianFrame
  /** Draw another chart into a sub-rectangle (small multiples). */
  drawChild: (id: string, data: unknown, options: Record<string, unknown>, rect: Rect, keyPrefix: string) => DrawResult
}

export interface TableModel {
  columns: string[]
  rows: Array<Array<string | number | null>>
}

export interface DescribeContext<D = unknown, O = Record<string, unknown>> {
  data: D
  options: O & BaseOptions
  fmt: Formatters
  name: string
}

export interface ChartDef<D = unknown, O extends Record<string, unknown> = Record<string, unknown>> {
  id: string
  name: string
  category: Category
  intents: Intent[]
  /** One line: what it shows. */
  summary: string
  /** Canonical input. Rows-shaped inputs also accept CSV text, column arrays and field mapping. */
  input: z.ZodType<D, z.ZodTypeDef, unknown>
  /** Chart-specific options (merged over the base options). */
  options: z.ZodObject<z.ZodRawShape>
  shape: 'rows' | 'object'
  /** Canonical field names (rows shape), for mapping and docs. */
  fields?: string[]
  /** Fields that must be dates (parsed with time/parse). */
  dateFields?: string[]
  /** Fields that must be numbers (CSV strings coerced). */
  numberFields?: string[]
  /** Raw -> canonical before validation (tree from flat rows, etc.). */
  adapt?(raw: unknown, options: O & BaseOptions): unknown
  /** true (or a message such as "All values are zero") shows the empty state. */
  isEmpty?(data: D, options: O & BaseOptions): boolean | string
  draw(ctx: DrawContext<D, O>): DrawResult
  describe(ctx: DescribeContext<D, O>): string
  table(data: D, options: O & BaseOptions, fmt: Formatters): TableModel
  /** 'x' or 'xy': supports options.zoom / options.brush. */
  zoom?: 'x' | 'xy'
  /** 'nearest-x': a pointer anywhere over the plot hovers the data mark nearest in x
   *  (ties by y), so thin lines and hidden dots still answer. Default: only the mark under the pointer. */
  hover?: 'mark' | 'nearest-x'
  /** height = width * aspect, clamped to [minHeight, maxHeight]. */
  aspect?: number
  minHeight?: number
  maxHeight?: number
}

export interface BaseOptions {
  width?: number
  height?: number
  theme?: 'auto' | 'light' | 'dark' | 'high-contrast'
  title?: string
  description?: string
  fields?: Record<string, string>
  locale?: string
  currency?: string
  timeZone?: string
  valueFormat?: string
  animate?: boolean
  legend?: boolean
  tooltip?: boolean
  patterns?: boolean
  zoom?: boolean
  brush?: boolean
  renderer?: 'auto' | 'svg' | 'canvas'
  canvasThreshold?: number
  showTable?: boolean
  emptyMessage?: string
  seed?: number
  xDomain?: [unknown, unknown]
  yDomain?: [number, number]
  onClick?: (datum: unknown, index: number) => void
  onHover?: (datum: unknown | null, index: number) => void
  onBrush?: (range: [unknown, unknown] | null) => void
  onLegendToggle?: (series: string, visible: boolean) => void
  onZoom?: (zoom: ZoomState) => void
}

export interface ValidationIssue {
  path: string
  message: string
}

export interface ChartHandle {
  readonly type: string
  readonly root: HTMLElement
  readonly state: 'ok' | 'empty' | 'error'
  readonly errors: ValidationIssue[]
  readonly renderer: 'svg' | 'canvas'
  update(data: unknown, options?: Record<string, unknown>): void
  resize(width?: number, height?: number): void
  destroy(): void
  exportSVG(): string
  exportPNG(scale?: number): Promise<Blob>
  exportCSV(): string
  /** Number of data marks drawn. */
  markCount(): number
  /** The aria-label summary. */
  summary(): string
  /** Move keyboard focus to data mark i (announces it). */
  focusMark(index: number): void
}

export interface ChartModule<D = unknown, O extends Record<string, unknown> = Record<string, unknown>> {
  def: ChartDef<D, O>
  render(container: HTMLElement, data: unknown, options?: Partial<O & BaseOptions>): ChartHandle
  validate(data: unknown, options?: Partial<O & BaseOptions>): { ok: boolean; data?: D; errors: ValidationIssue[] }
}

/** Any chart module, without its data/option types (registries, harnesses, the API). */
export interface AnyChartModule {
  def: ChartDef<unknown, Record<string, unknown>>
  render(container: HTMLElement, data: unknown, options?: Record<string, unknown>): ChartHandle
  validate(data: unknown, options?: Record<string, unknown>): { ok: boolean; data?: unknown; errors: ValidationIssue[] }
}
