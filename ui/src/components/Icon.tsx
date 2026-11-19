import { createElement } from 'react'
import { ICONS, ICON_STROKE, ICON_VIEWBOX, type IconName } from '../../../.omp/design/icons/components/icons.ts'

export type { IconName }

/** A decorative icon sits beside text that already says what it means (a
 *  button's label, a row's name) and is hidden from assistive technology. */
type Decorative = { decorative: true; label?: never }
/** A meaningful icon is the information -- a status glyph on its own, a
 *  monogram -- and is announced by its label. */
type Meaningful = { label: string; decorative?: false }

export type IconProps = {
  name: IconName
  /** Rendered box, in px. The drawing is 24×24 with 2px padding, so 16 is the
   *  text-size default and 12 is the floor at which 1.5px strokes still read. */
  size?: 12 | 14 | 16 | 20 | 24
  className?: string
} & (Decorative | Meaningful)

/** The one way an icon reaches the screen. Geometry comes from the design
 *  pack's generated `icons.ts`; colour is `currentColor`, so an icon inherits
 *  the token of the text it sits in and never carries one of its own. Omitting
 *  both `label` and `decorative` is a type error: that choice is the
 *  accessibility decision, and it is made at the call site. */
export function Icon(props: IconProps) {
  const { name, size = 16, className } = props
  const data = ICONS[name]
  const brand = 'attrs' in data ? data.attrs : undefined
  const a11y = props.decorative
    ? { 'aria-hidden': true as const, focusable: 'false' as const }
    : { role: 'img' as const, 'aria-label': props.label }
  return (
    <svg
      viewBox={ICON_VIEWBOX}
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth={brand?.strokeWidth ?? ICON_STROKE}
      strokeLinecap={brand?.strokeLinecap ?? 'round'}
      strokeLinejoin={brand?.strokeLinejoin ?? 'round'}
      className={className}
      data-icon={name}
      {...a11y}
    >
      {data.elements.map((el, i) => createElement(el.tag, { key: i, ...el.attrs }))}
    </svg>
  )
}
